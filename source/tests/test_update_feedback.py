"""Admission and review request invariants; private synthetic data only."""
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from framebridge_expanded import jobs,update_control,job_feedback,broker,fleet,runtime,activity_data

class UpdateAndFeedback(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.queue_root=self.root/"queue-v2"
        self.release=threading.Event()
        self.started=threading.Event()
        self.executed=[]
    def open_queue(self,unknown=False):
        def execute(row,slot):
            self.executed.append(row["id"]);self.started.set()
            self.release.wait(5)
            return {"ok":not unknown,"execution_state":"outcome_unknown" if unknown else "completed","exit_code":None if unknown else "0"}
        q=jobs.JobQueue(self.queue_root,execute,feedback_root=self.root)
        self.addCleanup(q.close);self.addCleanup(self.release.set)
        return q
    def payload(self,j):
        return {"job_id":j,"session_id":"qa","origin_request_id":"req-"+j,"script":"printf 'synthetic'","resources":["write:qa"],"timeout_s":30}
    def eventually(self,fn):
        end=time.monotonic()+6
        while time.monotonic()<end:
            value=fn()
            if value:return value
            time.sleep(.02)
        self.fail("Timed out waiting for state")
    def test_drain_blocks_new_admissions_and_preserves_existing_jobs(self):
        q=self.open_queue()
        self.assertTrue(q.submit(self.payload("one"))["ok"]);self.assertTrue(self.started.wait(2))
        self.assertTrue(q.submit(self.payload("two"))["ok"])
        gate=update_control._control(q.db,"prepare")
        self.assertTrue(gate["admission_paused"]);self.assertFalse(gate["queue_drained"])
        self.assertEqual(gate["running"],1);self.assertEqual(gate["queued"],1)
        self.assertEqual(q.submit(self.payload("three"))["error"],"UPDATE_PREPARATION_NEW_JOBS_PAUSED")
        self.assertEqual(q.submit(self.payload("one"))["execution_state"],"in_flight")
        self.release.set();self.assertEqual(q.wait("two")["execution_state"],"completed")
        self.assertTrue(update_control._control(q.db,"status")["queue_drained"])
        self.assertFalse(update_control._control(q.db,"resume")["admission_paused"])
        self.assertTrue(q.submit(self.payload("three"))["ok"])
        self.assertEqual(q.wait("three")["execution_state"],"completed")
        self.assertEqual(self.executed,["one","two","three"])
    def test_gate_survives_scheduler_reopen(self):
        q=self.open_queue()
        self.assertTrue(update_control._control(q.db,"prepare")["queue_drained"])
        q.close()
        other=self.open_queue()
        self.assertTrue(update_control._control(other.db,"status")["admission_paused"])
        self.assertEqual(other.submit(self.payload("new"))["error"],"UPDATE_PREPARATION_NEW_JOBS_PAUSED")
    def test_unknown_creates_incident_but_ack_does_not_release_receipt(self):
        q=self.open_queue(unknown=True)
        q.submit(self.payload("one"));self.release.set()
        self.assertEqual(q.wait("one")["execution_state"],"outcome_unknown")
        record=self.eventually(lambda: job_feedback.list_root(self.root)["incidents"])
        self.assertEqual(len(record),1);r=record[0]
        self.assertEqual(r["source"],"watchdog")
        self.assertEqual(r["state"],"pending");self.assertFalse(r["resolved"])
        self.assertNotIn("script",r);self.assertNotIn("stdout",r)
        with self.assertRaises(activity_data.ObservationError):
            job_feedback.acknowledge_root(self.root,r["incident_id"],r["command_sha256"],authorized=False)
        with self.assertRaises(activity_data.ObservationError):
            job_feedback.acknowledge_root(self.root,r["incident_id"],"0"*64,authorized=True)
        a=job_feedback.acknowledge_root(self.root,r["incident_id"],r["command_sha256"],authorized=True)
        self.assertFalse(a["resolved"])
        self.assertEqual(a,job_feedback.acknowledge_root(self.root,r["incident_id"],r["command_sha256"],authorized=True))
        self.assertEqual(job_feedback.list_root(self.root)["pending_count"],0)
        s=q.status("one");self.assertEqual(s["execution_state"],"outcome_unknown");self.assertTrue(s["do_not_replay"])
        gate=update_control._control(q.db,"prepare")
        self.assertFalse(gate["queue_drained"]);self.assertEqual(gate["outcome_unknown"],1)
    def test_feedback_read_independent_of_sqlite_write_lock(self):
        q=self.open_queue()
        d=hashlib.sha256(b"test").hexdigest()
        r=job_feedback.record(self.root,"manual",d,"req-manual","in_flight","local-owner")
        again=job_feedback.record(self.root,"manual",d,"req-manual","in_flight","local-owner")
        self.assertEqual(r,again)
        with closing(sqlite3.connect(q.db)) as db:
            db.execute("BEGIN EXCLUSIVE")
            self.assertEqual(job_feedback.list_root(self.root)["incidents"][0]["incident_id"],r["incident_id"])
            db.rollback()
    def test_broker_classifies_only_inert_feedback_control_payload(self):
        for a in ("feedback_list","feedback_ack"):
            req={"operation":"admin.exec","payload":{"request_type":jobs.WIRE_TYPE,"script":jobs.WIRE_SCRIPT,"job_action":a}}
            self.assertTrue(broker.is_job_control(req))
            req["payload"]["script"]="true";self.assertFalse(broker.is_job_control(req))
    def test_fleet_rejects_unauthorized_ack_before_dispatch(self):
        with patch.object(runtime,"adapter_request") as adapter:
            r=fleet.managed_job("LOCAL","feedback_ack",job_id="a"*64,sha256="b"*64,authorized=False)
            self.assertFalse(r["ok"]);adapter.assert_not_called()
    def test_exact_ack_identity_and_old_route_refusal(self):
        node={"alias":"TEST","node_id":"test","transport":"local","hostname":"test"}
        digest="b"*64;incident="a"*64
        def respond(payload):
            self.assertEqual(payload["script"],jobs.WIRE_SCRIPT)
            self.assertEqual(payload["job_action"],"feedback_ack")
            self.assertEqual(payload["job_sha256"],digest)
            return {"ok":True,"job_protocol":2,"job_id":incident,"incident_id":incident,"command_sha256":digest,"state":"acknowledged","resolved":False}
        with patch.object(fleet,"resolve_node",return_value=node),patch.object(runtime,"adapter_request",side_effect=respond):
            self.assertTrue(fleet.managed_job("TEST","feedback_ack",job_id=incident,sha256=digest,authorized=True)["ok"])
        with patch.object(fleet,"resolve_node",return_value=node),patch.object(runtime,"adapter_request",return_value={"ok":True,"exit_code":"0"}):
            self.assertFalse(fleet.managed_job("TEST","feedback_list")["ok"])

if __name__=="__main__":unittest.main()
