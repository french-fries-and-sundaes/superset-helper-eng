"""M3: reacting to GitHub events (closed issues, PR merged/closed, reviews, comments) and requeue guidance."""
import unittest

from app.states import State
from app.webhook import handle_event
from tests.helpers import (REPO, build, comment_event, issue_event, pr_event, review_event, run_ticks, state_of, task,
                           to_review)


def deliver(orch, event, payload, delivery=None):
    return handle_event(event, payload, delivery, orch, orch.settings)


class ClosedIssueTests(unittest.TestCase):
    def test_closed_not_planned_rejects_from_any_open_state(self):
        for prepare in ("ready", "in-progress", "blocked", "in-review"):
            orch, store, *_ = build()
            if prepare == "in-review":
                to_review(orch, store, 1)
            else:
                orch.request_ready(REPO, 1, "[sim:blocked] x" if prepare == "blocked" else "x", "")
                if prepare == "in-progress":
                    orch.dispatch_ready()
                elif prepare == "blocked":
                    run_ticks(orch)
            self.assertEqual(state_of(store, 1), prepare)
            r = deliver(orch, "issues", issue_event("closed", 1, reason="not_planned"))
            self.assertEqual(r["task"], "rejected", prepare)
            self.assertEqual(state_of(store, 1), "rejected")

    def test_rejecting_an_in_progress_task_terminates_its_session(self):
        orch, store, devin, *_ = build()
        orch.request_ready(REPO, 1, "x", "")
        orch.dispatch_ready()
        deliver(orch, "issues", issue_event("closed", 1, reason="not_planned"))
        self.assertEqual(len(devin.terminated), 1)

    def test_proposed_issue_closed_is_rejected(self):
        orch, store, *_ = build()
        orch.register_proposed(REPO, 4, "finding", "")
        deliver(orch, "issues", issue_event("closed", 4, reason="not_planned"))
        self.assertEqual(state_of(store, 4), "rejected")

    def test_closed_as_completed_completes_a_reviewed_task(self):
        orch, store, *_ = build()
        to_review(orch, store, 1)
        deliver(orch, "issues", issue_event("closed", 1, reason="completed"))
        self.assertEqual(state_of(store, 1), "completed")

    def test_unknown_issue_is_ignored(self):
        orch, *_ = build()
        self.assertEqual(deliver(orch, "issues", issue_event("closed", 99, reason="not_planned"))["task"], "ignored")


class PullRequestTests(unittest.TestCase):
    def test_pr_opened_links_via_closes_keyword(self):
        orch, store, *_ = build()
        orch.request_ready(REPO, 3, "x", "")
        orch.dispatch_ready()
        self.assertEqual(deliver(orch, "pull_request", pr_event("opened", 50, "This fixes #3 nicely"))["result"], "linked")
        self.assertEqual(store.prs_for_task(task(store, 3)["id"])[0]["pr_number"], 50)
        self.assertEqual(deliver(orch, "pull_request", pr_event("opened", 51, "no keyword here"))["result"], "ignored")

    def test_merge_completes_the_task(self):
        orch, store, *_ = build()
        to_review(orch, store, 1)
        r = deliver(orch, "pull_request", pr_event("closed", 101, merged=True))
        self.assertEqual(r["result"], "completed")
        self.assertEqual(state_of(store, 1), "completed")

    def test_close_without_merge_rejects_and_closes_the_issue(self):
        orch, store, _, gh, _ = build()
        to_review(orch, store, 1)
        r = deliver(orch, "pull_request", pr_event("closed", 101, merged=False))
        self.assertEqual(r["result"], "rejected")
        self.assertEqual(state_of(store, 1), "rejected")
        self.assertIn(("close_issue", {"repo": REPO, "number": 1, "reason": "not_planned"}), gh.calls)

    def test_close_without_merge_is_ignored_if_another_pr_is_open(self):
        orch, store, *_ = build()
        to_review(orch, store, 1)
        store.upsert_pr(task(store, 1)["id"], 202, "https://github.com/o/r/pull/202", "s", "open")
        self.assertEqual(deliver(orch, "pull_request", pr_event("closed", 101))["result"], "ignored")
        self.assertEqual(state_of(store, 1), "in-review")

    def test_reapplying_ready_after_rejection_restarts_and_reopens_the_issue(self):
        orch, store, _, gh, _ = build()
        to_review(orch, store, 1)
        deliver(orch, "pull_request", pr_event("closed", 101))
        self.assertEqual(deliver(orch, "issues", issue_event("labeled", 1, label="devin:ready"))["task"], "requeued")
        self.assertEqual(state_of(store, 1), "ready")
        self.assertIn(("reopen_issue", {"repo": REPO, "number": 1}), gh.calls)
        run_ticks(orch)
        self.assertEqual(task(store, 1)["attempt"], 2)


