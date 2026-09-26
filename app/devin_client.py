"""Thin client for the Devin v3 organization API, plus a free simulator.

Behaviours below were verified against a real org (see project notes):
  * Session ids: responses return bare hex; API paths use the `devin-` prefix.
  * `POST .../messages` returns a session snapshot whose status_detail and
    structured_output can be null. Always re-GET the session for state.
  * `DELETE .../sessions/{id}` on an already-exited session returns 400
    "already exited"; we treat that as success.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from .config import ConfigError, Settings

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {"exit", "error", "suspended"}


class DevinAPIError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Devin API error {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


def api_session_id(session_id: str) -> str:
    """Path form of a session id (`devin-` prefix)."""
    return session_id if session_id.startswith("devin-") else f"devin-{session_id}"


def bare_session_id(session_id: str) -> str:
    return session_id[len("devin-"):] if session_id.startswith("devin-") else session_id


class DevinClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        base_url: str = "https://api.devin.ai/v3",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/organizations/{org_id}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries
        self._sleep = sleep

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(1, self._max_retries + 1):
            resp = self._http.request(method, path.lstrip("/"), **kwargs)
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self._max_retries:
                log.warning("Devin API %s on %s %s; retry %d", resp.status_code, method, path, attempt)
                self._sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise DevinAPIError(resp.status_code, _detail(resp))
            return resp.json() if resp.content else {}
        raise AssertionError("unreachable")

    # ---- sessions ---------------------------------------------------------------
    def create_session(
        self,
        prompt: str,
        title: str,
        tags: list[str] | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        max_acu_limit: int | None = None,
        playbook_id: str | None = None,
        repos: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": prompt, "title": title}
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_required"] = True
            body["structured_output_schema"] = structured_output_schema
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if playbook_id:
            body["playbook_id"] = playbook_id
        if repos:  # format not yet verified against the real API; unused by default
            body["repos"] = repos
        return self._request("POST", "sessions", json=body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"sessions/{api_session_id(session_id)}")

    def list_messages(self, session_id: str, max_pages: int = 10) -> list[dict[str, Any]]:
        """All messages, following the cursor. The cursor query parameter name
        (`after`) is an assumption; the response fields end_cursor/has_next_page are verified."""
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        for _ in range(max_pages):
            page = self._request("GET", f"sessions/{api_session_id(session_id)}/messages", params=params)
            items.extend(page.get("items", []))
            if not page.get("has_next_page") or not page.get("end_cursor"):
                break
            params = {"after": page["end_cursor"]}
        return items

    def send_message(self, session_id: str, message: str) -> dict[str, Any]:
        return self._request(
            "POST", f"sessions/{api_session_id(session_id)}/messages", json={"message": message}
        )

    def terminate(self, session_id: str) -> dict[str, Any]:
        try:
            return self._request("DELETE", f"sessions/{api_session_id(session_id)}")
        except DevinAPIError as e:
            if e.status == 400 and "already exited" in e.detail.lower():
                return {"already_exited": True}
            raise


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("detail") or data)
    except Exception:
        return resp.text[:300]


class FakeDevinClient:
    """Free, deterministic stand-in so reviewers can run the whole workflow without
    spending anything. Behaviour is chosen by a marker in the session title:

        [sim:blocked]  -> Devin asks a question (task becomes devin:blocked)
        [sim:fail]     -> the session ends as failed (devin:failed)
        [scan]         -> a sweep that returns three sample findings (and one skipped)
        [learn]        -> a knowledge update that proposes two rules (opens a PR)
        (no marker)    -> Devin opens a PR (verification, then devin:in-review)

    Each session reports `working` for its first two polls, mirroring the real API's
    shapes: status running + status_detail waiting_for_user with a structured outcome,
    and status exit after a wrap-up message. Fix PRs are numbered issue+100.
    """

    def __init__(self, repo: str = "owner/repo", solution_repo: str = "owner/solution") -> None:
        self.repo = repo
        self.solution_repo = solution_repo
        self._sessions: dict[str, dict[str, Any]] = {}
        self._n = 0
        self.messages_sent: list[tuple[str, str]] = []
        self.terminated: list[str] = []
        self.created_prompts: list[str] = []

    def create_session(self, prompt: str, title: str, tags: list[str] | None = None, **_: Any) -> dict[str, Any]:
        self._n += 1
        sid = f"fake{self._n:04d}"
        issue = next((int(t.split("-", 1)[1]) for t in (tags or []) if t.startswith("issue-")), self._n)
        if "[scan]" in title:
            mode = "scan"
        elif "[learn]" in title:
            mode = "learn"
        elif "[sim:blocked]" in title:
            mode = "blocked"
        elif "[sim:fail]" in title:
            mode = "failed"
        else:
            mode = "done"
        self._sessions[sid] = {"polls": 0, "mode": mode, "issue": issue, "finished": False, "title": title}
        self.created_prompts.append(prompt)
        return self._snapshot(sid, status="running", detail="working", so=None)

    def _key(self, session_id: str) -> str:
        return bare_session_id(session_id)

    def _snapshot(self, sid: str, status: str, detail: str | None, so: dict[str, Any] | None, prs: list | None = None):
        return {
            "session_id": sid,
            "url": f"https://app.devin.ai/sessions/{sid}",
            "status": status,
            "status_detail": detail,
            "structured_output": so,
            "pull_requests": prs or [],
            "acus_consumed": 0.0,
        }

    def _scan_output(self) -> dict[str, Any]:
        return {
            "outcome": "done",
            "summary": "Simulated sweep: ran npm audit, pip-audit, and mypy.",
            "findings": [
                {
                    "title": "Clear remaining npm advisories via overrides",
                    "description": "npm audit reports high-severity advisories in transitive dependencies (js-yaml, pacote).",
                    "how_to_triage": "Run npm audit in superset-frontend/ and confirm the advisories are listed.",
                    "verify_command": "cd superset-frontend && npm audit --audit-level=high",
                    "severity": "high",
                    "type": "dependency",
                    "source": "npm audit (superset-frontend/)",
                },
                {
                    "title": "Fix mypy errors in superset/utils/date_parser.py",
                    "description": "mypy reports operator errors on pyparsing Forward objects.",
                    "how_to_triage": "Run mypy on the file and confirm the errors.",
                    "verify_command": "pip install mypy pyparsing && mypy --ignore-missing-imports superset/utils/date_parser.py",
                    "severity": "medium",
                    "type": "lint",
                    "source": "mypy --check-untyped-defs superset/utils",
                },
                {
                    "title": "Add unit tests for superset/utils/retries.py",
                    "description": "retry_call has no direct unit tests.",
                    "how_to_triage": "Search tests/ for references to retry_call.",
                    "verify_command": "pytest tests/unit_tests/utils/retries_test.py",
                    "severity": "low",
                    "type": "tests",
                    "source": "coverage review",
                },
            ],
            "skipped": [{"title": "Override underscore", "reason": "Already covered by merged PR #1 / closed issue."}],
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        sid = self._key(session_id)
        s = self._sessions[sid]
        s["polls"] += 1
        mode = s["mode"]
        pr_url = f"https://github.com/{self.repo}/pull/{s['issue'] + 100}"
        if mode == "scan":
            so = self._scan_output()
            if s["finished"]:
                return self._snapshot(sid, "exit", None, so)
            if s["polls"] < 3:
                return self._snapshot(sid, "running", "working", None)
            return self._snapshot(sid, "running", "waiting_for_user", so)
        if mode == "learn":
            so = {
                "outcome": "done",
                "summary": "Proposed 2 rules from the feedback.",
                "pull_request_urls": [f"https://github.com/{self.solution_repo}/pull/7"],
                "rules_proposed": 2,
                "rules_skipped": [{"rule": "Always run the full test suite", "reason": "Contradicts an existing rule."}],
            }
            if s["finished"]:
                return self._snapshot(sid, "exit", None, so)
            if s["polls"] < 3:
                return self._snapshot(sid, "running", "working", None)
            return self._snapshot(sid, "running", "waiting_for_user", so)
        if s["finished"]:
            so = {"outcome": mode, "summary": "Simulated run finished.", "pull_request_urls": [pr_url]}
            return self._snapshot(sid, "exit", None, so, [{"pr_url": pr_url, "pr_state": "open"}])
        if s["polls"] < 3:
            return self._snapshot(sid, "running", "working", None)
        if mode == "blocked":
            so = {
                "outcome": "blocked",
                "summary": "Triage stopped before changing code.",
                "question": "Simulated question: which mitigation do you prefer?",
            }
            return self._snapshot(sid, "running", "waiting_for_user", so)
        if mode == "failed":
            so = {"outcome": "failed", "summary": "Simulated failure: verification kept failing."}
            return self._snapshot(sid, "exit", None, so)
        so = {
            "outcome": "done",
            "summary": "Simulated fix opened as a PR; verify command passed.",
            "pull_request_urls": [pr_url],
            "verify_passed": True,
        }
        return self._snapshot(sid, "running", "waiting_for_user", so, [{"pr_url": pr_url, "pr_state": "open"}])

    def send_message(self, session_id: str, message: str) -> dict[str, Any]:
        sid = self._key(session_id)
        self.messages_sent.append((sid, message))
        if self._sessions[sid]["mode"] in ("done", "scan", "learn"):
            self._sessions[sid]["finished"] = True
        return self._snapshot(sid, "running", None, None)  # like the real API: state fields are unreliable here

    def terminate(self, session_id: str) -> dict[str, Any]:
        sid = self._key(session_id)
        self.terminated.append(sid)
        if self._sessions[sid]["finished"]:
            return {"already_exited": True}
        self._sessions[sid]["finished"] = True
        return {}

    def list_messages(self, session_id: str, max_pages: int = 10) -> list[dict[str, Any]]:
        return [{"source": "devin", "message": "Simulated question: which mitigation do you prefer?"}]


def make_client(settings: Settings) -> DevinClient | FakeDevinClient:
    if settings.devin_mode == "fake":
        return FakeDevinClient(repo=settings.target_repo, solution_repo=settings.solution_repo)
    if not settings.devin_api_key or not settings.devin_org_id:
        raise ConfigError("DEVIN_API_KEY and DEVIN_ORG_ID are required unless DEVIN_MODE=fake")
    return DevinClient(settings.devin_api_key, settings.devin_org_id, settings.devin_api_base)
