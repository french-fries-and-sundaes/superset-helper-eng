"""M7 and M8: the scan and learn jobs."""
import unittest

from app.github_client import GitHubAPIError, RecordingGitHub
from app.states import State
from tests.helpers import REPO, SOLUTION, build, state_of, task


def finish_jobs(orch, n=5):
    for _ in range(n):
        orch.poll_jobs()


class ScanTests(unittest.TestCase):
    def test_scan_files_proposed_issues_with_the_sections_and_registers_tasks(self):
        orch, store, devin, gh, _ = build()
        res = orch.start_scan()
        self.assertEqual(res["status"], "started")
        finish_jobs(orch)
        job = store.get_job(res["job"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(job["result"]["filed"]), 3)
        self.assertEqual(len(job["result"]["skipped"]), 1)  # the scan reports what it skipped
        created = [kw for name, kw in gh.calls if name == "create_issue"]
        self.assertEqual(len(created), 3)
        self.assertIn("devin:proposed", created[0]["labels"])
        self.assertIn("type:dependency", created[0]["labels"])
        proposed = store.list_tasks(State.PROPOSED)
        self.assertEqual(len(proposed), 3)
        body = proposed[0]["body"]
        for section in ("## Bug Description", "## How to Triage / Test", "## Verify command", "```verify"):
            self.assertIn(section, body)
        self.assertEqual(proposed[0]["source"], "scan")

    def test_scan_makes_no_code_changes_and_asks_to_skip_existing_issues(self):
        orch, store, devin, *_ = build()
        orch.start_scan()
        prompt = devin.created_prompts[-1]
        self.assertIn("Skip anything already covered by an existing issue", prompt)
        self.assertIn("Do NOT change any code", prompt)
        self.assertIn("at most 7", prompt)

    def test_findings_are_capped_per_sweep(self):
        orch, store, *_ = build(scan_max_findings=2)
        orch.start_scan()
        finish_jobs(orch)
        self.assertEqual(len(store.list_tasks(State.PROPOSED)), 2)
        self.assertEqual(store.list_jobs("scan")[0]["result"]["truncated"], 1)

    def test_proposed_tasks_wait_for_a_human_and_then_run(self):
        orch, store, *_ = build()
        orch.start_scan()
        finish_jobs(orch)
        orch.tick()
        self.assertEqual(len(store.list_tasks(State.PROPOSED)), 3)  # nothing starts without approval
        t = store.list_tasks(State.PROPOSED)[0]
        orch.request_ready(REPO, t["issue_number"], t["title"], t["body"])
        for _ in range(6):
            orch.tick()
        self.assertEqual(state_of(store, t["issue_number"]), "in-review")

    def test_only_one_scan_at_a_time(self):
        orch, *_ = build()
        first = orch.start_scan()
        second = orch.start_scan()
        self.assertEqual(second["status"], "already_running")
        self.assertEqual(second["job"], first["job"])

    def test_a_github_error_on_one_issue_does_not_lose_the_others(self):
        class Flaky(RecordingGitHub):
            n = 0

            def create_issue(self, repo, title, body, labels):
                self.n += 1
                if self.n == 1:
                    raise GitHubAPIError(422, "Validation Failed")
                return super().create_issue(repo, title, body, labels)

        orch, store, *_ = build()
        orch.github = orch.syncer.github = Flaky()
        orch.start_scan()
        finish_jobs(orch)
        res = store.list_jobs("scan")[0]["result"]
        self.assertEqual((len(res["filed"]), len(res["errors"])), (2, 1))


class LearnTests(unittest.TestCase):
    def test_nothing_new_does_not_start_a_session(self):
        orch, store, devin, *_ = build()
        self.assertEqual(orch.start_learn(), {"status": "nothing_new"})
        self.assertEqual(devin._n, 0)

    def test_learn_sends_feedback_and_existing_rules_then_labels_the_pr(self):
        orch, store, devin, gh, _ = build()
        orch.request_ready(REPO, 2, "[sim:blocked] Paramiko", "")
        for _ in range(6):
            orch.tick()
        orch.handle_comment(REPO, 2, "alice", "Always prefer config mitigation over waiting on upstream.", on_pr=False)
        res = orch.start_learn()
        self.assertEqual(res["status"], "started")
        prompt = devin.created_prompts[-1]
        self.assertIn("Always prefer config mitigation", prompt)
        self.assertIn("(state then: blocked)", prompt)
        self.assertIn("RULES", prompt)  # the current rulebook is included so Devin can check for duplicates
        self.assertIn(SOLUTION, prompt)
        finish_jobs(orch)
        job = store.get_job(res["job"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["rules_proposed"], 2)
        self.assertIn(("add_labels", {"repo": SOLUTION, "number": 7, "labels": ["devin:knowledge-base-improvement"]}), gh.calls)

    def test_each_run_only_sees_feedback_since_the_previous_run(self):
        import time

        orch, store, devin, _, clock = build()
        clock.t = time.time()  # production uses one real clock for both feedback and jobs
        orch.request_ready(REPO, 1, "x", "")
        orch.handle_comment(REPO, 1, "a", "first lesson", on_pr=False)
        orch.start_learn()
        finish_jobs(orch)
        self.assertEqual(orch.start_learn(), {"status": "nothing_new"})
        store.add_feedback(task(store, 1)["id"], "issue_comment", "a", "second lesson", "ready", now=int(clock.t) + 60)
        self.assertEqual(orch.start_learn()["status"], "started")
        self.assertIn("second lesson", devin.created_prompts[-1])
        self.assertNotIn("first lesson", devin.created_prompts[-1])

    def test_previously_rejected_proposals_are_passed_along(self):
        class WithRejected(RecordingGitHub):
            def list_closed_unmerged_prs(self, repo, label):
                return [{"title": "Rule: always squash", "body": "rejected by a human"}]

        orch, store, devin, *_ = build()
        orch.github = orch.syncer.github = WithRejected()
        orch.request_ready(REPO, 1, "x", "")
        orch.handle_comment(REPO, 1, "a", "some lesson", on_pr=False)
        orch.start_learn()
        self.assertIn("Rule: always squash", devin.created_prompts[-1])


if __name__ == "__main__":
    unittest.main()
