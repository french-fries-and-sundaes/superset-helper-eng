"""Shared builders for tests: an orchestrator wired to fakes, and GitHub-shaped payloads."""
from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import Store
from app.devin_client import FakeDevinClient
from app.github_client import RecordingGitHub
from app.orchestrator import Orchestrator
from app.states import State

REPO = "o/r"
SOLUTION = "o/solution"


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def build(verify_mode: str = "off", **overrides: Any):
    store = Store(":memory:")
    devin = FakeDevinClient(REPO, SOLUTION)
    gh = RecordingGitHub()
    clock = Clock()
    settings = Settings(target_repo=REPO, solution_repo=SOLUTION, verify_mode=verify_mode, **overrides)
    orch = Orchestrator(store, devin, settings, github=gh, now=clock, knowledge="RULES")
    return orch, store, devin, gh, clock


def run_ticks(orch: Orchestrator, n: int = 6) -> None:
    for _ in range(n):
        orch.tick()


def state_of(store: Store, issue: int) -> str:
    return store.get_task_by_issue(REPO, issue)["state"]


def task(store: Store, issue: int) -> dict[str, Any]:
    return store.get_task_by_issue(REPO, issue)


def to_review(orch: Orchestrator, store: Store, issue: int = 1, title: str = "Fix thing") -> dict[str, Any]:
    """Drive a task all the way to in-review (requires verify_mode off)."""
    orch.request_ready(REPO, issue, title, "body")
    run_ticks(orch)
    assert state_of(store, issue) == State.IN_REVIEW.value
    return task(store, issue)


# ---- GitHub-shaped payloads ---------------------------------------------------------
def repo_block(repo: str = REPO) -> dict[str, Any]:
    return {"full_name": repo}


def issue_event(action: str, number: int, title: str = "t", body: str = "b", label: str | None = None,
                labels: list[str] | None = None, reason: str | None = None, repo: str = REPO) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "number": number, "title": title, "body": body,
        "labels": [{"name": n} for n in (labels or ([label] if label else []))],
        "state": "closed" if action == "closed" else "open",
    }
    if reason:
        issue["state_reason"] = reason
    payload: dict[str, Any] = {"action": action, "issue": issue, "repository": repo_block(repo)}
    if action == "labeled" and label:
        payload["label"] = {"name": label}
    return payload


def comment_event(number: int, text: str, author: str = "alice", user_type: str = "User", on_pr: bool = False,
                  repo: str = REPO) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": number}
    if on_pr:
        issue["pull_request"] = {"url": "x"}
    return {"action": "created", "issue": issue, "comment": {"body": text, "user": {"login": author, "type": user_type}},
            "repository": repo_block(repo)}


def pr_event(action: str, number: int, body: str = "Closes #1", sha: str = "sha1", merged: bool = False,
             repo: str = REPO) -> dict[str, Any]:
    return {
        "action": action, "number": number,
        "pull_request": {"number": number, "html_url": f"https://github.com/{repo}/pull/{number}", "body": body,
                         "merged": merged, "head": {"sha": sha}},
        "repository": repo_block(repo),
    }


def review_event(number: int, state: str, body: str = "please change X", author: str = "reviewer",
                 review_id: int = 5, repo: str = REPO) -> dict[str, Any]:
    return {"action": "submitted", "review": {"id": review_id, "state": state, "body": body, "user": {"login": author}},
            "pull_request": {"number": number}, "repository": repo_block(repo)}


def workflow_run(conclusion: str, pr_numbers: list[int] | None = None, sha: str = "sha1", run_id: int = 77,
                 name: str = "devin-verify", repo: str = REPO) -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {"id": run_id, "name": name, "conclusion": conclusion, "head_sha": sha,
                         "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
                         "pull_requests": [{"number": n} for n in (pr_numbers or [])]},
        "repository": repo_block(repo),
    }
