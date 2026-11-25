"""Read-only menu and exact receipt identity; no execution or review actions."""
import ast,math,unittest
from pathlib import Path
from unittest.mock import patch
from framebridge_expanded import owner_job_menu as menu,activity_data as data,owner_jobs as owner,unified_viewer as view,activity_ui

class InformativeMenu(unittest.TestCase):
    def setUp(self):
        self.r={"id":"synthetic","digest":"a"*64,"request":"qa-req","state":"completed",
                "created":10,"started":11,"finished":75,"exit_code":"124","capture_status":"complete"}
        self.expected=menu.identity(self.r)
    def test_only_three_informational_actions(self):
        tree=ast.parse(Path(menu.__file__).read_text())
        labels=[n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call)
                and isinstance(n.func,ast.Name) and n.func.id=="item" and n.args
                and isinstance(n.args[0],ast.Constant)]
        self.assertEqual(set(labels),{"View job details…","View verified output…","Copy job ID"})
        names={n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute)}
        self.assertFalse(names & {"request_cancel","submit_edited","original_script","acknowledge_root","request"})
        tray=Path(activity_ui.__file__).read_text()
        self.assertNotIn("job_feedback",tray);self.assertNotIn("Review requests:",tray)
        self.assertNotIn("feedback_item",tray)
    def test_duration_is_recorded_not_estimated(self):
        self.assertEqual(menu.duration(self.r),"1m 04s")
        self.assertEqual(menu.duration(dict(self.r,finished=11.2)),"<1s")
        for row in (dict(self.r,state="running"),dict(self.r,started=None),dict(self.r,finished=2),
                    dict(self.r,finished=math.nan),dict(self.r,state="outcome_unknown")):
            self.assertEqual(menu.duration(row),"-")
        self.assertEqual(menu.duration(dict(self.r,state="not_started")),"N/A")
        self.assertIn("DUR: 1m 04s | EXIT: 124",view.job_text(self.r))
    def test_details_exact_identity_and_unknown(self):
        with patch.object(owner,"details",return_value=self.r):
            v=menu.read_details(None,self.expected)
            self.assertIn("Recorded duration: 1m 04s",v)
            self.assertIn("Exit: 124",v)
        with patch.object(owner,"details",return_value=dict(self.r,digest="b"*64)):
            with self.assertRaises(data.ObservationError):menu.read_details(None,self.expected)
        with patch.object(owner,"details",return_value=dict(self.r,state="outcome_unknown")):
            v=menu.read_details(None,self.expected);self.assertIn("Exit: Unconfirmed",v);self.assertIn("Do not replay",v)
    def test_verified_output_requires_same_immutable_identity(self):
        with patch.object(owner,"row",return_value=self.r),patch.object(data,"job_output",return_value={"text":"synthetic"}) as q:
            self.assertEqual(menu.read_output(None,self.expected)["text"],"synthetic")
            q.assert_called_once_with(None,"synthetic",expected_digest="a"*64,expected_request="qa-req")
        with patch.object(owner,"row",side_effect=[self.r,dict(self.r,request="other")]),patch.object(data,"job_output",return_value={"text":"synthetic"}):
            with self.assertRaises(data.ObservationError):menu.read_output(None,self.expected)
        with patch.object(owner,"row",return_value=dict(self.r,state="running")),patch.object(data,"job_output") as q:
            with self.assertRaises(data.ObservationError):menu.read_output(None,self.expected)
            q.assert_not_called()
    def test_receipt_requires_binding_and_sanitization(self):
        r={"node_id":"node","job_id":"synthetic","command_sha256":"a"*64,"request_id":"qa-req","execution_state":"completed",
           "stdout":"synthetic","stdout_integrity":"verified","stdout_sanitized":True}
        args={"expected_digest":"a"*64,"expected_request":"qa-req"}
        self.assertEqual(data.verified_output(r,"node","synthetic",**args)["text"],"synthetic")
        for ch in ({"command_sha256":"b"*64},{"request_id":"other"},{"stdout_integrity":"unverified"},{"stdout_sanitized":False}):
            with self.assertRaises(data.ObservationError):data.verified_output(dict(r,**ch),"node","synthetic",**args)
if __name__=="__main__":unittest.main()
