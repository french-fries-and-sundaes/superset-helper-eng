"""M6: the independent verification gate (GitHub Actions result decides in-review vs failed)."""
import unittest

from app.states import State
from tests.helpers import REPO, build, pr_event, run_ticks, state_of, task, workflow_run

from app.webhook import handle_event


def deliver(orch, event, payload):
    return handle_event(event, payload, None, orch, orch.settings)


def finish_session(orch, issue=1, title="Fix thing"):
    orch.request_ready(REPO, issue, title, "body")
    run_ticks(orch)  # Devin reports done; PR is issue+100


class GateTests(unittest.TestCase):
    def test_done_session_waits_for_verification(self):
        orch, store, devin, gh, _ = build(verify_mode="actions")
        finish_session(orch)
        t = task(store, 1)
        self.assertEqual(t["state"], "in-progress")  # NOT in-review yet
        self.assertEqual(t["verify_status"], "pending")
        self.assertEqual(t["pr_urls"], [f"https://github.com/{REPO}/pull/101"])
        self.assertEqual(devin.messages_sent[0][0], t["current_session_id"])  # session was wrapped up

    def test_passing_run_moves_to_review(self):
        orch, store, *_ = build(verify_mode="actions")
        finish_session(orch)
        r = deliver(orch, "workflow_run", workflow_run("success", [101]))
        self.assertEqual(r["result"], "passed")
        self.assertEqual(state_of(store, 1), "in-review")
        self.assertEqual(task(store, 1)["verify_status"], "passed")

    def test_failing_run_fails_the_task_with_a_reason(self):
        orch, store, *_ = build(verify_mode="actions")
        finish_session(orch)
        deliver(orch, "workflow_run", workflow_run("failure", [101], run_id=88))
        t = task(store, 1)
        self.assertEqual(t["state"], "failed")
        self.assertIn("verification check failed", t["last_summary"])
        self.assertIn("actions/runs/88", t["last_summary"])

    def test_result_that_arrives_before_the_session_reports_done(self):
        # The workflow can finish before Devin's session says done. The PR-opened event links
        # the PR to the task early, so the stored result is used the moment the session finishes.
        orch, store, *_ = build(verify_mode="actions")
        orch.request_ready(REPO, 1, "Fix thing", "body")
        orch.dispatch_ready()  # session started, still working
        deliver(orch, "pull_request", pr_event("opened", 101, "Closes #1"))
        deliver(orch, "workflow_run", workflow_run("success", [101]))
        self.assertEqual(state_of(store, 1), "in-progress")  # gate only applies once done
        run_ticks(orch)
        self.assertEqual(state_of(store, 1), "in-review")

    def test_stale_result_for_an_older_commit_is_ignored(self):
        orch, store, *_ = build(verify_mode="actions")
        orch.request_ready(REPO, 1, "Fix thing", "body")
        orch.dispatch_ready()
        deliver(orch, "pull_request", pr_event("opened", 101, "Closes #1", sha="new"))
        r = deliver(orch, "workflow_run", workflow_run("failure", [101], sha="old"))
        self.assertEqual(r["result"], "stale")
        self.assertEqual(store.prs_for_task(task(store, 1)["id"])[0]["verify_status"], "")

    def test_new_commits_reset_verification(self):
        orch, store, *_ = build(verify_mode="actions")
        finish_session(orch)
        deliver(orch, "workflow_run", workflow_run("success", [101]))
        pr = store.prs_for_task(task(store, 1)["id"])[0]
        self.assertEqual(pr["verify_status"], "passed")
        deliver(orch, "pull_request", pr_event("synchronize", 101, sha="sha2"))
        pr = store.prs_for_task(task(store, 1)["id"])[0]
        self.assertEqual((pr["verify_status"], pr["head_sha"]), ("", "sha2"))

    def test_cancelled_and_other_workflows_are_ignored(self):
        orch, store, *_ = build(verify_mode="actions")
        finish_session(orch)
        self.assertEqual(deliver(orch, "workflow_run", workflow_run("cancelled", [101]))["result"], "ignored")
        self.assertEqual(deliver(orch, "workflow_run", workflow_run("failure", [101], name="lint"))["result"], "ignored")
        self.assertEqual(task(store, 1)["verify_status"], "pending")

    def test_matches_by_head_sha_when_pull_requests_is_empty(self):
        orch, store, *_ = build(verify_mode="actions")
        orch.request_ready(REPO, 1, "Fix thing", "body")
        orch.dispatch_ready()
        deliver(orch, "pull_request", pr_event("opened", 101, "Closes #1", sha="abc"))
        run_ticks(orch)
        r = deliver(orch, "workflow_run", workflow_run("success", [], sha="abc"))
        self.assertEqual(r["result"], "passed")
        self.assertEqual(state_of(store, 1), "in-review")

    def test_unknown_pr_is_reported_not_crashed(self):
        orch, *_ = build(verify_mode="actions")
        self.assertEqual(deliver(orch, "workflow_run", workflow_run("success", [999]))["result"], "no_matching_task")

    def test_a_revision_never_reuses_the_previous_commits_passing_result(self):
        from tests.helpers import review_event

        orch, store, *_ = build(verify_mode="actions")
        finish_session(orch)
        deliver(orch, "workflow_run", workflow_run("success", [101]))
        self.assertEqual(state_of(store, 1), "in-review")
        deliver(orch, "pull_request_review", review_event(101, "changes_requested", "add a test"))
        run_ticks(orch)  # the revision session finishes; NO new verification result has arrived yet
        self.assertEqual(state_of(store, 1), "in-progress")
        self.assertEqual(task(store, 1)["verify_status"], "pending")
        deliver(orch, "workflow_run", workflow_run("success", [101], run_id=78))
        self.assertEqual(state_of(store, 1), "in-review")

    def test_verify_mode_off_skips_the_gate(self):
        orch, store, *_ = build(verify_mode="off")
        finish_session(orch)
        self.assertEqual(state_of(store, 1), "in-review")

    def test_two_prs_must_both_pass(self):
        orch, store, *_ = build(verify_mode="actions")
        finish_session(orch)
        tid = task(store, 1)["id"]
        store.upsert_pr(tid, 202, "https://github.com/o/r/pull/202", "s", "open")
        deliver(orch, "workflow_run", workflow_run("success", [101]))
        self.assertEqual(state_of(store, 1), "in-progress")  # second PR not verified yet
        deliver(orch, "workflow_run", workflow_run("success", [202], sha="s"))
        self.assertEqual(state_of(store, 1), "in-review")


