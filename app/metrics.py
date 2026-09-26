"""Answers "how would I know this is working?" from the event log and task tables."""
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


def compute_metrics(store: Store, now: float | None = None, completed_window: float | None = None) -> dict[str, Any]:
    """`completed_window` (seconds) limits the 'completed' count/list; None = all time."""
    now = now if now is not None else time.time()
    tasks = store.list_tasks()
    counts = store.counts_by_state()

    queue_wait: list[float] = []
    work_to_review: list[float] = []
    created_to_review: list[float] = []
    completed_recent = 0
    ever_blocked = ever_failed = changes_requested = 0
    first_pass = reached_review = 0

    for t in tasks:
        events = store.list_events(t["id"], limit=1000, ascending=True)
        first_started = first_review = None
        seen_blocked = seen_failed = False
        for e in events:
            d = e["detail"]
            if e["kind"] == "state_changed":
                if "-> in-progress" in d and first_started is None:
                    first_started = e["ts"]
                if "-> in-review" in d and first_review is None:
                    first_review = e["ts"]
                if "-> blocked" in d:
                    seen_blocked = True
                if "-> failed" in d:
                    seen_failed = True
                if "-> completed" in d and (completed_window is None or now - e["ts"] <= completed_window):
                    completed_recent += 1
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

    finished = counts["completed"] + counts["failed"] + counts["rejected"]
    prs = [p for t in tasks for p in store.prs_for_task(t["id"])]
    v_pass = sum(1 for p in prs if p["verify_status"] == "passed")
    v_fail = sum(1 for p in prs if p["verify_status"] == "failed")
    sessions_total = sum(len(store.sessions_for_task(t["id"])) for t in tasks)
    started = sum(1 for t in tasks if t["attempt"] > 0)

    def pct(a: int, b: int) -> float | None:
        return round(100 * a / b) if b else None

    return {
        "tasks_total": len(tasks),
        "counts": counts,
        "completed_in_window": completed_recent,
        "finished": finished,
        "merge_rate_pct": pct(counts["completed"], finished),
        "first_pass_rate_pct": pct(first_pass, reached_review),
        "blocked_rate_pct": pct(ever_blocked, started),
        "failed_rate_pct": pct(ever_failed, started),
        "changes_requested": changes_requested,
        "median_queue_wait": _median(queue_wait),
        "median_work_to_review": _median(work_to_review),
        "median_created_to_review": _median(created_to_review),
        "verification": {"passed": v_pass, "failed": v_fail, "pass_rate_pct": pct(v_pass, v_pass + v_fail)},
        "sessions_total": sessions_total,
        "sessions_per_task": round(sessions_total / started, 2) if started else None,
    }
