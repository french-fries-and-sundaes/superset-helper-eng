"""M4: dashboard, JSON API, auth, and the action buttons."""
import base64
import hashlib
import hmac
import json
import unittest

from starlette.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.devin_client import FakeDevinClient
from app.github_client import RecordingGitHub
from app.main import create_app
from tests.helpers import REPO, SOLUTION, comment_event, issue_event, pr_event, workflow_run

SECRET = "s3cret"


def make_client(password="", verify_mode="actions"):
    settings = Settings(
        github_webhook_secret=SECRET, target_repo=REPO, solution_repo=SOLUTION, run_worker=False,
        devin_mode="fake", verify_mode=verify_mode, dashboard_password=password,
    )
    app = create_app(settings, devin=FakeDevinClient(REPO, SOLUTION), store=Store(":memory:"), github=RecordingGitHub())
    return TestClient(app), app.state.orchestrator, app.state.store


def hook(client, event, payload, delivery):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook/github", content=body,
                       headers={"x-github-event": event, "x-github-delivery": delivery, "x-hub-signature-256": sig})


class DashboardTests(unittest.TestCase):
    def test_empty_dashboard_renders(self):
        c, *_ = make_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Fixes merged", r.text)
        self.assertIn("Nothing is waiting on a person.", r.text)
        self.assertIn("GitHub sync: dry-run", r.text)

    def test_shows_each_bucket_and_the_needs_a_human_reasons(self):
        c, orch, store = make_client(verify_mode="off")
        hook(c, "issues", issue_event("labeled", 1, "Override underscore", label="devin:ready"), "a")
        hook(c, "issues", issue_event("labeled", 2, "[sim:blocked] Paramiko", label="devin:ready"), "b")
        hook(c, "issues", issue_event("labeled", 3, "Scanner finding", label="devin:proposed"), "c")
        for _ in range(6):
            orch.tick()
        page = c.get("/").text
        self.assertIn("Simulated question: which mitigation do you prefer?", page)  # blocked reason
        self.assertIn("Approve by applying", page)  # proposed
        self.assertIn("Review the pull request", page)  # in review
        self.assertIn("/tasks/1", page)

    def test_verifying_bucket_is_visible_while_waiting_for_the_check(self):
        c, orch, store = make_client()
        hook(c, "issues", issue_event("labeled", 1, "Fix", label="devin:ready"), "a")
        for _ in range(6):
            orch.tick()
        self.assertIn("checking the PR", c.get("/").text)
        hook(c, "workflow_run", workflow_run("success", [101]), "w")
        self.assertIn("Review the pull request", c.get("/").text)

    def test_user_supplied_text_is_escaped(self):
        c, orch, store = make_client()
        hook(c, "issues", issue_event("labeled", 1, "<script>alert(1)</script>", body="<img src=x onerror=alert(2)>", label="devin:ready"), "a")
        for path in ("/", "/tasks/1"):
            page = c.get(path).text
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertNotIn("<img src=x onerror", page)
            self.assertIn("&lt;script&gt;", page)

    def test_task_page_shows_timeline_sessions_and_feedback(self):
        c, orch, store = make_client(verify_mode="off")
        hook(c, "issues", issue_event("labeled", 1, "Fix", label="devin:ready"), "a")
        hook(c, "issue_comment", comment_event(1, "please be careful"), "b")
        for _ in range(6):
            orch.tick()
        page = c.get("/tasks/1").text
        for needle in ("Sessions", "Timeline", "state_changed", "please be careful", "fake0001", "Pull requests and verification"):
            self.assertIn(needle, page)
        self.assertEqual(c.get("/tasks/999").status_code, 404)

    def test_windows_switch_the_completed_section(self):
        c, orch, store = make_client(verify_mode="off")
        for w in ("24h", "7d", "all", "bogus"):
            self.assertEqual(c.get(f"/?window={w}").status_code, 200)

    def test_flash_message_is_escaped(self):
        c, *_ = make_client()
        self.assertNotIn("<b>x</b>", c.get("/?msg=<b>x</b>").text)


