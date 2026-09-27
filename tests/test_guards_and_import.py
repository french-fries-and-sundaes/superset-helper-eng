"""Startup safety checks, deleted-issue handling, GitHub import, and the dashboard refresh/sync controls."""
import os
import tempfile
import unittest

from starlette.testclient import TestClient

from app.config import ConfigError, Settings
from app.db import Store
from app.devin_client import FakeDevinClient
from app.github_client import GitHubAPIError, RecordingGitHub
from app.guards import check_safety
from app.main import create_app
from app.states import State
from tests.helpers import REPO, SOLUTION, build, run_ticks, state_of, task


class GuardTests(unittest.TestCase):
    def test_a_database_with_tasks_and_no_recorded_mode_is_refused_in_real_mode(self):
        # This is exactly the reported accident: an older database full of demo tasks, now run for real.
        store = Store(":memory:")
        store.create_task(REPO, 3, "[sim:fail] demo", "", State.FAILED)
        with self.assertRaises(ConfigError) as cm:
            check_safety(store, Settings(devin_mode="real", github_token="ghp_x"))
        self.assertIn("rm -rf data", str(cm.exception))
        self.assertIn("collide with real issues", str(cm.exception))

    def test_the_same_old_database_is_fine_in_fake_mode_and_records_the_mode(self):
        store = Store(":memory:")
        store.create_task(REPO, 3, "demo", "", State.FAILED)
        check_safety(store, Settings(devin_mode="fake"))
        self.assertEqual(store.get_meta("devin_mode"), "fake")

    def test_a_fresh_database_records_its_mode_and_then_refuses_the_other(self):
        store = Store(":memory:")
        check_safety(store, Settings(devin_mode="real"))
        self.assertEqual(store.get_meta("devin_mode"), "real")
        check_safety(store, Settings(devin_mode="real"))  # same mode again: fine
        with self.assertRaises(ConfigError) as cm:
            check_safety(store, Settings(devin_mode="fake"))
        self.assertIn("created with DEVIN_MODE=real", str(cm.exception))

    def test_a_fake_database_is_refused_in_real_mode_even_when_it_is_empty_of_tasks(self):
        store = Store(":memory:")
        check_safety(store, Settings(devin_mode="fake"))
        with self.assertRaises(ConfigError):
            check_safety(store, Settings(devin_mode="real"))

    def test_fake_devin_with_a_real_github_token_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            check_safety(Store(":memory:"), Settings(devin_mode="fake", github_token="ghp_x"))
        self.assertIn("simulated tasks", str(cm.exception))

    def test_the_escape_hatch(self):
        store = Store(":memory:")
        store.create_task(REPO, 3, "demo", "", State.FAILED)
        check_safety(store, Settings(devin_mode="real", allow_mode_change=True))
        self.assertEqual(store.get_meta("devin_mode"), "real")

    def test_persisted_database_file_keeps_the_recorded_mode_across_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tasks.db")
            check_safety(Store(path), Settings(devin_mode="fake"))
            with self.assertRaises(ConfigError):
                check_safety(Store(path), Settings(devin_mode="real"))

    def test_the_app_refuses_to_start_on_a_mismatched_database(self):
        store = Store(":memory:")
        store.create_task(REPO, 3, "demo", "", State.FAILED)
        with self.assertRaises(ConfigError):
            create_app(Settings(devin_mode="real", devin_api_key="k", devin_org_id="o", run_worker=False),
                       devin=object(), store=store, github=RecordingGitHub())


class DeletedIssueTests(unittest.TestCase):
    def make(self, status):
        class Gone(RecordingGitHub):
            def set_status_label(self, repo, number, target):
                raise GitHubAPIError(status, "This issue was deleted" if status == 410 else "Not Found")

        orch, store, *_ = build()
        orch.github = orch.syncer.github = Gone()
        orch.register_proposed(REPO, 3, "x", "")  # stays in one state, like a real stuck task
        return orch, store

    def test_410_is_logged_once_and_never_retried(self):
        orch, store = self.make(410)
        for _ in range(4):
            orch.tick()
        events = [e["kind"] for e in store.list_events(task(store, 3)["id"])]
        self.assertEqual(events.count("github_issue_missing"), 1)
        self.assertNotIn("github_sync_failed", events)
        self.assertEqual(task(store, 3)["synced_state"], task(store, 3)["state"])  # gave up: no more attempts

    def test_404_keeps_retrying_but_logs_once(self):
        orch, store = self.make(404)
        for _ in range(4):
            orch.tick()
        events = [e["kind"] for e in store.list_events(task(store, 3)["id"])]
        self.assertEqual(events.count("github_sync_failed"), 1)  # no spam
        self.assertEqual(task(store, 3)["synced_state"], "")  # still trying: could be a permissions problem


