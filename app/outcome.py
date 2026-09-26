"""Turn a Devin session snapshot into a decision for the bot.

Why this is needed (verified against the real API): `status: running` with
`status_detail: waiting_for_user` is AMBIGUOUS. It appears both while Devin waits
for an answer and after Devin has finished and gone idle. So we ask Devin to
report an explicit `outcome` in its structured output and read that. A session
that ends as `suspended` (idle timeout) is a normal end, not an error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .devin_client import TERMINAL_STATUSES

VALID_OUTCOMES = ("done", "blocked", "failed")


@dataclass
class Interpretation:
    kind: str  # working | done | blocked | failed
    summary: str = ""
    question: str = ""
    pr_urls: list[str] = field(default_factory=list)
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)  # the raw structured output


def _pr_urls(session: dict[str, Any], so: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for u in list(so.get("pull_request_urls") or []) + [
        p.get("pr_url") for p in (session.get("pull_requests") or []) if isinstance(p, dict)
    ]:
        if u and u not in urls:
            urls.append(u)
    return urls


def interpret(session: dict[str, Any], require_pr: bool = True) -> Interpretation:
    """`require_pr=True` is for fix sessions (done without a PR is a failure).
    Scan and learn sessions pass False: they can legitimately finish with no PR."""
    so = session.get("structured_output")
    so = so if isinstance(so, dict) else {}
    outcome = so.get("outcome") if so.get("outcome") in VALID_OUTCOMES else None
    status = session.get("status")
    detail = session.get("status_detail")
    prs = _pr_urls(session, so)
    summary = str(so.get("summary") or "")
    question = str(so.get("question") or "")

    def make(kind: str, reason: str = "") -> Interpretation:
        if kind == "done" and require_pr and not prs:
            return Interpretation("failed", summary, "", [], "reported done but no pull request was found", so)
        return Interpretation(kind, summary, question, prs, reason, so)

    if status == "error":
        return Interpretation("failed", summary, "", prs, "Devin session ended with an error", so)

    if status in TERMINAL_STATUSES:  # exit or suspended (idle timeout)
        if outcome:
            return make(outcome)
        if prs and require_pr:
            return make("done", "session ended without an outcome, but a pull request exists")
        return Interpretation("failed", summary, "", [], f"session ended ({status}) with no outcome and no PR", so)

    # new / claimed / running / resuming
    if detail == "waiting_for_user":
        if outcome:
            return make(outcome)
        return Interpretation("blocked", summary, question, prs, "waiting_for_user with no explicit outcome", so)
    return Interpretation("working", data=so)
