"""Interpretation of session snapshots. The shapes below are the ones observed
against the real Devin API (probe session and a real fix session)."""
import unittest

from app.outcome import interpret


class InterpretTests(unittest.TestCase):
    def test_working(self):
        r = interpret({"status": "running", "status_detail": "working", "structured_output": None})
        self.assertEqual(r.kind, "working")

    def test_probe_waiting_with_placeholder_is_blocked(self):
        # Real probe: waiting_for_user with a placeholder structured output, no explicit outcome.
        snap = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "structured_output": {"choice": "pending: awaiting user's answer (red or blue)"},
        }
        r = interpret(snap)
        self.assertEqual(r.kind, "blocked")

    def test_waiting_with_explicit_blocked_outcome(self):
        snap = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "structured_output": {"outcome": "blocked", "summary": "s", "question": "Which mitigation?"},
        }
        r = interpret(snap)
        self.assertEqual((r.kind, r.question), ("blocked", "Which mitigation?"))

    def test_idle_done_is_done_not_blocked(self):
        # The ambiguity that matters: waiting_for_user AFTER finishing the work.
        snap = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "structured_output": {"outcome": "done", "summary": "Fixed", "pull_request_urls": ["https://x/pull/1"]},
            "pull_requests": [{"pr_url": "https://x/pull/1", "pr_state": "open"}],
        }
        r = interpret(snap)
        self.assertEqual(r.kind, "done")
        self.assertEqual(r.pr_urls, ["https://x/pull/1"])  # de-duplicated across both sources

    def test_done_without_a_pr_is_failed(self):
        snap = {"status": "exit", "structured_output": {"outcome": "done", "summary": "trust me"}}
        r = interpret(snap)
        self.assertEqual(r.kind, "failed")
        self.assertIn("no pull request", r.reason)

    def test_real_suspended_idle_session_with_prs_is_done(self):
        # Real fix session: status suspended / inactivity, two merged PRs, no structured output.
        snap = {
            "status": "suspended",
            "status_detail": "inactivity",
            "structured_output": None,
            "pull_requests": [
                {"pr_url": "https://github.com/o/r/pull/1", "pr_state": "merged"},
                {"pr_url": "https://github.com/o/r/pull/2", "pr_state": "merged"},
            ],
        }
        r = interpret(snap)
        self.assertEqual(r.kind, "done")
        self.assertEqual(len(r.pr_urls), 2)

    def test_exit_with_structured_output(self):
        # Real probe after wrap-up: status exit, status_detail null, structured output kept.
        snap = {"status": "exit", "status_detail": None, "structured_output": {"choice": "blue"}}
        r = interpret(snap)
        self.assertEqual(r.kind, "failed")  # no outcome, no PR: not a valid fix session

    def test_error_status_is_failed(self):
        self.assertEqual(interpret({"status": "error"}).kind, "failed")

    def test_suspended_without_anything_is_failed(self):
        self.assertEqual(interpret({"status": "suspended", "status_detail": "inactivity"}).kind, "failed")

    def test_explicit_failed_outcome(self):
        snap = {"status": "exit", "structured_output": {"outcome": "failed", "summary": "tests kept failing"}}
        r = interpret(snap)
        self.assertEqual((r.kind, r.summary), ("failed", "tests kept failing"))


if __name__ == "__main__":
    unittest.main()
