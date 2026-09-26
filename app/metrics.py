"""Answers "how would I know this is working?" from the event log and task tables.

Outcomes are kept honest and mutually exclusive, so being right is never penalized:
  merged             a fix was verified, reviewed, and merged            (work delivered)
  not_needed         Devin pushed back and a human agreed and closed it  (a false positive / duplicate caught)
  rejected_fix       a human declined a fix Devin produced               (quality signal)
  proposal_rejected  a human declined a scanner proposal (or an approved task before work began)
  failed             Devin could not complete it (including tasks closed after failing)
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from .db import Store
from .states import State


def humanize(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{round(s / 60)}m"
    if s < 48 * 3600:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _pct(a: int, b: int) -> int | None:
    return round(100 * a / b) if b else None


def _previous_state(events: list[dict[str, Any]], target: str) -> str:
    """The state a task was in just before it entered `target` (from the event log)."""
    for e in reversed(events):
        if e["kind"] == "state_changed" and f"-> {target}" in e["detail"]:
            return e["detail"].split(" -> ")[0]
    return ""


def compute_metrics(store: Store, now: float | None = None, completed_window: float | None = None) -> dict[str, Any]:
    """`completed_window` (seconds) limits the merged / not-needed counts; None = all time."""
    now = now if now is not None else time.time()
    tasks = store.list_tasks()
    counts = store.counts_by_state()

    queue_wait: list[float] = []
    work_to_review: list[float] = []
    created_to_review: list[float] = []
    merged_recent = not_needed_recent = 0
    ever_blocked = ever_failed = changes_requested = 0
    first_pass = reached_review = 0
    outcomes = {"merged": 0, "not_needed": 0, "rejected_fix": 0, "proposal_rejected": 0, "failed": 0}
    pushbacks = {"total": 0, "closed_not_needed": 0, "answered_and_continued": 0, "waiting": 0}
    proposals = {"filed": 0, "approved": 0, "rejected": 0, "pending": 0}

    def in_window(ts: int) -> bool:
        return completed_window is None or now - ts <= completed_window

    for t in tasks:
        events = store.list_events(t["id"], limit=1000, ascending=True)
        first_started = first_review = None
        seen_blocked = seen_failed = went_ready_from_proposed = False
        review_after_block = False
        for e in events:
            d = e["detail"]
            if e["kind"] == "state_changed":
                if "-> in-progress" in d and first_started is None:
                    first_started = e["ts"]
                if "-> in-review" in d:
                    if first_review is None:
                        first_review = e["ts"]
                    if seen_blocked:
                        review_after_block = True
                if "-> blocked" in d:
                    seen_blocked = True
                if "-> failed" in d:
                    seen_failed = True
                if "proposed -> ready" in d:
                    went_ready_from_proposed = True
                if "-> completed" in d and in_window(e["ts"]):
                    merged_recent += 1
                if "-> not-needed" in d and in_window(e["ts"]):
                    not_needed_recent += 1
            elif e["kind"] == "changes_requested":
                changes_requested += 1
        ever_blocked += seen_blocked
        ever_failed += seen_failed
        if first_started:
            queue_wait.append(first_started - t["created_at"])
        if first_started and first_review:
            work_to_review.append(first_review - first_started)
        if first_review:
            created_to_review.append(first_review - t["created_at"])
            reached_review += 1
            if not (seen_blocked or seen_failed) and t["attempt"] <= 1:
                first_pass += 1

        # ---- outcome classification (terminal or awaiting-human-after-failure states only)
        state = t["state"]
        if state == State.COMPLETED.value:
            outcomes["merged"] += 1
        elif state == State.NOT_NEEDED.value:
            outcomes["not_needed"] += 1
        elif state == State.FAILED.value:
            outcomes["failed"] += 1
        elif state == State.REJECTED.value:
            prev = _previous_state(events, "rejected")
            if prev in ("proposed", "ready"):
                outcomes["proposal_rejected"] += 1
            elif prev == "failed":
                outcomes["failed"] += 1  # a human gave up on a failed task: still a failure
            else:
                outcomes["rejected_fix"] += 1

        # ---- pushbacks: how did Devin's "blocked at triage" turn out?
        if seen_blocked:
            pushbacks["total"] += 1
            if state == State.NOT_NEEDED.value:
                pushbacks["closed_not_needed"] += 1
            elif state == State.BLOCKED.value:
                pushbacks["waiting"] += 1
            elif t["attempt"] >= 2:  # a human's answer started a fresh session, whatever came of it since
                pushbacks["answered_and_continued"] += 1

        # ---- scanner proposals
        if t["source"] == "scan":
            proposals["filed"] += 1
            if went_ready_from_proposed:
                proposals["approved"] += 1
            if state == State.PROPOSED.value:
                proposals["pending"] += 1
            elif state == State.REJECTED.value and _previous_state(events, "rejected") == "proposed":
                proposals["rejected"] += 1

    attempted = outcomes["merged"] + outcomes["rejected_fix"] + outcomes["failed"]
    prs = [p for t in tasks for p in store.prs_for_task(t["id"])]
    v_pass = sum(1 for p in prs if p["verify_status"] == "passed")
    v_fail = sum(1 for p in prs if p["verify_status"] == "failed")
    sessions_total = sum(len(store.sessions_for_task(t["id"])) for t in tasks)
    started = sum(1 for t in tasks if t["attempt"] > 0)

    return {
        "tasks_total": len(tasks),
        "counts": counts,
        "outcomes": outcomes,
        "completed_in_window": merged_recent,  # kept name: merged fixes in the window
        "not_needed_in_window": not_needed_recent,
        "finished": sum(outcomes.values()),
        # Being right is never penalized: no-change outcomes are not in this denominator.
        "merge_rate_pct": _pct(outcomes["merged"], attempted),
        "first_pass_rate_pct": _pct(first_pass, reached_review),
        "blocked_rate_pct": _pct(ever_blocked, started),
        "failed_rate_pct": _pct(ever_failed, started),
        "changes_requested": changes_requested,
        "pushbacks": pushbacks,
        "proposals": {**proposals, "approval_rate_pct": _pct(proposals["approved"], proposals["approved"] + proposals["rejected"])},
        "median_queue_wait": _median(queue_wait),
        "median_work_to_review": _median(work_to_review),
        "median_created_to_review": _median(created_to_review),
        "verification": {"passed": v_pass, "failed": v_fail, "pass_rate_pct": _pct(v_pass, v_pass + v_fail)},
        "sessions_total": sessions_total,
        "sessions_per_task": round(sessions_total / started, 2) if started else None,
    }
