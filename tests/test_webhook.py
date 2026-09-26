import hashlib
import hmac
import json
import unittest

from starlette.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.devin_client import FakeDevinClient
from app.main import create_app
from app.webhook import verify_signature

REPO = "o/r"
SECRET = "s3cret"


def sign(body: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_payload(action="labeled", number=1, label="devin:ready", labels=None, repo=REPO, pr=False):
    issue = {"number": number, "title": "Override underscore", "body": "b", "labels": [{"name": n} for n in (labels or [label])]}
    if pr:
        issue["pull_request"] = {"url": "x"}
    payload = {"action": action, "issue": issue, "repository": {"full_name": repo}}
    if action == "labeled":
        payload["label"] = {"name": label}
    return payload


class SignatureTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        body = b'{"a":1}'
        self.assertTrue(verify_signature(SECRET, body, sign(body)))
        self.assertFalse(verify_signature(SECRET, body, sign(body, "other")))
        self.assertFalse(verify_signature(SECRET, body, None))
        self.assertFalse(verify_signature(SECRET, body, "sha1=abc"))
        self.assertFalse(verify_signature("", body, sign(body, "")))  # no secret configured


class AppTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        settings = Settings(github_webhook_secret=SECRET, target_repo=REPO, run_worker=False, devin_mode="fake")
        self.app = create_app(settings, devin=FakeDevinClient(REPO), store=self.store)
        self.client = TestClient(self.app)

    def post(self, payload, event="issues", delivery="d-1", secret=SECRET):
        body = json.dumps(payload).encode()
        return self.client.post(
            "/webhook/github",
            content=body,
            headers={"x-github-event": event, "x-github-delivery": delivery, "x-hub-signature-256": sign(body, secret)},
        )

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})

    def test_bad_signature_is_rejected(self):
        r = self.post(issue_payload(), secret="wrong")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store.list_tasks(), [])

    def test_ready_label_creates_a_ready_task(self):
        r = self.post(issue_payload())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok", "task": "created", "issue": 1})
        self.assertEqual(self.store.get_task_by_issue(REPO, 1)["state"], "ready")

    def test_same_delivery_twice_is_a_duplicate(self):
        self.post(issue_payload(), delivery="same")
        r = self.post(issue_payload(), delivery="same")
        self.assertEqual(r.json(), {"status": "duplicate_delivery"})
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_opened_and_labeled_for_one_issue_make_one_task(self):
        # Creating an issue with the label already on it can emit both events.
        self.post(issue_payload(action="opened", labels=["devin:ready"]), delivery="a")
        r = self.post(issue_payload(action="labeled"), delivery="b")
        self.assertEqual(r.json()["task"], "ignored")
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_proposed_label_registers_a_proposal(self):
        self.post(issue_payload(label="devin:proposed"))
        self.assertEqual(self.store.get_task_by_issue(REPO, 1)["state"], "proposed")

    def test_unrelated_label_wrong_repo_and_prs_are_ignored(self):
        self.assertEqual(self.post(issue_payload(label="bug"), delivery="1").json()["status"], "ignored")
        self.assertEqual(self.post(issue_payload(repo="x/y"), delivery="2").json()["status"], "ignored")
        self.assertEqual(self.post(issue_payload(pr=True), delivery="3").json()["status"], "ignored")
        self.assertEqual(self.store.list_tasks(), [])

    def test_ping_and_other_events(self):
        self.assertEqual(self.post({}, event="ping").json(), {"status": "pong"})
        self.assertEqual(self.post({}, event="push", delivery="p").json()["status"], "ignored")

    def test_no_secret_configured_refuses(self):
        settings = Settings(github_webhook_secret="", target_repo=REPO, run_worker=False, devin_mode="fake")
        c = TestClient(create_app(settings, devin=FakeDevinClient(REPO), store=Store(":memory:")))
        self.assertEqual(c.post("/webhook/github", content=b"{}").status_code, 503)

    def test_status_and_tasks_endpoints(self):
        self.post(issue_payload(number=5))
        self.post(issue_payload(number=6, label="devin:proposed"), delivery="d-2")
        status = self.client.get("/api/status").json()
        self.assertEqual(status["counts"]["ready"], 1)
        self.assertEqual([t["issue"] for t in status["needs_human"]], [6])  # proposed needs a human
        self.assertEqual(len(self.client.get("/api/tasks?state=ready").json()["tasks"]), 1)
        self.assertEqual(self.client.get("/api/tasks?state=nope").status_code, 400)

    def test_end_to_end_through_the_orchestrator(self):
        self.post(issue_payload(number=8))
        orch = self.app.state.orchestrator
        for _ in range(5):
            orch.tick()
        task = self.client.get("/api/tasks?state=in-review").json()["tasks"]
        self.assertEqual([t["issue"] for t in task], [8])
        self.assertTrue(task[0]["pr_urls"])


if __name__ == "__main__":
    unittest.main()
