"""Mirror task state onto GitHub: one `devin:*` status label per issue, plus a short
explanatory comment the first time a task reaches each state.

The store is the source of truth; GitHub is a view of it. `sync_all()` looks for tasks
whose `synced_state` lags their `state` and catches them up, so a GitHub outage only delays
the mirror: the next tick retries. Each comment is posted at most once per (state, attempt).
"""
from __future__ import annotations

import logging
from typing import Any

from .config import Settings
from .db import Store
from .github_client import GitHubAPIError
from .states import State

log = logging.getLogger(__name__)


class Syncer:
    def __init__(self, store: Store, github: Any, settings: Settings) -> None:
        self.store = store
        self.github = github
        self.settings = settings

    def sync_all(self) -> int:
        n = 0
        for task in self.store.unsynced_tasks():
            if self.sync_task(task):
                n += 1
        return n

    def sync_task(self, task: dict[str, Any]) -> bool:
        state = State(task["state"])
        if state is State.IN_PROGRESS and not task["current_session_id"]:
            return False  # session is being started; wait so the comment can link to it
        repo, number = task["repo"], task["issue_number"]
        try:
            self.github.set_status_label(repo, number, state.label)
            body = self._comment_for(task, state)
            if body:
                key = f"{state.value}:{task['attempt']}"
                if self.store.claim_sync_key(task["id"], key):
                    try:
                        self.github.comment(repo, number, body)
                    except GitHubAPIError:
                        self.store.release_sync_key(task["id"], key)
                        raise
            self.store.update_task(task["id"], synced_state=state.value)
            return True
        except GitHubAPIError as e:
            log.warning("GitHub sync failed for #%s: %s", number, e)
            if e.status == 410:  # "This issue was deleted": retrying can never succeed
                if self.store.claim_sync_key(task["id"], f"gone:{state.value}"):
                    self.store.add_event(task["id"], "github_issue_missing", f"#{number}: {e}. Not retrying.")
                self.store.update_task(task["id"], synced_state=state.value)
                return False
            # anything else may be temporary (outage, permissions): retry next tick, but log it once
            if self.store.claim_sync_key(task["id"], f"syncfail:{state.value}:{e.status}"):
                self.store.add_event(task["id"], "github_sync_failed", str(e))
            return False

    # ---- comment bodies --------------------------------------------------------
    def _session_url(self, task: dict[str, Any]) -> str:
        sessions = self.store.sessions_for_task(task["id"])
        return sessions[-1]["url"] if sessions else ""

    def _comment_for(self, task: dict[str, Any], state: State) -> str:
        url = self._session_url(task)
        session_line = f"\n\nDevin session: {url}" if url else ""
        attempt = task["attempt"]
        if state is State.IN_PROGRESS:
            return (
                f"Devin started working on this (attempt {attempt}).{session_line}\n\n"
                "It runs the verify command on the unmodified code first. If the problem does not reproduce, "
                "or the task is unclear, it stops and asks instead of guessing."
            )
        if state is State.BLOCKED:
            q = task["blocked_question"] or "Devin is waiting for input."
            return (
                "Devin needs your input before it can continue.\n\n"
                f"> {q}\n\n"
                "To unblock: answer in a comment (or edit the issue), then re-apply the `devin:ready` label. "
                f"A fresh Devin session will start with your answer.{session_line}"
            )
        if state is State.IN_REVIEW:
            prs = "\n".join(f"- {u}" for u in task["pr_urls"]) or "- (see the linked pull request)"
            verified = (
                "Independent verification passed on GitHub Actions."
                if self.settings.verify_mode == "actions"
                else "Independent verification is turned off."
            )
            return (
                f"A pull request is ready for review:\n{prs}\n\n{verified}\n\n"
                "Merge it to complete this task, request changes to send feedback back to Devin, "
                f"or close it without merging to reject.{session_line}"
            )
        if state is State.FAILED:
            why = task["last_summary"] or "The session did not finish."
            return (
                f"Devin could not complete this.\n\n{why}\n\n"
                f"To retry: comment with a hint (or edit the issue), then re-apply `devin:ready`.{session_line}"
            )
        if state is State.REJECTED:
            return "Marked as rejected. To bring it back: reopen the issue and re-apply `devin:ready`."
        return ""
