"""GitHub webhook handling: signature check and event -> orchestrator actions."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from .config import Settings
from .orchestrator import Orchestrator


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Validate GitHub's X-Hub-Signature-256 header (constant-time comparison)."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def handle_event(
    event: str, payload: dict[str, Any], delivery_id: str | None, orch: Orchestrator, settings: Settings
) -> dict[str, Any]:
    """Map a GitHub event to an action. Safe to call twice with the same delivery."""
    if event == "ping":
        return {"status": "pong"}
    if event != "issues":
        return {"status": "ignored", "reason": f"event '{event}' not handled yet"}
    if delivery_id and orch.store.has_delivery(delivery_id):
        return {"status": "duplicate_delivery"}

    result = _handle_issue_event(payload, orch, settings)

    if delivery_id:
        orch.store.record_delivery(delivery_id)
    return result


def _handle_issue_event(payload: dict[str, Any], orch: Orchestrator, settings: Settings) -> dict[str, Any]:
    action = payload.get("action")
    issue = payload.get("issue") or {}
    repo = (payload.get("repository") or {}).get("full_name")

    if repo != settings.target_repo:
        return {"status": "ignored", "reason": f"repository {repo!r} is not the target"}
    if "pull_request" in issue:  # GitHub delivers PRs as issues too
        return {"status": "ignored", "reason": "pull request"}

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
        return {"status": "ignored", "reason": f"issues action '{action}' not handled yet"}

    number = issue.get("number")
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    if not isinstance(number, int):
        return {"status": "ignored", "reason": "no issue number"}

    if trigger == settings.ready_label:
        return {"status": "ok", "task": orch.request_ready(repo, number, title, body), "issue": number}
    if trigger == settings.proposed_label:
        return {"status": "ok", "task": orch.register_proposed(repo, number, title, body), "issue": number}
    return {"status": "ignored", "reason": f"label {trigger!r} is not a trigger"}
