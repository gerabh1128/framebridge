import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from framebridge_expanded import activity_data as a, config


class ActivityDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/"profile"
        self.settings=config.default_config(role="node",name="qa-node")
        config.save_config(self.path,self.settings)
        self.p=a.Profile(self.path,self.settings["node_id"],"qa-node","node")
        self.db=self.path/"home/.local/share/framebridge/fbterm/queue-v2/jobs.sqlite3"
        self.db.parent.mkdir(parents=True,mode=0o700)
        c=sqlite3.connect(self.db)
        try:
            c.execute("create table jobs(id text,state text,created real,started real,finished real,result text,script text)")
            c.executemany("insert into jobs values (?,?,?,?,?,?,?)",[
                ("queued-job","queued",10,None,None,None,"PRIVATE_TEST_COMMAND"),
                ("active-job","in_flight",20,21,None,None,"PRIVATE_TEST_COMMAND"),
                ("unknown-job","outcome_unknown",30,31,None,None,"PRIVATE_TEST_COMMAND"),
                ("failed-job","completed",40,41,42,json.dumps({"exit_code":"1","stdout":"PRIVATE_TEST_OUTPUT"}),"PRIVATE_TEST_COMMAND"),
                ("passed-job","completed",50,51,52,json.dumps({"exit_code":"0"}),"PRIVATE_TEST_COMMAND"),
                ("expired-job","expired_before_start",60,None,None,None,"PRIVATE_TEST_COMMAND")])
            c.commit()
        finally:c.close()
        self.db.chmod(0o600)

    def test_read_only_counts_and_private_columns_not_exported(self):
        before=self.db.read_bytes()
        r=a.local_jobs(self.p)
        self.assertEqual(r["total"],6)
        self.assertEqual(r["counts"]["completed"],2)
        self.assertEqual(r["counts"]["outcome_unknown"],1)
        self.assertEqual(r["counts"]["in_flight"],1)
        self.assertNotIn("PRIVATE_TEST",json.dumps(r))
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(r["jobs"][0]["id"],"unknown-job")
        self.assertEqual(next(x for x in r["jobs"] if x["id"]=="failed-job")["exit_code"],"1")

    def test_missing_queue_is_not_empty_and_does_not_create_files(self):
        self.db.unlink()
        with self.assertRaises(OSError):a.local_jobs(self.p)
        self.assertFalse(self.db.exists())

    def test_symlink_database_is_rejected(self):
        original=self.root/"other.sqlite3"
        self.db.rename(original)
        self.db.symlink_to(original)
        with self.assertRaises(a.ObservationError):a.local_jobs(self.p)

    def test_profile_identity_changed_refuses_read_and_command(self):
        self.settings["node_id"]=str(uuid.uuid4());config.save_config(self.path,self.settings)
        with self.assertRaises(ValueError):a.local_jobs(self.p)
        with self.assertRaises(ValueError):a.local_argv(self.p,"status")

    def test_local_output_must_be_bound_verified_and_sanitized(self):
        node=self.p.node_id
        good={"node_id":node,"job_id":"qa-1","execution_state":"completed",
              "stdout":"hello\nworld","stdout_integrity":"verified","stdout_sanitized":True}
        self.assertEqual(a.verified_output(good,node,"qa-1")["text"],"hello\nworld")
        for changed in ({"stdout_integrity":"unverified"},{"stdout_sanitized":False},
                        {"job_id":"qa-2"},{"node_id":str(uuid.uuid4())},
                        {"execution_state":"outcome_unknown"}):
            with self.subTest(changed=changed),self.assertRaises(a.ObservationError):
                a.verified_output(dict(good,**changed),node,"qa-1")

    def test_job_output_is_bound_to_local_target_only(self):
        r={"node_id":self.p.node_id,"job_id":"qa-1","execution_state":"completed",
           "stdout":"local receipt","stdout_integrity":"verified","stdout_sanitized":True}
        with mock.patch.object(a,"query",return_value=r) as q:
            self.assertEqual(a.job_output(self.p,"qa-1")["text"],"local receipt")
            self.assertEqual(q.call_args.args[0],
              [a.FB,"--state-dir",str(self.p.path),"--json","nodes","jobs",
               "--target","LOCAL","--job-id","qa-1","--include-stdout"])
        with mock.patch.object(a,"query") as q:
            with self.assertRaises(a.ObservationError):a.job_output(self.p,"../bad")
        q.assert_not_called()

    def test_no_remote_administration_surface_in_desktop_modules(self):
        from framebridge_expanded import activity_ui
        for name in ("Central","inventory","selected_node_jobs","remote_jobs"):
            self.assertFalse(hasattr(a,name))
        text=Path(activity_ui.__file__).read_text()
        for name in ("connect_dialog","refresh_inventory","refresh_remote","Visores de los nodos",
                     "Conectar a central","Solicitar inscripción","nodes","--target"):
            self.assertNotIn(name,text)
        data_text=Path(a.__file__).read_text()
        self.assertNotIn('["ssh"',data_text)
        self.assertNotIn("StrictHostKeyChecking",data_text)

    def test_restart_refuses_active_and_uncertain_work(self):
        with mock.patch.object(a,"local_status",return_value={"managed":True}),mock.patch.object(a,"query") as q:
            with self.assertRaisesRegex(a.ObservationError,"WORK_ACTIVE_OR_UNCONFIRMED"):a.service(self.p,"restart")
        q.assert_not_called()

    def test_restart_does_not_start_after_unconfirmed_stop(self):
        with mock.patch.object(a,"local_status",return_value={"managed":True}),mock.patch.object(a,"local_jobs",return_value={"counts":{},"other":0}),mock.patch.object(a,"query",return_value={"ok":True,"state":"stop_incomplete"}) as q:
            with self.assertRaisesRegex(a.ObservationError,"STOP_NOT_CONFIRMED"):a.service(self.p,"restart")
        self.assertEqual(q.call_count,1)

    def test_restart_preserves_lifecycle_and_waits_for_confirmed_stop(self):
        with mock.patch.object(a,"local_status",side_effect=[{"managed":True},{"running":False}]),mock.patch.object(a,"local_jobs",return_value={"counts":{},"other":0}),mock.patch.object(a,"query",side_effect=[{"ok":True,"state":"stopped"},{"ok":True,"state":"start_requested"}]) as q:
            self.assertEqual(a.service(self.p,"restart"),"start_requested")
        self.assertEqual([x.args[0][-2:] for x in q.call_args_list],[["service","stop"],["service","start"]])

    def test_activity_indicator_reflects_verified_local_observations(self):
        ready={"running":True,"ready":True,"role":"node","connection":"fresh"}
        for status,history,expected in (
            (None,None,"attention"),
            ({"running":False},None,"stopped"),
            (dict(ready,ready=False),{"counts":{}},"attention"),
            (ready,None,"attention"),
            (ready,{"counts":{},"other":0},"ready"),
            (ready,{"counts":{"in_flight":1}},"busy"),
            (ready,{"counts":{"queued":1}},"attention"),
            (ready,{"counts":{"outcome_unknown":1,"in_flight":1}},"attention"),
            (ready,{"counts":{},"other":1},"attention"),
            (dict(ready,connection="unverified"),{"counts":{}},"attention"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(a.activity_indicator(status,history)[0],expected)

    def test_runtime_environment_does_not_leak_into_owner_cli(self):
        with mock.patch.dict(os.environ,{"FRAMEBRIDGE_EXPANDED_STATE":"/tmp/test","PYTHONPATH":"/tmp/test","FBTERM_ROOT":"/tmp/test"}):
            e=a.owner_environment()
        self.assertNotIn("FRAMEBRIDGE_EXPANDED_STATE",e)
        self.assertNotIn("PYTHONPATH",e)
        self.assertNotIn("FBTERM_ROOT",e)