class ApiTests(unittest.TestCase):
    def test_status_tasks_metrics_jobs(self):
        c, orch, store = make_client(verify_mode="off")
        hook(c, "issues", issue_event("labeled", 1, "Fix", label="devin:ready"), "a")
        for _ in range(6):
            orch.tick()
        s = c.get("/api/status").json()
        self.assertEqual(s["counts"]["in-review"], 1)
        self.assertEqual(s["devin_mode"], "fake")
        self.assertEqual([t["issue"] for t in s["needs_human"]], [1])
        m = c.get("/api/metrics").json()
        self.assertEqual(m["tasks_total"], 1)
        self.assertIsNotNone(m["median_work_to_review"])
        self.assertEqual(c.get("/api/jobs").json(), {"jobs": []})
        self.assertEqual(len(c.get("/api/tasks").json()["tasks"]), 1)

    def test_metrics_merge_rate_and_first_pass(self):
        c, orch, store = make_client(verify_mode="off")
        hook(c, "issues", issue_event("labeled", 1, "Fix", label="devin:ready"), "a")
        hook(c, "issues", issue_event("labeled", 2, "[sim:fail] Hard", label="devin:ready"), "b")
        for _ in range(6):
            orch.tick()
        hook(c, "pull_request", pr_event("closed", 101, merged=True), "m")
        m = c.get("/api/metrics").json()
        self.assertEqual(m["counts"]["completed"], 1)
        self.assertEqual(m["counts"]["failed"], 1)
        self.assertEqual(m["merge_rate_pct"], 50)
        self.assertEqual(m["failed_rate_pct"], 50)
        self.assertEqual(m["first_pass_rate_pct"], 100)  # the one task that reached review did so first try


class ActionTests(unittest.TestCase):
    def test_scan_button_starts_a_job_and_redirects_with_a_message(self):
        c, orch, store = make_client()
        r = c.post("/actions/scan", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("scan%20started", r.headers["location"])
        self.assertEqual(len(store.running_jobs()), 1)
        again = c.post("/api/scan").json()
        self.assertEqual(again["status"], "already_running")

    def test_learn_button_reports_when_there_is_nothing_new(self):
        c, *_ = make_client()
        self.assertEqual(c.post("/api/learn").json(), {"status": "nothing_new"})
        r = c.post("/actions/learn", follow_redirects=False)
        self.assertIn("Nothing%20new", r.headers["location"])

    def test_cross_origin_posts_are_refused(self):
        c, *_ = make_client()
        r = c.post("/actions/scan", headers={"origin": "https://evil.example"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)

    def test_get_on_an_action_route_is_not_allowed(self):
        c, *_ = make_client()
        self.assertEqual(c.get("/actions/scan").status_code, 405)


class AuthTests(unittest.TestCase):
    def test_password_protects_everything_except_webhook_and_health(self):
        c, *_ = make_client(password="hunter2")
        self.assertEqual(c.get("/healthz").status_code, 200)
        for path in ("/", "/api/status", "/api/metrics", "/tasks/1"):
            r = c.get(path)
            self.assertEqual(r.status_code, 401, path)
            self.assertIn("Basic", r.headers["www-authenticate"])
        self.assertEqual(c.post("/actions/scan").status_code, 401)  # spend-triggering button is protected too
        self.assertEqual(c.post("/api/scan").status_code, 401)

    def test_correct_and_wrong_credentials(self):
        c, *_ = make_client(password="hunter2")
        good = {"authorization": "Basic " + base64.b64encode(b"anyone:hunter2").decode()}
        bad = {"authorization": "Basic " + base64.b64encode(b"anyone:wrong").decode()}
        self.assertEqual(c.get("/", headers=good).status_code, 200)
        self.assertEqual(c.get("/", headers=bad).status_code, 401)
        self.assertEqual(c.get("/", headers={"authorization": "Basic !!!notbase64"}).status_code, 401)

    def test_webhook_still_uses_its_signature_not_the_password(self):
        c, *_ = make_client(password="hunter2")
        self.assertEqual(hook(c, "ping", {}, "p").json(), {"status": "pong"})


if __name__ == "__main__":
    unittest.main()


class NeedsAHumanOrderTests(unittest.TestCase):
    def test_review_first_then_blocked_then_failed_then_proposed_and_oldest_first_within_a_group(self):
        from app.db import Store
        from app.states import State
        from app.dashboard import render_dashboard
        from tests.helpers import build, REPO

        orch, store, *_ = build()
        # created in the worst possible order, with the oldest waiting longest
        specs = [(1, State.PROPOSED, 100), (2, State.FAILED, 200), (3, State.BLOCKED, 300),
                 (4, State.IN_REVIEW, 500), (5, State.IN_REVIEW, 400), (6, State.BLOCKED, 250)]
        for n, state, ts in specs:
            store.create_task(REPO, n, f"task-{n}", "", state, now=ts)
        page = render_dashboard(orch)
        section = page[page.index("Needs a human</h2>"):page.index("In flight</h2>")]
        order = [int(x) for x in __import__("re").findall(r"task-(\d)", section)]
        # 5 (older review) before 4, then blocked 6 (older) before 3, then failed 2, then proposed 1
        self.assertEqual(order, [5, 4, 6, 3, 2, 1])
