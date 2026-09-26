"""GitHub webhook handling: signature check and event -> orchestrator actions.

Events handled: issues (labeled / opened / closed), issue_comment, pull_request
(opened / reopened / synchronize / closed), pull_request_review, workflow_run, ping.
Subscribe the fork's webhook to exactly those.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from .config import Settings
from .github_client import BOT_MARKER
from .orchestrator import Orchestrator


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Validate GitHub's X-Hub-Signature-256 header (constant-time comparison)."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _ignored(reason: str) -> dict[str, Any]:
    return {"status": "ignored", "reason": reason}


def handle_event(
    event: str, payload: dict[str, Any], delivery_id: str | None, orch: Orchestrator, settings: Settings
) -> dict[str, Any]:
    """Map a GitHub event to an action. Safe to call twice with the same delivery."""
    if event == "ping":
        return {"status": "pong"}
    handler = {
        "issues": _issues,
        "issue_comment": _issue_comment,
        "pull_request": _pull_request,
        "pull_request_review": _review,
        "workflow_run": _workflow_run,
    }.get(event)
    if handler is None:
        return _ignored(f"event '{event}' not handled")
    if delivery_id and orch.store.has_delivery(delivery_id):
        return {"status": "duplicate_delivery"}

    repo = (payload.get("repository") or {}).get("full_name")
    if repo != settings.target_repo:
        result = _ignored(f"repository {repo!r} is not the target")
    else:
        result = handler(payload, repo, orch, settings)
        if result.get("status") == "ok":
            orch.sync()  # push label/comment changes to GitHub right away

    if delivery_id:
        orch.store.record_delivery(delivery_id)
    return result


def _ok(**kw: Any) -> dict[str, Any]:
    return {"status": "ok", **kw}


# ---- issues ---------------------------------------------------------------------------
def _issues(payload: dict[str, Any], repo: str, orch: Orchestrator, settings: Settings) -> dict[str, Any]:
    action = payload.get("action")
    issue = payload.get("issue") or {}
    if "pull_request" in issue:  # GitHub delivers PRs as issues too
        return _ignored("pull request")
    number = issue.get("number")
    if not isinstance(number, int):
        return _ignored("no issue number")
    title = issue.get("title") or ""
    body = issue.get("body") or ""

    if action == "closed":
        return _ok(task=orch.handle_issue_closed(repo, number, issue.get("state_reason")), issue=number)

    labels = {lab.get("name") for lab in issue.get("labels", []) if isinstance(lab, dict)}
    if action == "labeled":
        trigger = (payload.get("label") or {}).get("name")
    elif action == "opened":  # an issue created with the label already on it
        trigger = (
            settings.ready_label
            if settings.ready_label in labels
            else settings.proposed_label
            if settings.proposed_label in labels
            else None
        )
    else:
        return _ignored(f"issues action '{action}' not handled")

    if trigger == settings.ready_label:
        return _ok(task=orch.request_ready(repo, number, title, body), issue=number)
    if trigger == settings.proposed_label:
        return _ok(task=orch.register_proposed(repo, number, title, body), issue=number)
    return _ignored(f"label {trigger!r} is not a trigger")


# ---- comments -------------------------------------------------------------------------
def _is_bot_comment(comment: dict[str, Any]) -> bool:
    user = comment.get("user") or {}
    return user.get("type") == "Bot" or BOT_MARKER in (comment.get("body") or "")


def _issue_comment(payload: dict[str, Any], repo: str, orch: Orchestrator, settings: Settings) -> dict[str, Any]:
    if payload.get("action") != "created":
        return _ignored("comment action not handled")
    comment = payload.get("comment") or {}
    if _is_bot_comment(comment):
        return _ignored("bot comment")
    issue = payload.get("issue") or {}
    number = issue.get("number")
    if not isinstance(number, int):
        return _ignored("no issue number")
    author = (comment.get("user") or {}).get("login", "")
    res = orch.handle_comment(repo, number, author, comment.get("body") or "", on_pr="pull_request" in issue)
    return _ok(feedback=res, issue=number)


# ---- pull requests --------------------------------------------------------------------
def _pull_request(payload: dict[str, Any], repo: str, orch: Orchestrator, settings: Settings) -> dict[str, Any]:
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    number = pr.get("number") or payload.get("number")
    if not isinstance(number, int):
        return _ignored("no pull request number")
    sha = (pr.get("head") or {}).get("sha", "")
    if action in ("opened", "reopened"):
        return _ok(pr=number, result=orch.handle_pr_opened(repo, number, pr.get("html_url", ""), sha, pr.get("body") or ""))
    if action == "synchronize":
        return _ok(pr=number, result=orch.handle_pr_synchronize(repo, number, sha))
    if action == "closed":
        return _ok(pr=number, result=orch.handle_pr_closed(repo, number, bool(pr.get("merged"))))
    return _ignored(f"pull_request action '{action}' not handled")


def _review(payload: dict[str, Any], repo: str, orch: Orchestrator, settings: Settings) -> dict[str, Any]:
    if payload.get("action") != "submitted":
        return _ignored("review action not handled")
    review = payload.get("review") or {}
    number = (payload.get("pull_request") or {}).get("number")
    if not isinstance(number, int):
        return _ignored("no pull request number")
    res = orch.handle_review(
        repo,
        number,
        (review.get("state") or "").lower(),
        review.get("body") or "",
        review.get("id"),
        (review.get("user") or {}).get("login", ""),
    )
    return _ok(pr=number, result=res)


def _workflow_run(payload: dict[str, Any], repo: str, orch: Orchestrator, settings: Settings) -> dict[str, Any]:
    if payload.get("action") != "completed":
        return _ignored("workflow_run action not handled")
    return _ok(result=orch.handle_workflow_run(repo, payload.get("workflow_run") or {}))
