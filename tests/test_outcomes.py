"""Being right is never penalized: closing an issue after Devin's pushback is 'not needed', not a rejection
and not a merged fix. Also covers the honest metrics and the dashboard's treatment of each outcome."""
import unittest

from starlette.testclient import TestClient

from app.config import Settings
from app.db import InvalidTransition, Store
from app.devin_client import FakeDevinClient
from app.github_client import RecordingGitHub
from app.main import create_app
from app.metrics import compute_metrics
from app.states import ALLOWED, STATUS_LABELS, State
from app.webhook import handle_event
from tests.helpers import (REPO, SOLUTION, build, comment_event, issue_event, pr_event, run_ticks, state_of, task,
                           to_review)


def deliver(orch, event, payload):
    return handle_event(event, payload, None, orch, orch.settings)


def blocked_task(orch, store, issue=2, title="[sim:blocked] Already fixed"):
    orch.request_ready(REPO, issue, title, "")
    run_ticks(orch)
    assert state_of(store, issue) == "blocked"


class NotNeededTests(unittest.TestCase):
    def test_closing_a_blocked_issue_is_not_needed_whatever_the_close_reason(self):
        for reason in ("completed", "not_planned", "duplicate", None):
            orch, store, *_ = build()
            blocked_task(orch, store)
            r = deliver(orch, "issues", issue_event("closed", 2, reason=reason))
            self.assertEqual(r["task"], "not_needed", reason)
            self.assertEqual(state_of(store, 2), "not-needed", reason)

    def test_it_is_neither_a_merged_fix_nor_a_rejection(self):
        orch, store, *_ = build()
        blocked_task(orch, store)
        deliver(orch, "issues", issue_event("closed", 2, reason="completed"))  # the default button
        counts = store.counts_by_state()
        self.assertEqual((counts["completed"], counts["rejected"], counts["not-needed"]), (0, 0, 1))
        m = compute_metrics(store)
        self.assertEqual(m["completed_in_window"], 0)  # fixes merged stays 0
        self.assertEqual(m["not_needed_in_window"], 1)
        self.assertEqual(m["outcomes"]["not_needed"], 1)
        self.assertIsNone(m["merge_rate_pct"])  # nothing attempted: no rate to penalize

    def test_it_has_no_github_label_and_no_comment_of_its_own(self):
        orch, store, _, gh, _ = build()
        blocked_task(orch, store)
        self.assertEqual(gh.get_labels(REPO, 2), ["devin:blocked"])
        n_comments = len([c for c in gh.calls if c[0] == "comment"])
        deliver(orch, "issues", issue_event("closed", 2, reason="completed"))
        self.assertEqual(gh.get_labels(REPO, 2), [])  # status labels cleared, like completed
        self.assertEqual(len([c for c in gh.calls if c[0] == "comment"]), n_comments)

    def test_a_human_can_change_their_mind_reopen_and_reapply_ready(self):
        orch, store, _, gh, _ = build()
        blocked_task(orch, store)
        deliver(orch, "issues", issue_event("closed", 2, reason="completed"))
        r = deliver(orch, "issues", issue_event("labeled", 2, title="Already fixed", body="really not", label="devin:ready"))
        self.assertEqual(r["task"], "requeued")
        self.assertIn(("reopen_issue", {"repo": REPO, "number": 2}), gh.calls)
        run_ticks(orch)
        self.assertEqual(task(store, 2)["attempt"], 2)

    def test_state_machine_and_labels(self):
        self.assertIsNone(State.NOT_NEEDED.label)
        self.assertNotIn("devin:not-needed", STATUS_LABELS)
        self.assertEqual(ALLOWED[State.NOT_NEEDED], {State.READY})
        self.assertIn(State.NOT_NEEDED, ALLOWED[State.BLOCKED])
        store = Store(":memory:")
        t = store.create_task(REPO, 1, "t", "", State.READY)
        with self.assertRaises(InvalidTransition):
            store.transition(t["id"], State.NOT_NEEDED)  # only a blocked task can end this way

    def test_closing_after_other_states_keeps_the_old_meaning(self):
        # failed -> closed: still a failure. proposed -> closed: a rejected proposal.
        orch, store, *_ = build()
        orch.request_ready(REPO, 3, "[sim:fail] hard", "")
        run_ticks(orch)
        deliver(orch, "issues", issue_event("closed", 3, reason="completed"))  # default button on a failed task
        orch.register_proposed(REPO, 4, "finding", "")
        deliver(orch, "issues", issue_event("closed", 4, reason="completed"))
        self.assertEqual((state_of(store, 3), state_of(store, 4)), ("rejected", "rejected"))