if __name__ == "__main__":
    unittest.main()


class BrokenWorkflowAndTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.orch, self.store, self.devin, self.gh, self.clock = build(verify_mode="actions")
        self.orch.request_ready(REPO, 1, "Fix thing", "body")
        run_ticks(self.orch)
        self.assertEqual(state_of(self.store, 1), "in-progress")
        self.assertEqual(task(self.store, 1)["verify_status"], "pending")
        self.sha = self.store.prs_for_task(task(self.store, 1)["id"])[0]["head_sha"]

    def test_a_run_of_an_invalid_workflow_file_fails_the_task_with_a_clear_reason(self):
        # GitHub names such a run after the file path and attaches no pull request to it.
        run = {"name": ".github/workflows/devin-verify.yml", "path": ".github/workflows/devin-verify.yml",
               "conclusion": "failure", "head_sha": self.sha, "html_url": "https://github.com/o/r/actions/runs/9",
               "id": 9, "pull_requests": []}
        if not self.sha:  # fake PRs may carry no sha: link by pull request number instead
            run["pull_requests"] = [{"number": self.store.prs_for_task(task(self.store, 1)["id"])[0]["pr_number"]}]
        self.assertEqual(self.orch.handle_workflow_run(REPO, run), "failed")
        self.assertEqual(state_of(self.store, 1), "failed")
        self.assertIn("workflow file may be invalid", task(self.store, 1)["last_summary"])

    def test_other_workflows_are_still_ignored(self):
        run = {"name": "unit-tests", "path": ".github/workflows/unit-tests.yml", "conclusion": "failure",
               "head_sha": self.sha, "pull_requests": []}
        self.assertEqual(self.orch.handle_workflow_run(REPO, run), "ignored")
        self.assertEqual(state_of(self.store, 1), "in-progress")

    def test_a_check_that_never_reports_times_out_into_failed(self):
        self.clock.t += 44 * 60
        self.assertEqual(self.orch.check_verify_timeouts(), 0)
        self.assertEqual(state_of(self.store, 1), "in-progress")
        self.clock.t += 2 * 60
        self.assertEqual(self.orch.check_verify_timeouts(), 1)
        t = task(self.store, 1)
        self.assertEqual((t["state"], t["verify_status"]), ("failed", "timed_out"))
        self.assertIn("never reported", t["last_summary"])
        self.assertEqual(self.orch.check_verify_timeouts(), 0)  # once only

    def test_a_result_that_arrives_in_time_wins_over_the_timeout(self):
        pr = self.store.prs_for_task(task(self.store, 1)["id"])[0]
        self.store.set_pr_verify(task(self.store, 1)["id"], pr["pr_number"], "passed", "u", 1)
        self.orch.evaluate_gate(task(self.store, 1)["id"])
        self.clock.t += 3600
        self.orch.check_verify_timeouts()
        self.assertEqual(state_of(self.store, 1), "in-review")
