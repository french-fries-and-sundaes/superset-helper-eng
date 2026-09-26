"""The bot's brain: dispatch approved tasks to Devin, poll sessions, move task states.

Design notes
  * Every state change is a compare-and-set in the store, so the webhook thread and
    the worker loop can run at the same time without double-starting a session.
  * A task is "claimed" (READY -> IN_PROGRESS) BEFORE the Devin API call, and released
    back to READY if the call fails for a retryable reason.
  * MAX_SESSIONS_PER_DAY is a rolling 24h count of sessions started. It is the main
    spend guard, because the API's acus_consumed field did not report usage.
  * M6 hook: when a session reports `done`, the task currently goes straight to
    IN_REVIEW. Milestone 6 inserts the GitHub Actions verification gate here.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Callable

from .config import Settings
from .db import Store
from .devin_client import DevinAPIError
from .outcome import Interpretation, interpret
from .prompts import OUTPUT_SCHEMA, WRAP_UP_MESSAGE, build_task_prompt, load_knowledge
from .states import State

log = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60


class Orchestrator:
    def __init__(
        self,
        store: Store,
        devin: Any,
        settings: Settings,
        now: Callable[[], float] = time.time,
        knowledge: str | None = None,
    ) -> None:
        self.store = store
        self.devin = devin
        self.settings = settings
        self.now = now
        self._knowledge = knowledge  # None = load from disk each time

    # ---- intake (called by the webhook / CLI) ----------------------------------
    def request_ready(self, repo: str, issue_number: int, title: str, body: str, source: str = "human") -> str:
        """A human applied devin:ready. Idempotent: returns what happened."""
        task = self.store.get_task_by_issue(repo, issue_number)
        if task is None:
            try:
                self.store.create_task(repo, issue_number, title, body, State.READY, source, now=int(self.now()))
                return "created"
            except sqlite3.IntegrityError:  # lost a race with a duplicate delivery
                task = self.store.get_task_by_issue(repo, issue_number)
        state = State(task["state"])  # type: ignore[index]
        if state in (State.PROPOSED, State.BLOCKED, State.FAILED, State.REJECTED):
            # Issue text may have been edited to answer a question: take the latest.
            self.store.update_task(task["id"], title=title, body=body, current_session_id=None)  # type: ignore[index]
            if self.store.transition(task["id"], State.READY, "human re-applied devin:ready", expected=state):  # type: ignore[index]
                return "requeued"
        return "ignored"

    def register_proposed(self, repo: str, issue_number: int, title: str, body: str, source: str = "scan") -> str:
        if self.store.get_task_by_issue(repo, issue_number) is not None:
            return "ignored"
        try:
            self.store.create_task(repo, issue_number, title, body, State.PROPOSED, source, now=int(self.now()))
            return "created"
        except sqlite3.IntegrityError:
            return "ignored"

    # ---- worker ----------------------------------------------------------------
    def tick(self) -> None:
        self.poll_active()
        self.dispatch_ready()

    def dispatch_ready(self) -> int:
        started = 0
        for task in self.store.list_tasks(State.READY):
            if self.sessions_last_24h() >= self.settings.max_sessions_per_day:
                log.warning("daily session limit (%d) reached; %d task(s) stay in READY",
                            self.settings.max_sessions_per_day, len(self.store.list_tasks(State.READY)))
                break
            if self._start(task):
                started += 1
        return started

    def sessions_last_24h(self) -> int:
        return self.store.sessions_started_since(self.now() - DAY_SECONDS)

    def _knowledge_text(self) -> str:
        return self._knowledge if self._knowledge is not None else load_knowledge(self.settings.knowledge_dir)

    def _start(self, task: dict[str, Any]) -> bool:
        tid = task["id"]
        if not self.store.transition(tid, State.IN_PROGRESS, "claimed for a new session", expected=State.READY):
            return False  # someone else moved it
        attempt = task["attempt"] + 1
        n = task["issue_number"]
        try:
            resp = self.devin.create_session(
                prompt=build_task_prompt(task, self._knowledge_text()),
                title=f"#{n}: {task['title']}"[:120],
                tags=["superset-helper-eng", f"issue-{n}", f"attempt-{attempt}"],
                structured_output_schema=OUTPUT_SCHEMA,
                max_acu_limit=self.settings.max_acu_per_session,
            )
        except DevinAPIError as e:
            self.store.add_event(tid, "session_start_failed", str(e))
            if e.retryable:
                self.store.transition(tid, State.READY, f"retryable start failure: {e}", expected=State.IN_PROGRESS)
            else:
                self.store.transition(tid, State.FAILED, f"could not start session: {e}", expected=State.IN_PROGRESS)
            return False
        sid = resp["session_id"]
        self.store.add_session(
            sid, tid, attempt, resp.get("url", ""), resp.get("status", ""), resp.get("status_detail"), now=int(self.now())
        )
        self.store.update_task(tid, current_session_id=sid, attempt=attempt)
        self.store.add_event(tid, "session_started", f"{sid} (attempt {attempt})")
        return True

    def poll_active(self) -> None:
        for sess in self.store.active_sessions():
            sid = sess["session_id"]
            try:
                snap = self.devin.get_session(sid)
            except DevinAPIError as e:
                log.warning("poll failed for %s: %s", sid, e)
                self.store.add_event(sess["task_id"], "poll_failed", str(e))
                continue
            self.store.update_session(
                sid,
                status=snap.get("status") or "",
                status_detail=snap.get("status_detail"),
                last_polled_at=int(self.now()),
                raw_json=json.dumps(snap),
            )
            result = interpret(snap)
            if result.kind != "working":
                self._apply(sess["task_id"], sid, result)

    def _apply(self, task_id: int, sid: str, r: Interpretation) -> None:
        if r.kind == "done":
            # M6: gate on GitHub Actions verification here before moving to IN_REVIEW.
            if self.store.transition(task_id, State.IN_REVIEW, r.summary or "PR opened", expected=State.IN_PROGRESS):
                self.store.update_task(task_id, pr_urls=r.pr_urls, last_summary=r.summary)
                self.store.update_session(sid, outcome="done", finished_at=int(self.now()))
                self._end_session(sid, wrap_up=True)
        elif r.kind == "blocked":
            question = r.question or self._last_devin_message(sid) or "Devin is waiting for input."
            if self.store.transition(task_id, State.BLOCKED, question, expected=State.IN_PROGRESS):
                self.store.update_task(task_id, blocked_question=question, last_summary=r.summary)
                self.store.update_session(sid, outcome="blocked", finished_at=int(self.now()))
                self._end_session(sid, wrap_up=False)
        else:  # failed
            reason = r.summary or r.reason or "session failed"
            if self.store.transition(task_id, State.FAILED, reason, expected=State.IN_PROGRESS):
                self.store.update_task(task_id, last_summary=reason)
                self.store.update_session(sid, outcome="failed", finished_at=int(self.now()))
                self._end_session(sid, wrap_up=False)

    def _end_session(self, sid: str, wrap_up: bool) -> None:
        """Done: ask Devin to finish (session exits). Blocked/failed: terminate; a human's
        answer starts a fresh session with the updated issue text."""
        try:
            if wrap_up:
                self.devin.send_message(sid, WRAP_UP_MESSAGE)
            else:
                self.devin.terminate(sid)
        except DevinAPIError as e:
            log.warning("could not end session %s cleanly: %s", sid, e)

    def _last_devin_message(self, sid: str) -> str:
        try:
            msgs = [m for m in self.devin.list_messages(sid) if m.get("source") == "devin"]
        except DevinAPIError:
            return ""
        return str(msgs[-1].get("message", "")) if msgs else ""

    # ---- reporting -------------------------------------------------------------
    def status_summary(self) -> dict[str, Any]:
        return {
            "counts": self.store.counts_by_state(),
            "sessions_last_24h": self.sessions_last_24h(),
            "max_sessions_per_day": self.settings.max_sessions_per_day,
        }
