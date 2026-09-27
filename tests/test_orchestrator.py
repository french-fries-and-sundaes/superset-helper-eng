import unittest

from app.config import Settings
from app.db import Store
from app.devin_client import DevinAPIError, FakeDevinClient
from app.orchestrator import Orchestrator
from app.prompts import WRAP_UP_MESSAGE
from app.states import State

REPO = "o/r"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def build(devin=None):
    store = Store(":memory:")
    devin = devin or FakeDevinClient(REPO)
    clock = Clock()
    settings = Settings(target_repo=REPO, verify_mode="off")
    return Orchestrator(store, devin, settings, now=clock, knowledge="RULES"), store, devin, clock


def state_of(store, issue):
    return store.get_task_by_issue(REPO, issue)["state"]


def run_ticks(orch, n=5):
    for _ in range(n):
        orch.tick()


class OrchestratorTests(unittest.TestCase):
    def test_happy_path_ends_in_review_with_pr_and_wraps_up(self):
        orch, store, devin, _ = build()
        self.assertEqual(orch.request_ready(REPO, 1, "Override underscore", "body"), "created")
        run_ticks(orch)
        task = store.get_task_by_issue(REPO, 1)
        self.assertEqual(task["state"], "in-review")
        self.assertEqual(task["pr_urls"], ["https://github.com/o/r/pull/101"])
        self.assertEqual(task["attempt"], 1)
        # Wrap-up message sent so the session exits.
        self.assertEqual(devin.messages_sent[0][1], WRAP_UP_MESSAGE)

    def test_blocked_path_records_question_and_terminates_session(self):
        orch, store, devin, _ = build()
        orch.request_ready(REPO, 2, "[sim:blocked] Paramiko", "")
        run_ticks(orch)
        task = store.get_task_by_issue(REPO, 2)
        self.assertEqual(task["state"], "blocked")
        self.assertIn("Simulated question", task["blocked_question"])
        self.assertEqual(len(devin.terminated), 1)

    def test_failed_path(self):
        orch, store, _, _ = build()
        orch.request_ready(REPO, 3, "[sim:fail] Too broad", "")
        run_ticks(orch)
        self.assertEqual(state_of(store, 3), "failed")

    def test_duplicate_ready_is_idempotent(self):
        orch, store, devin, _ = build()
        orch.request_ready(REPO, 1, "t", "")
        self.assertEqual(orch.request_ready(REPO, 1, "t", ""), "ignored")
        orch.tick()
        self.assertEqual(orch.request_ready(REPO, 1, "t", ""), "ignored")  # in progress now
        self.assertEqual(len(devin._sessions), 1)

    def test_human_reapplies_ready_after_blocked_starts_a_fresh_session(self):
        orch, store, devin, _ = build()
        orch.request_ready(REPO, 2, "[sim:blocked] Paramiko", "old body")
        run_ticks(orch)
        self.assertEqual(state_of(store, 2), "blocked")
        # The human edits the issue to answer the question and re-applies the label.
        self.assertEqual(orch.request_ready(REPO, 2, "[sim:blocked] Paramiko", "answer: use config mitigation"), "requeued")
        task = store.get_task_by_issue(REPO, 2)
        self.assertEqual((task["state"], task["body"], task["current_session_id"]), ("ready", "answer: use config mitigation", None))
        orch.dispatch_ready()
        self.assertEqual(store.get_task_by_issue(REPO, 2)["attempt"], 2)
        self.assertEqual(len(devin._sessions), 2)

    def test_proposed_then_approved(self):
        orch, store, _, _ = build()
        self.assertEqual(orch.register_proposed(REPO, 9, "Scanner finding", "x"), "created")
        self.assertEqual(state_of(store, 9), "proposed")
        orch.tick()
        self.assertEqual(state_of(store, 9), "proposed")  # nothing happens until a human approves
        self.assertEqual(orch.request_ready(REPO, 9, "Scanner finding", "x"), "requeued")
        run_ticks(orch)
        self.assertEqual(state_of(store, 9), "in-review")

    def test_retryable_start_failure_releases_the_task(self):
        class Flaky(FakeDevinClient):
            def create_session(self, *a, **k):
                raise DevinAPIError(429, "rate limited")

        orch, store, _, _ = build(devin=Flaky(REPO))
        orch.request_ready(REPO, 1, "t", "")
        orch.dispatch_ready()
        self.assertEqual(state_of(store, 1), "ready")  # will be retried next tick
        self.assertEqual(store.sessions_started_since(0), 0)  # and it did not count against the daily guard

    def test_permanent_start_failure_marks_failed(self):
        class Denied(FakeDevinClient):
            def create_session(self, *a, **k):
                raise DevinAPIError(403, "Unauthorized")

        orch, store, _, _ = build(devin=Denied(REPO))
        orch.request_ready(REPO, 1, "t", "")
        orch.dispatch_ready()
        self.assertEqual(state_of(store, 1), "failed")

    def test_prompt_contains_issue_rules_and_knowledge(self):
        captured = {}

        class Spy(FakeDevinClient):
            def create_session(self, prompt, title, tags=None, **kw):
                captured.update(prompt=prompt, tags=tags, title=title, schema=kw.get("structured_output_schema"))
                return super().create_session(prompt, title, tags, **kw)

        orch, _, _, _ = build(devin=Spy(REPO))
        orch.request_ready(REPO, 7, "Fix thing", "details here\n```verify\nnpm audit\n```")
        orch.dispatch_ready()
        p = captured["prompt"]
        for needle in ("Issue #7: Fix thing", "TRIAGE FIRST", "Closes #7", "RULES", "npm audit"):
            self.assertIn(needle, p)
        self.assertIn("issue-7", captured["tags"])
        self.assertEqual(captured["schema"]["properties"]["outcome"]["enum"], ["done", "blocked", "failed"])


if __name__ == "__main__":
    unittest.main()