class ImportTests(unittest.TestCase):
    def gh(self, proposed=(), ready=()):
        class Listing(RecordingGitHub):
            dry_run = False

            def list_issues(self, repo, label, state="open"):
                return {"devin:proposed": list(proposed), "devin:ready": list(ready)}.get(label, [])

        return Listing()

    def issue(self, n, title="t", body="b"):
        return {"number": n, "title": title, "body": body}

    def test_imports_proposed_and_ready_issues_the_bot_never_saw(self):
        orch, store, *_ = build()
        orch.github = orch.syncer.github = self.gh(proposed=[self.issue(4), self.issue(5)], ready=[self.issue(8, "retries")])
        out = orch.import_from_github()
        self.assertEqual((out["status"], out["proposed"], out["ready"], out["seen"]), ("ok", 2, 1, 3))
        self.assertEqual((state_of(store, 4), state_of(store, 5), state_of(store, 8)), ("proposed", "proposed", "ready"))
        self.assertEqual(task(store, 4)["source"], "import")
        run_ticks(orch)
        self.assertEqual(state_of(store, 8), "in-review")  # an imported ready task runs like any other
        self.assertEqual(state_of(store, 4), "proposed")  # proposals wait for a human

    def test_running_it_twice_changes_nothing(self):
        orch, store, *_ = build()
        orch.github = orch.syncer.github = self.gh(proposed=[self.issue(4)])
        orch.import_from_github()
        again = orch.import_from_github()
        self.assertEqual((again["proposed"], again["ready"]), (0, 0))
        self.assertEqual(len(store.list_tasks()), 1)

    def test_an_issue_with_both_labels_ends_up_ready(self):
        orch, store, *_ = build()
        orch.github = orch.syncer.github = self.gh(proposed=[self.issue(4)], ready=[self.issue(4)])
        orch.import_from_github()
        self.assertEqual(state_of(store, 4), "ready")

    def test_dry_run_reads_nothing(self):
        orch, *_ = build()  # RecordingGitHub: dry run
        self.assertEqual(orch.import_from_github()["status"], "dry_run")

    def test_a_github_error_is_reported_not_raised(self):
        class Broken(RecordingGitHub):
            dry_run = False

            def list_issues(self, repo, label, state="open"):
                raise GitHubAPIError(403, "Resource not accessible")

        orch, *_ = build()
        orch.github = orch.syncer.github = Broken()
        out = orch.import_from_github()
        self.assertEqual(out["status"], "error")
        self.assertIn("Resource not accessible", out["errors"][0])


