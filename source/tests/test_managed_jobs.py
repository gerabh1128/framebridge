"""Job identity, overlap, fair claims and recovery in private profiles."""
import hashlib
import json
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from framebridge_expanded import fleet, jobs, runtime


class ManagedJobs(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release = threading.Event()
        self.calls = []

        def execute(row, slot):
            self.calls.append(row["id"])
            self.release.wait(4)
            return {"ok":True, "execution_state":"completed","exit_code":"0"}

        self.queue = jobs.JobQueue(self.root, execute)
        self.addCleanup(self.queue.close)
        self.addCleanup(self.release.set)

    def payload(self, job, resources):
        return {"job_id":job,"session_id":"chat-" + job,"script":"true", "resources":resources}

    def wait_for(self, condition):
        end = time.monotonic() + 4
        while not condition():
            if time.monotonic() > end:
                self.fail("expected transition timed out")
            time.sleep(0.01)

    def test_shared_readers_overlap_and_writer_reports_blocker(self):
        for name in ("one", "two"):
            self.assertTrue(self.queue.submit(self.payload(name, ["read:disk"]))["ok"])
        self.wait_for(lambda: len(self.calls) == 2)
        queued = self.queue.submit(self.payload("three", ["write:disk"]))
        self.assertIn(queued["blocking_job_id"], ("one","two"))
        self.assertEqual(queued["wait_reason"], "resource")
        self.assertEqual(self.queue.status("three")["execution_state"], "queued")
        self.release.set()
        self.assertEqual(self.queue.wait("three")["exit_code"], "0")
        self.assertTrue(all(self.queue.status(i)["finished_at"] for i in self.calls))

    def test_independent_resource_progresses_despite_an_earlier_waiter(self):
        self.queue.submit(self.payload("first", ["write:usb"]))
        self.wait_for(lambda: "first" in self.calls)
        self.queue.submit(self.payload("second", ["write:usb"]))
        self.queue.submit(self.payload("third", ["write:archive"]))
        self.wait_for(lambda: "third" in self.calls)
        self.assertNotIn("second", self.calls)
        self.assertEqual(self.queue.status("second")["blocking_job_id"], "first")

    def test_duplicate_id_conflict_and_exact_duplicate(self):
        req = self.payload("same", ["write:usb"])
        self.queue.submit(req)
        answer = self.queue.submit(dict(req, script="false"))
        self.assertEqual(answer["error"],"JOB_ID_CONFLICT")
        self.release.set()
        self.queue.wait("same")
        self.assertEqual(self.queue.submit(req)["execution_state"],"completed")
        self.assertEqual(self.calls, ["same"])

    def test_restart_quarantines_the_uncertain_resource_and_preserves_queued_jobs(self):
        self.queue.close()
        with closing(self.queue.connect()) as db, db:
            for job, claim in (("old","usb"),("conflict","usb"),("unrelated","disk")):
                state = "in_flight" if job == "old" else "queued"
                db.execute("""INSERT INTO jobs (id,session,request,digest,resources,timeout,script,state,
                              created,deadline,slot,legacy) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)""",
                           (job,"chat","request-"+job,"a"*64,json.dumps({claim:"write"}),30,
                            "true",state,time.time(),time.time()+30,1 if job=="old" else None))
        self.release.set()
        other = jobs.JobQueue(self.root, self.queue.execute)
        self.addCleanup(other.close)
        self.assertEqual(other.wait("unrelated")["execution_state"],"completed")
        self.assertEqual(other.status("conflict")["blocking_job_id"],"old")
        self.assertTrue(other.status("old")["do_not_replay"])
        self.assertNotIn("old",self.calls)

    def test_legacy_adapter_cannot_execute_an_async_payload_as_shell(self):
        node = {"alias":"TEST","node_id":"test","transport":"local","hostname":"test"}
        def old(payload):
            self.assertEqual(payload["script"], jobs.WIRE_SCRIPT)
            self.assertTrue(payload["script"].startswith("#"))
            return {"ok":True,"exit_code":"0","job_id":payload["job_id"]}
        with patch.object(fleet,"resolve_node",return_value=node), patch.object(runtime,"adapter_request",side_effect=old):
            result = fleet.managed_job("TEST","submit",authorized=True,session_id="a",script="sentinel")
        self.assertEqual(result["error"],"JOB_PROTOCOL_UNSUPPORTED")
