"""Regression coverage for authenticated job receipts and dispatch deadlines."""
import hashlib
import socket
import threading
import time
import unittest
from unittest import mock

from framebridge_expanded import broker, node
import test_fleet_broker_node as fixtures


class BrokerDispatchDeadlineTests(unittest.TestCase):
    setUp = fixtures.BrokerNodeTests.setUp
    tearDown = fixtures.BrokerNodeTests.tearDown
    make_node = fixtures.BrokerNodeTests.make_node
    pair = fixtures.BrokerNodeTests.pair

    def payload(self, job_id="deadline-test", script="printf harmless"):
        return {"authorized": True, "job_id": job_id, "timeout_s": 1,
                "script": script, "sha256": hashlib.sha256(script.encode()).hexdigest()}

    def enqueue(self, ident, payload, start_by):
        created = broker._control("admin-exec", {"node_id": ident, "payload": payload,
                                                  "start_by": start_by})
        return created["request_id"]

    def ready(self, ident):
        return mock.patch.object(broker, "list_confirmed_nodes", return_value=[
            {"node_id": ident, "ready": True, "supports_start_deadline": True}])

    def test_old_broker_does_not_receive_admin_work(self):
        with mock.patch.object(broker, "list_confirmed_nodes",
                               return_value=[{"node_id": "old-node", "ready": True}]), \
                mock.patch.object(broker, "_control") as control:
            with self.assertRaisesRegex(broker.BrokerError, "^BROKER_START_DEADLINE_UNSUPPORTED$"):
                broker.request("old-node", "admin.exec", self.payload(), timeout_s=1)
        control.assert_not_called()

    def test_late_submission_expires_without_a_dispatch(self):
        _, ident = self.pair("expired-node")
        receipt_id = self.enqueue(ident, self.payload(), time.time() - 1)
        receipt = broker.request_status(ident, job_id="deadline-test")
        self.assertEqual(receipt["state"], "expired")
        self.assertEqual(receipt["request_id"], receipt_id)
        self.assertIsNone(self.server.expanded_store.next_read(ident))
        self.assertFalse(self.server.expanded_store.put_result(ident, receipt_id, {"ok": True}))

    def test_pending_deadline_expires_even_without_a_status_query(self):
        _, ident = self.pair("pending-node")
        start_by = time.time() + 5
        receipt_id = self.enqueue(ident, self.payload(), start_by)
        store = self.server.expanded_store
        with mock.patch.object(broker.time, "time", return_value=start_by):
            self.assertIsNone(store.next_read(ident))
        self.assertEqual(broker.request_status(ident, request_id=receipt_id)["state"], "expired")

    def test_cancellation_and_claim_are_mutually_exclusive(self):
        _, ident = self.pair("race-node")
        store = self.server.expanded_store
        for iteration in range(10):
            receipt_id = self.enqueue(ident, self.payload(f"race-{iteration}"), time.time() + 10)
            gate = threading.Barrier(3)
            results = {}

            def claim():
                gate.wait()
                results["claim"] = store.next_read(ident)

            def cancel():
                gate.wait()
                results["cancel"] = store.admin_request_status(ident, receipt_id, "", True)

            workers = [threading.Thread(target=target) for target in (claim, cancel)]
            for worker in workers:
                worker.start()
            gate.wait()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            if results["cancel"]["state"] == "cancelled":
                self.assertIsNone(results["claim"])
                self.assertFalse(store.put_result(ident, receipt_id, {"ok": True}))
            else:
                self.assertEqual(results["cancel"]["state"], "dispatched")
                self.assertEqual(results["claim"]["request_id"], receipt_id)
                self.assertTrue(store.put_result(ident, receipt_id, {"ok": True}))

    def test_lost_enqueue_reply_reconciles_by_job_id(self):
        _, ident = self.pair("lost-enqueue-node")
        real_control = broker._control

        def drop_reply(route, args, **kwargs):
            response = real_control(route, args, **kwargs)
            if route == "admin-exec":
                raise broker.BrokerError("BROKER_TRANSPORT_FAILED")
            return response

        with mock.patch.object(broker, "_control", side_effect=drop_reply), self.ready(ident):
            with self.assertRaises(broker.BrokerRequestError) as failure:
                broker.request(ident, "admin.exec", self.payload(), timeout_s=1)
        self.assertEqual(str(failure.exception), "BROKER_REQUEST_CANCELLED")
        self.assertEqual(failure.exception.execution_state, "expired_before_dispatch")
        receipt = broker.request_status(ident, job_id="deadline-test")
        self.assertEqual(receipt["request_id"], failure.exception.request_id)
        self.assertEqual(receipt["state"], "cancelled")
        self.assertIsNone(self.server.expanded_store.next_read(ident))

    def test_lost_result_query_after_claim_preserves_late_receipt(self):
        _, ident = self.pair("in-flight-node")
        store = self.server.expanded_store
        real_control = broker._control

        def lose_result(route, args, **kwargs):
            if route == "result":
                self.assertIsNotNone(store.next_read(ident))
                raise broker.BrokerError("BROKER_TRANSPORT_FAILED")
            return real_control(route, args, **kwargs)

        payload = self.payload()
        with mock.patch.object(broker, "_control", side_effect=lose_result), self.ready(ident):
            with self.assertRaises(broker.BrokerRequestError) as failure:
                broker.request(ident, "admin.exec", payload, timeout_s=1)
        rid = failure.exception.request_id
        self.assertEqual(failure.exception.execution_state, "in_flight")
        self.assertEqual(self.enqueue(ident, payload, time.time() + 10), rid)
        self.assertIsNone(store.next_read(ident))
        self.assertTrue(store.put_result(ident, rid, {"ok": True}))
        self.assertTrue(store.put_result(ident, rid, {"ok": True}))
        self.assertFalse(store.put_result(ident, rid, {"ok": False}))
        self.assertEqual(broker.request_status(ident, request_id=rid)["result"], {"ok": True})
        self.assertEqual(broker.request_status(ident, request_id=rid, job_id="other")["state"], "unknown")
        self.assertEqual(broker.request_status("different-node", request_id=rid)["state"], "unknown")
        with self.assertRaises(broker.BrokerError):
            self.enqueue(ident, self.payload(script="printf other"), time.time() + 10)

    def test_transport_loss_with_no_verified_receipt_is_unknown(self):
        _, ident = self.pair("unknown-node")
        with mock.patch.object(broker, "_control", side_effect=broker.BrokerError("BROKER_TRANSPORT_FAILED")), \
                self.ready(ident):
            with self.assertRaises(broker.BrokerRequestError) as failure:
                broker.request(ident, "admin.exec", self.payload(), timeout_s=1)
        self.assertEqual(failure.exception.execution_state, "outcome_unknown")
        self.assertIsNone(failure.exception.request_id)

    def test_completed_result_wins_reconciliation(self):
        _, ident = self.pair("complete-node")
        store = self.server.expanded_store
        real_control = broker._control

        def lose_result(route, args, **kwargs):
            if route == "result":
                claimed = store.next_read(ident)
                self.assertTrue(store.put_result(ident, claimed["request_id"], {"ok": True}))
                raise broker.BrokerError("BROKER_TRANSPORT_FAILED")
            return real_control(route, args, **kwargs)

        with mock.patch.object(broker, "_control", side_effect=lose_result), self.ready(ident):
            result = broker.request(ident, "admin.exec", self.payload(), timeout_s=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["request_id"],
                         broker.request_status(ident, job_id="deadline-test")["request_id"])

    def test_transient_result_poll_retries_without_cancelling(self):
        _, ident = self.pair("poll-retry-node")
        store = self.server.expanded_store
        real_control = broker._control
        routes = []

        def retry_result(route, args, **kwargs):
            routes.append(route)
            if route == "result" and routes.count("result") == 1:
                claimed = store.next_read(ident)
                self.assertTrue(store.put_result(ident, claimed["request_id"], {"ok": True}))
                raise broker.BrokerError("BROKER_HTTP_REJECTED_503")
            return real_control(route, args, **kwargs)

        with mock.patch.object(broker, "_control", side_effect=retry_result), self.ready(ident):
            result = broker.request(ident, "admin.exec", self.payload(), timeout_s=1)
        self.assertTrue(result["ok"])
        self.assertEqual(routes, ["admin-exec", "result", "result"])

    def test_active_heartbeat_tracking_outlives_a_claimed_jobs_start_deadline(self):
        _, ident = self.pair("active-node")
        start_by = time.time() + 5
        receipt_id = self.enqueue(ident, self.payload(), start_by)
        store = self.server.expanded_store
        self.assertEqual(store.next_read(ident)["request_id"], receipt_id)
        with store.lock:
            self.assertEqual(store._active_node_requests(ident, start_by), [receipt_id])
            self.assertEqual(store._active_node_requests(ident, start_by + 100), [])

    def test_node_rechecks_start_deadline_and_stamps_real_host(self):
        state, ident = self.pair("receiver-node")
        expires_at = time.time() + 10
        request = {"request_id": "valid-receipt", "node_id": ident,
                   "expires_at": expires_at, "operation": "admin.exec", "payload": self.payload()}
        adapter = mock.Mock(return_value={"ok": True, "executed_hostname": "injected"})
        with mock.patch.object(node, "time") as clock:
            clock.time.side_effect = [expires_at - 1, expires_at]
            self.assertEqual(node.dispatch_request(request, adapter, state), {
                "ok": False, "error": "REQUEST_EXPIRED", "execution_state": "expired_before_start"})
        adapter.assert_not_called()
        result = node.dispatch_request(request, adapter, state)
        self.assertEqual(result["executed_hostname"], socket.gethostname())
        self.assertEqual(result["stdout"], "")
        adapter.assert_called_once()
        self.assertEqual(adapter.call_args.args[0]["start_before"], expires_at)
        request.update(operation="admin.cua", payload={
            "authorized": True, "job_id": "cua-test", "timeout_s": 1,
            "cua": {"action": "OBSERVE"}})
        self.assertEqual(node.dispatch_request(request, adapter, state)["executed_hostname"],
                         socket.gethostname())
        request.pop("operation")
        request.update(capability="system.status", args={})
        self.assertEqual(node.dispatch_request(request, state_dir=state)["executed_hostname"],
                         socket.gethostname())

    def test_node_reports_uncertain_adapter_outcomes_without_replay(self):
        state, ident = self.pair("uncertain-node")
        request = {"request_id": "valid-receipt", "node_id": ident,
                   "expires_at": time.time() + 10, "operation": "admin.exec",
                   "payload": self.payload()}
        for adapter, error in (
            (mock.Mock(side_effect=OSError("unavailable receipt")), "NODE_DISPATCH_FAILED"),
            (mock.Mock(return_value=None), "ADAPTER_RESPONSE_INVALID"),
        ):
            with self.subTest(error=error):
                self.assertEqual(node.dispatch_request(request, adapter, state), {
                    "ok": False, "error": error, "execution_state": "outcome_unknown",
                    "do_not_replay": True})
                adapter.assert_called_once()
        adapter = mock.Mock()
        invalid = dict(request, node_id="different-node")
        self.assertEqual(node.dispatch_request(invalid, adapter, state), {
            "ok": False, "error": "REQUEST_NODE_MISMATCH", "execution_state": "not_started"})
        adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