class DashboardControlTests(unittest.TestCase):
    def client(self):
        s = Settings(github_webhook_secret="s", target_repo=REPO, solution_repo=SOLUTION, run_worker=False,
                     devin_mode="fake", verify_mode="off")
        app = create_app(s, devin=FakeDevinClient(REPO, SOLUTION), store=Store(":memory:"), github=RecordingGitHub())
        return TestClient(app)

    def test_page_shows_when_it_was_updated_and_a_refresh_link(self):
        page = self.client().get("/").text
        self.assertNotIn("Refresh now", page)
        self.assertIn("refreshes itself every 10 seconds", page)
        self.assertIn('<details class="fold" id="fold-working">', page)
        self.assertIn('<details class="fold" id="fold-activity">', page)
        self.assertNotIn("<details class=\"fold\" id=\"fold-working\" open", page)
        self.assertIn('<details class="fold" id="fold-sync">', page)
        self.assertGreater(page.index("Sync now"), page.index("Recent activity"))  # tucked away at the bottom
        self.assertLess(page.index(">SCAN<"), page.index("Needs a human</h2>"))
        self.assertRegex(page, r"Updated \d\d:\d\d:\d\d UTC")

    def test_sync_button_explains_dry_run(self):
        c = self.client()
        self.assertIn("Sync now", c.get("/").text)
        self.assertEqual(c.post("/api/sync").json()["status"], "dry_run")
        r = c.post("/actions/sync", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("dry-run", r.headers["location"])


if __name__ == "__main__":
    unittest.main()


class FlashMessageTests(unittest.TestCase):
    def test_the_one_time_message_is_shown_but_the_automatic_reload_drops_it(self):
        from app.dashboard import render_dashboard

        orch, *_ = build()
        page = render_dashboard(orch, "7d", "scan started (session x). Results appear here when Devin finishes.")
        self.assertIn("scan started", page)
        self.assertIn('<meta http-equiv="refresh" content="10;url=/?window=7d">', page)
        self.assertNotIn("scan started", render_dashboard(orch, "7d"))


class RecoverMergedFixesTests(unittest.TestCase):
    def gh(self, prs, issues=None):
        issues = issues or {}

        class Listing(RecordingGitHub):
            dry_run = False

            def list_merged_prs_from_branch(self, repo, prefix, max_pages=5):
                assert prefix == "devin/"
                return list(prs)

            def get_issue(self, repo, number):
                return issues.get(number, {"number": number, "title": f"issue {number}", "body": "b"})

        return Listing()

    def pr(self, number, closes, merged_at=1_000_000):
        return {"number": number, "html_url": f"https://github.com/o/r/pull/{number}", "title": f"PR {number}",
                "body": f"Closes #{closes}", "head_sha": "abc", "merged_at": merged_at}

    def wire(self, orch, gh):
        orch.github = orch.syncer.github = gh

    def test_a_merged_devin_pr_becomes_a_completed_task(self):
        orch, store, *_ = build()
        self.wire(orch, self.gh([self.pr(11, 5)], {5: {"title": "Real title", "body": "Real body"}}))
        out = orch.import_from_github()
        self.assertEqual(out["recovered"], 1)
        t = task(store, 5)
        self.assertEqual((t["state"], t["source"], t["title"]), ("completed", "import", "Real title"))
        self.assertEqual(store.prs_for_task(t["id"])[0]["state"], "merged")

    def test_recovered_fixes_count_as_merged_in_the_metrics_and_the_window(self):
        from app.metrics import compute_metrics

        orch, store, _, _, clock = build()
        self.wire(orch, self.gh([self.pr(11, 5, merged_at=int(clock.t) - 60)]))
        orch.import_from_github()
        m = compute_metrics(store, now=clock.t, completed_window=24 * 3600)
        self.assertEqual((m["outcomes"]["merged"], m["completed_in_window"]), (1, 1))

    def test_it_is_idempotent_and_leaves_known_tasks_alone(self):
        orch, store, *_ = build()
        orch.request_ready(REPO, 5, "mine", "b")  # the bot already tracks #5
        self.wire(orch, self.gh([self.pr(11, 5), self.pr(12, 6)]))
        self.assertEqual(orch.import_from_github()["recovered"], 1)
        self.assertEqual(orch.import_from_github()["recovered"], 0)
        self.assertEqual(task(store, 5)["title"], "mine")
        self.assertEqual(state_of(store, 6), "completed")

    def test_it_never_posts_labels_or_comments_for_recovered_tasks(self):
        orch, store, *_ = build()
        listing = self.gh([self.pr(11, 5)])
        self.wire(orch, listing)
        orch.import_from_github()
        self.assertEqual([c for c in listing.calls if c[0] in ("comment", "set_status_label")], [])

    def test_a_pr_that_closes_several_issues_recovers_each(self):
        orch, store, *_ = build()
        p = self.pr(11, 5)
        p["body"] = "Closes #5 and fixes #6"
        self.wire(orch, self.gh([p]))
        self.assertEqual(orch.import_from_github()["recovered"], 2)

    def test_a_pr_without_a_closes_line_is_ignored(self):
        orch, store, *_ = build()
        p = self.pr(11, 5)
        p["body"] = "Just a change"
        self.wire(orch, self.gh([p]))
        self.assertEqual(orch.import_from_github()["recovered"], 0)
        self.assertEqual(store.count_tasks(), 0)

    def test_a_github_error_is_reported_without_losing_the_rest_of_the_sync(self):
        orch, store, *_ = build()

        class Broken(RecordingGitHub):
            dry_run = False

            def list_issues(self, repo, label, state="open"):
                return [{"number": 8, "title": "t", "body": "b"}] if label == "devin:ready" else []

            def list_merged_prs_from_branch(self, repo, prefix, max_pages=5):
                raise GitHubAPIError(500, "boom")

        self.wire(orch, Broken())
        out = orch.import_from_github()
        self.assertEqual(out["ready"], 1)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(any("merged pull requests" in e for e in out["errors"]))