class MetricTests(unittest.TestCase):
    def scenario(self):
        orch, store, *_ = build()
        to_review(orch, store, 1, "merged fix")
        deliver(orch, "pull_request", pr_event("closed", 101, merged=True))            # merged
        blocked_task(orch, store, 2)
        deliver(orch, "issues", issue_event("closed", 2, reason="completed"))           # not needed
        to_review(orch, store, 3, "declined fix")
        deliver(orch, "pull_request", pr_event("closed", 103, body="Closes #3"))        # rejected fix
        orch.request_ready(REPO, 4, "[sim:fail] hard", "")
        run_ticks(orch)
        deliver(orch, "issues", issue_event("closed", 4, reason="not_planned"))         # failed, then given up on
        orch.register_proposed(REPO, 5, "unwanted", "", source="scan")
        deliver(orch, "issues", issue_event("closed", 5, reason="not_planned"))         # proposal rejected
        orch.register_proposed(REPO, 6, "wanted", "", source="scan")
        orch.request_ready(REPO, 6, "wanted", "")                                       # proposal approved
        run_ticks(orch)
        orch.register_proposed(REPO, 7, "pending", "", source="scan")                   # proposal pending
        blocked_task(orch, store, 8, "[sim:blocked] needs an answer")                   # pushback still waiting
        return orch, store

    def test_each_task_lands_in_exactly_one_outcome(self):
        _, store = self.scenario()
        o = compute_metrics(store)["outcomes"]
        self.assertEqual(o, {"merged": 1, "not_needed": 1, "rejected_fix": 1, "proposal_rejected": 1, "failed": 1})

    def test_merge_rate_ignores_no_change_outcomes(self):
        _, store = self.scenario()
        m = compute_metrics(store)
        # attempted fixes: 1 merged + 1 rejected fix + 1 failed
        self.assertEqual(m["merge_rate_pct"], 33)

    def test_pushbacks_are_reported_by_how_they_ended(self):
        _, store = self.scenario()
        pb = compute_metrics(store)["pushbacks"]
        self.assertEqual((pb["total"], pb["closed_not_needed"], pb["waiting"], pb["answered_and_continued"]), (2, 1, 1, 0))

    def test_an_answered_pushback_that_goes_on_to_merge_counts_as_continued(self):
        orch, store, *_ = build()
        blocked_task(orch, store, 2, "[sim:blocked] unclear")
        deliver(orch, "issue_comment", comment_event(2, "Do X"))
        deliver(orch, "issues", issue_event("labeled", 2, title="clear now", body="", label="devin:ready"))
        run_ticks(orch)
        deliver(orch, "pull_request", pr_event("closed", 102, body="Closes #2", merged=True))
        pb = compute_metrics(store)["pushbacks"]
        self.assertEqual((pb["total"], pb["answered_and_continued"], pb["closed_not_needed"]), (1, 1, 0))

    def test_a_pushback_that_was_answered_counts_as_continued_even_while_the_new_session_is_running(self):
        orch, store, *_ = build()
        blocked_task(orch, store, 2, "[sim:blocked] unclear")
        deliver(orch, "issues", issue_event("labeled", 2, title="clear now", body="", label="devin:ready"))
        orch.dispatch_ready()  # the fresh session has just started; nothing has reached review
        pb = compute_metrics(store)["pushbacks"]
        self.assertEqual((pb["total"], pb["answered_and_continued"], pb["waiting"]), (1, 1, 0))

    def test_scanner_proposal_approval_rate(self):
        _, store = self.scenario()
        p = compute_metrics(store)["proposals"]
        self.assertEqual((p["filed"], p["approved"], p["rejected"], p["pending"], p["approval_rate_pct"]), (3, 1, 1, 1, 50))


class DashboardOutcomeTests(unittest.TestCase):
    def test_hero_counts_only_merged_fixes_and_caught_before_coding_is_shown_separately(self):
        s = Settings(github_webhook_secret="s", target_repo=REPO, solution_repo=SOLUTION, run_worker=False,
                     devin_mode="fake", verify_mode="off")
        app = create_app(s, devin=FakeDevinClient(REPO, SOLUTION), store=Store(":memory:"), github=RecordingGitHub())
        orch, c = app.state.orchestrator, TestClient(app)
        blocked_task(orch, app.state.store, 2, "[sim:blocked] Override underscore (already fixed)")
        deliver(orch, "issues", issue_event("closed", 2, reason="completed"))
        page = c.get("/").text
        self.assertRegex(page, r'Fixes merged \(last 7 days\)</div><div class="num">0</div>')
        self.assertIn("caught before coding (no fix needed)", page)
        self.assertIn("Caught before coding (last 7 days)", page)
        self.assertIn("not-needed", page)
        self.assertNotIn("Nothing yet: these are issues Devin flagged", page)  # the section lists the task
        self.assertIn("Pushbacks at triage", page)
        self.assertIn("Session Link", page)


if __name__ == "__main__":
    unittest.main()