class ReviewTests(unittest.TestCase):
    def test_changes_requested_starts_a_revision_on_the_same_pr(self):
        orch, store, devin, *_ = build()
        to_review(orch, store, 1)
        r = deliver(orch, "pull_request_review", review_event(101, "changes_requested", "Please add a test."))
        self.assertEqual(r["result"], "changes_requested")
        self.assertEqual(state_of(store, 1), "ready")
        orch.dispatch_ready()
        prompt = devin.created_prompts[-1]
        self.assertIn("This is a REVISION", prompt)
        self.assertIn("Please add a test.", prompt)
        self.assertIn("https://github.com/o/r/pull/101", prompt)
        self.assertIn("Do NOT open a new pull request", prompt)
        self.assertEqual(task(store, 1)["attempt"], 2)

    def test_approval_and_comment_reviews_only_record_feedback(self):
        orch, store, *_ = build()
        to_review(orch, store, 1)
        self.assertEqual(deliver(orch, "pull_request_review", review_event(101, "approved", "LGTM"))["result"], "recorded")
        self.assertEqual(deliver(orch, "pull_request_review", review_event(101, "commented", "nit"))["result"], "recorded")
        self.assertEqual(state_of(store, 1), "in-review")
        self.assertEqual(len(store.feedback_since(0, task(store, 1)["id"])), 2)

    def test_changes_requested_outside_review_is_ignored(self):
        orch, store, *_ = build()
        orch.request_ready(REPO, 1, "x", "")
        orch.dispatch_ready()
        store.upsert_pr(task(store, 1)["id"], 101, "https://github.com/o/r/pull/101")
        self.assertEqual(deliver(orch, "pull_request_review", review_event(101, "changes_requested"))["result"], "ignored")


class CommentTests(unittest.TestCase):
    def test_human_comments_are_recorded_but_bot_comments_are_not(self):
        orch, store, *_ = build()
        orch.request_ready(REPO, 1, "x", "")
        self.assertEqual(deliver(orch, "issue_comment", comment_event(1, "Use the config option."))["status"], "ok")
        bot = deliver(orch, "issue_comment", comment_event(1, "Devin started.\n\n<!-- superset-helper-eng:bot -->"))
        self.assertEqual(bot["status"], "ignored")
        githubbot = deliver(orch, "issue_comment", comment_event(1, "hi", user_type="Bot"))
        self.assertEqual(githubbot["status"], "ignored")
        fb = store.feedback_since(0, task(store, 1)["id"])
        self.assertEqual([f["text"] for f in fb], ["Use the config option."])

    def test_pr_comments_map_to_the_task(self):
        orch, store, *_ = build()
        to_review(orch, store, 1)
        deliver(orch, "issue_comment", comment_event(101, "why this approach?", on_pr=True))
        fb = store.feedback_since(0, task(store, 1)["id"])
        self.assertEqual(fb[-1]["kind"], "pr_comment")

    def test_human_answer_flows_into_the_next_sessions_prompt(self):
        orch, store, devin, *_ = build()
        orch.request_ready(REPO, 2, "[sim:blocked] Paramiko", "old text")
        run_ticks(orch)
        self.assertEqual(state_of(store, 2), "blocked")
        deliver(orch, "issue_comment", comment_event(2, "Apply the config mitigation, do not wait for upstream."))
        deliver(orch, "issues", issue_event("labeled", 2, title="[sim:blocked] Paramiko", body="old text", label="devin:ready"))
        orch.dispatch_ready()
        prompt = devin.created_prompts[-1]
        self.assertIn("Apply the config mitigation", prompt)
        self.assertIn("Simulated question: which mitigation do you prefer?", prompt)
        self.assertIn("Attempt 1 ended as 'blocked'", prompt)


if __name__ == "__main__":
    unittest.main()
