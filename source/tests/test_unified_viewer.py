import unittest
from framebridge_expanded import unified_viewer as u

class CompactHistory(unittest.TestCase):
    def test_one_line_with_recorded_duration_without_timestamps(self):
        row = {"id":"demo-job-1", "state":"completed", "exit_code":"0",
               "created":100,"started":101,"finished":102}
        self.assertEqual(u.job_text(row), "JOB: demo-job-1 | STATE: Completed | DUR: 1s | EXIT: 0")
        self.assertEqual(u.history_text([row,row]).count("\n"),1)
        self.assertNotIn("CREATED",u.history_text([row]))
        self.assertNotIn("STARTED",u.history_text([row]))
        self.assertNotIn("FINISHED",u.history_text([row]))
    def test_observations_not_guessed(self):
        for state, code, colour in [
            ("completed","0","success"),("completed","3","error"),
            ("completed",None,"warning"),("running",None,"active"),
            ("queued",None,"warning"),("outcome_unknown",None,"warning")]:
            with self.subTest(state=state,code=code):
                row={"id":"demo-job-1","state":state,"exit_code":code}
                self.assertEqual(u.job_colour(row),colour)
                self.assertNotIn("EXIT: 0",u.job_text(row)) if code != "0" else None
        self.assertEqual(u.summary(None),"Execution state not verified")

    def test_nonexecuted_exit_labels(self):
        for state in ("not_started","expired_before_start"):
            self.assertTrue(u.job_text({"id":"demo-job","state":state,"exit_code":None}).endswith("EXIT: N/A"))
        for state in ("queued","in_flight","running"):
            self.assertTrue(u.job_text({"id":"demo-job","state":state,"exit_code":None}).endswith("EXIT: -"))
        self.assertTrue(u.job_text({"id":"demo-job","state":"outcome_unknown","exit_code":None}).endswith("EXIT: Unconfirmed"))
        self.assertTrue(u.job_text({"id":"demo-job","state":"completed","exit_code":None}).endswith("EXIT: Unconfirmed"))
