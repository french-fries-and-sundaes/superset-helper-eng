"""The bot's brain: dispatch approved tasks to Devin, poll sessions, move task states,
react to GitHub events, and run the scan and learn jobs.

Design notes
  * Every state change is a compare-and-set in the store, so the webhook thread and the
    worker loop can run at the same time without double-starting a session.
  * A task is "claimed" (READY -> IN_PROGRESS) BEFORE the Devin API call, and released
    back to READY if the call fails for a retryable reason.
  * MAX_SESSIONS_PER_DAY is a rolling 24h count of sessions started (fix, scan, and learn).
    It is the main spend guard, because the API's acus_consumed field did not report usage.
  * Verification gate: when Devin reports `done`, the task stays IN_PROGRESS with
    verify_status='pending' until the GitHub Actions workflow on the fork reports for the
    PR; pass -> IN_REVIEW, fail -> FAILED. (VERIFY_MODE=off skips the gate.)
  * One human signal: applying `devin:ready`. Recovery from blocked / failed / rejected
    starts a FRESH session carrying the human's comments as guidance.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Callable

from .config import Settings
from .db import Store
from .devin_client import DevinAPIError
from .github_client import GitHubAPIError, RecordingGitHub, parse_pr_url
from .outcome import Interpretation, interpret
from .prompts import (
    FINDING_TYPES,
    LEARN_SCHEMA,
    OUTPUT_SCHEMA,
    SCAN_SCHEMA,
    WRAP_UP_MESSAGE,
    build_learn_prompt,
    build_scan_prompt,
    build_task_prompt,
    load_knowledge,
)
from .states import State
from .sync import Syncer

log = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60
CLOSES_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")


def parse_closes(body: str | None) -> list[int]:
    return [int(n) for n in CLOSES_RE.findall(body or "")]


def render_issue_body(f: dict[str, Any]) -> str:
    """Issue body for a scan finding: description and how-to-triage as separate sections."""
    return (
        f"## Bug Description\n{str(f.get('description', '')).strip()}\n\n"
        f"## How to Triage / Test\n{str(f.get('how_to_triage', '')).strip()}\n\n"
        f"## Verify command\n```verify\n{str(f.get('verify_command', '')).strip()}\n```\n\n"
        f"## Details\n- Severity: {f.get('severity', 'unknown')}\n- Type: {f.get('type', 'other')}\n"
        f"- Found by: {f.get('source', 'scan')}\n- Filed by: superset-helper-eng scan (needs human approval: apply `devin:ready`)\n"
    )


class Orchestrator:
    def __init__(
        self,
        store: Store,
        devin: Any,
        settings: Settings,
        github: Any = None,
        now: Callable[[], float] = time.time,
        knowledge: str | None = None,
    ) -> None:
        self.store = store
        self.devin = devin
        self.settings = settings
        self.github = github if github is not None else RecordingGitHub()
        self.now = now
        self._knowledge = knowledge  # None = load from disk each time
        self.syncer = Syncer(store, self.github, settings)

    # ============================================================================
    # intake
    # ============================================================================
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
            guidance = self._build_guidance(task) if state is not State.PROPOSED else ""  # type: ignore[arg-type]
            # Issue text may have been edited to answer a question: take the latest.
            self.store.update_task(
                task["id"], title=title, body=body, current_session_id=None, guidance=guidance, verify_status=""  # type: ignore[index]
            )
            if self.store.transition(task["id"], State.READY, "human re-applied devin:ready", expected=state):  # type: ignore[index]
                if state is State.REJECTED:
                    try:
                        self.github.reopen_issue(repo, issue_number)
                    except GitHubAPIError as e:
                        log.warning("could not reopen #%s: %s", issue_number, e)
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

    def _build_guidance(self, task: dict[str, Any]) -> str:
        """What the next session should know: how the last attempt ended, and the human's replies."""
        sessions = self.store.sessions_for_task(task["id"])
        last = sessions[-1] if sessions else None
        parts: list[str] = []
        if last:
            parts.append(f"Attempt {last['attempt']} ended as '{task['state']}'.")
            if task["last_summary"]:
                parts.append(f"Its summary: {task['last_summary']}")
            if task["blocked_question"]:
                parts.append(f"Devin's question was: {task['blocked_question']}")
        since = last["created_at"] if last else 0
        comments = [
            f"- @{f['author'] or 'human'}: {f['text'].strip()}"
            for f in self.store.feedback_since(since - 1, task["id"])
            if f["kind"] in ("issue_comment", "pr_comment", "review") and f["text"].strip()
        ]
        if comments:
            parts.append("Human comments since then:\n" + "\n".join(comments))
        return "\n".join(parts)[:4000]

    # ============================================================================
    # worker
    # ============================================================================
    def tick(self) -> None:
        self.poll_active()
        self.poll_jobs()
        self.dispatch_ready()
        self.sync()

    def sync(self) -> int:
        try:
            return self.syncer.sync_all()
        except Exception:  # GitHub trouble must never stop the worker
            log.exception("GitHub sync failed")
            return 0

    def sessions_last_24h(self) -> int:
        since = self.now() - DAY_SECONDS
        return self.store.sessions_started_since(since) + self.store.jobs_started_since(since)

    def _knowledge_text(self, kind: str = "fix") -> str:
        if self._knowledge is not None:
            return self._knowledge
        return load_knowledge(self.settings.knowledge_dir, kind)

    def dispatch_ready(self) -> int:
        started = 0
        for task in self.store.list_tasks(State.READY):
            if self.sessions_last_24h() >= self.settings.max_sessions_per_day:
                log.warning("daily session limit (%d) reached; tasks stay in READY", self.settings.max_sessions_per_day)
                break
            if self._start(task):
                started += 1
        return started

    def _start(self, task: dict[str, Any]) -> bool:
        tid = task["id"]
        if not self.store.transition(tid, State.IN_PROGRESS, "claimed for a new session", expected=State.READY):
            return False  # someone else moved it
        attempt = task["attempt"] + 1
        n = task["issue_number"]
        open_prs = [p["pr_url"] for p in self.store.prs_for_task(tid) if p["state"] == "open" and p["pr_url"]]
        for pr in self.store.prs_for_task(tid):
            if pr["state"] == "open":  # a revision will push new commits: any earlier result is stale
                self.store.set_pr_verify(tid, pr["pr_number"], "")
        try:
            resp = self.devin.create_session(
                prompt=build_task_prompt(task, self._knowledge_text("fix"), task.get("guidance", ""), open_prs),
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
                self.store.update_task(tid, last_summary=f"Could not start a Devin session: {e}")
                self.store.transition(tid, State.FAILED, f"could not start session: {e}", expected=State.IN_PROGRESS)
            return False
        sid = resp["session_id"]
        self.store.add_session(
            sid, tid, attempt, resp.get("url", ""), resp.get("status", ""), resp.get("status_detail"), now=int(self.now())
        )
        self.store.update_task(tid, current_session_id=sid, attempt=attempt, verify_status="")
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
        task = self.store.get_task(task_id)
        if task is None or State(task["state"]) is not State.IN_PROGRESS:
            return
        if r.kind == "done":
            self.store.update_task(task_id, pr_urls=r.pr_urls, last_summary=r.summary)
            for url in r.pr_urls:
                parsed = parse_pr_url(url)
                if parsed and parsed[0].lower() == task["repo"].lower():
                    self.store.upsert_pr(task_id, parsed[1], url)
            self.store.update_session(sid, outcome="done", finished_at=int(self.now()))
            self._end_session(sid, wrap_up=True)
            if self.settings.verify_mode == "off":
                self.store.transition(task_id, State.IN_REVIEW, r.summary or "PR opened", expected=State.IN_PROGRESS)
            else:
                self.store.update_task(task_id, verify_status="pending")
                self.store.add_event(task_id, "verification_pending", ", ".join(r.pr_urls))
                self.evaluate_gate(task_id)
        elif r.kind == "blocked":
            question = r.question or self._last_devin_message(sid) or "Devin is waiting for input."
            self.store.update_task(task_id, blocked_question=question, last_summary=r.summary)
            if self.store.transition(task_id, State.BLOCKED, question, expected=State.IN_PROGRESS):
                self.store.update_session(sid, outcome="blocked", finished_at=int(self.now()))
                self._end_session(sid, wrap_up=False)
        else:  # failed
            reason = r.summary or r.reason or "session failed"
            self.store.update_task(task_id, last_summary=reason)
            if self.store.transition(task_id, State.FAILED, reason, expected=State.IN_PROGRESS):
                self.store.update_session(sid, outcome="failed", finished_at=int(self.now()))
                self._end_session(sid, wrap_up=False)

    def _end_session(self, sid: str, wrap_up: bool) -> None:
        """Done: ask Devin to finish (the session exits). Blocked/failed: terminate; a human's
        answer starts a fresh session with the updated issue text."""
        try:
            if wrap_up:
                self.devin.send_message(sid, WRAP_UP_MESSAGE)
            else:
                self.devin.terminate(sid)
        except DevinAPIError as e:
            log.warning("could not end session %s cleanly: %s", sid, e)

    def _stop_session(self, task: dict[str, Any]) -> None:
        sid = task.get("current_session_id")
        sess = self.store.get_session(sid) if sid else None
        if sess and sess["finished_at"] is None:
            self.store.update_session(sid, finished_at=int(self.now()))
            self._end_session(sid, wrap_up=False)

    def _last_devin_message(self, sid: str) -> str:
        try:
            msgs = [m for m in self.devin.list_messages(sid) if m.get("source") == "devin"]
        except DevinAPIError:
            return ""
        return str(msgs[-1].get("message", "")) if msgs else ""

    # ============================================================================
    # verification gate (M6)
    # ============================================================================
    def evaluate_gate(self, task_id: int) -> str:
        """Decide whether a finished session's PR(s) may go to human review."""
        task = self.store.get_task(task_id)
        if task is None or State(task["state"]) is not State.IN_PROGRESS or task["verify_status"] != "pending":
            return "not_pending"
        prs = [p for p in self.store.prs_for_task(task_id) if p["state"] == "open"]
        if not prs:
            return "no_open_prs"
        failed = [p for p in prs if p["verify_status"] == "failed"]
        if failed:
            why = self._verification_failure_text(task, failed[0])
            self.store.update_task(task_id, last_summary=why, verify_status="failed")
            self.store.transition(task_id, State.FAILED, "verification failed", expected=State.IN_PROGRESS)
            return "failed"
        if all(p["verify_status"] == "passed" for p in prs):
            self.store.update_task(task_id, verify_status="passed")
            self.store.transition(task_id, State.IN_REVIEW, "verification passed", expected=State.IN_PROGRESS)
            return "passed"
        return "pending"

    def _verification_failure_text(self, task: dict[str, Any], pr: dict[str, Any]) -> str:
        text = f"The independent verification check failed for PR #{pr['pr_number']}."
        if pr.get("verify_url"):
            text += f" Run: {pr['verify_url']}"
        excerpt = ""
        if pr.get("verify_run_id"):
            try:
                excerpt = self.github.job_log_excerpt(task["repo"], pr["verify_run_id"])
            except Exception:
                excerpt = ""
        if excerpt:
            text += f"\n\nEnd of the failing log:\n```\n{excerpt}\n```"
        return text

    def handle_workflow_run(self, repo: str, run: dict[str, Any]) -> str:
        if run.get("name") != self.settings.verify_workflow_name:
            return "ignored"
        conclusion = run.get("conclusion")
        if conclusion == "cancelled" or conclusion is None:
            return "ignored"
        status = "passed" if conclusion == "success" else "failed"
        sha = run.get("head_sha") or ""
        matched: list[tuple[int, int]] = []  # (task_id, pr_number)
        for p in run.get("pull_requests") or []:
            task = self.store.find_task_by_pr(repo, p.get("number"))
            if task:
                matched.append((task["id"], p["number"]))
        if not matched and sha:
            matched = [(p["task_id"], p["pr_number"]) for p in self.store.find_prs_by_sha(repo, sha)]
        if not matched:
            return "no_matching_task"
        applied = 0
        for task_id, pr_number in matched:
            row = next((p for p in self.store.prs_for_task(task_id) if p["pr_number"] == pr_number), None)
            if row and row["head_sha"] and sha and row["head_sha"] != sha:
                continue  # result for an older commit
            self.store.set_pr_verify(task_id, pr_number, status, run.get("html_url", ""), run.get("id"))
            self.store.add_event(task_id, f"verification_{status}", f"PR #{pr_number} {run.get('html_url', '')}")
            self.evaluate_gate(task_id)
            applied += 1
        return status if applied else "stale"

    # ============================================================================
    # GitHub events (M3)
    # ============================================================================
    def reject(self, task: dict[str, Any], why: str, close_issue: bool = False) -> bool:
        fresh = self.store.get_task(task["id"])
        state = State(fresh["state"])  # type: ignore[index]
        if state in (State.REJECTED, State.COMPLETED):
            return False
        if state is State.IN_PROGRESS:
            self._stop_session(fresh)  # type: ignore[arg-type]
        if not self.store.transition(task["id"], State.REJECTED, why, expected=state):
            return False
        self.store.add_feedback(task["id"], "rejected", "", why, state.value)
        if close_issue:
            try:
                self.github.close_issue(task["repo"], task["issue_number"], "not_planned")
            except GitHubAPIError as e:
                log.warning("could not close #%s: %s", task["issue_number"], e)
        return True

    def complete(self, task: dict[str, Any], why: str) -> bool:
        fresh = self.store.get_task(task["id"])
        state = State(fresh["state"])  # type: ignore[index]
        if state in (State.COMPLETED, State.REJECTED, State.PROPOSED, State.READY):
            return False
        if state is State.IN_PROGRESS:
            self._stop_session(fresh)  # type: ignore[arg-type]
        return self.store.transition(task["id"], State.COMPLETED, why, expected=state)

    def handle_issue_closed(self, repo: str, number: int, reason: str | None) -> str:
        task = self.store.get_task_by_issue(repo, number)
        if task is None:
            return "ignored"
        if reason == "not_planned":
            return "rejected" if self.reject(task, "issue closed as not planned") else "ignored"
        if reason == "completed":
            return "completed" if self.complete(task, "issue closed as completed") else "ignored"
        return "ignored"

    def handle_comment(self, repo: str, number: int, author: str, text: str, on_pr: bool) -> str:
        task = self.store.find_task_by_pr(repo, number) if on_pr else self.store.get_task_by_issue(repo, number)
        if task is None or not text.strip():
            return "ignored"
        self.store.add_feedback(task["id"], "pr_comment" if on_pr else "issue_comment", author, text, task["state"])
        return "recorded"

    def handle_pr_opened(self, repo: str, number: int, url: str, head_sha: str, body: str) -> str:
        linked = False
        for issue_no in parse_closes(body):
            task = self.store.get_task_by_issue(repo, issue_no)
            if task:
                self.store.upsert_pr(task["id"], number, url, head_sha, "open")
                self.store.add_event(task["id"], "pr_opened", url)
                linked = True
        if not linked:
            task = self.store.find_task_by_pr(repo, number)
            if task:
                self.store.upsert_pr(task["id"], number, url, head_sha, "open")
                linked = True
        return "linked" if linked else "ignored"

    def handle_pr_synchronize(self, repo: str, number: int, head_sha: str) -> str:
        task = self.store.find_task_by_pr(repo, number)
        if task is None:
            return "ignored"
        self.store.upsert_pr(task["id"], number, head_sha=head_sha)
        self.store.set_pr_verify(task["id"], number, "")  # result for the old commit is stale
        return "reset_verification"

    def handle_pr_closed(self, repo: str, number: int, merged: bool) -> str:
        task = self.store.find_task_by_pr(repo, number)
        if task is None:
            return "ignored"
        self.store.upsert_pr(task["id"], number, state="merged" if merged else "closed")
        if merged:
            return "completed" if self.complete(task, f"PR #{number} merged") else "ignored"
        if any(p["state"] == "open" for p in self.store.prs_for_task(task["id"])):
            return "ignored"  # another PR for this task is still open
        self.store.add_feedback(task["id"], "pr_closed_unmerged", "", f"PR #{number} was closed without merging", task["state"])
        why = f"PR #{number} closed without merging"
        return "rejected" if self.reject(task, why, close_issue=True) else "ignored"

    def handle_review(self, repo: str, number: int, state: str, body: str, review_id: int | None, author: str) -> str:
        task = self.store.find_task_by_pr(repo, number)
        if task is None:
            return "ignored"
        if body.strip():
            self.store.add_feedback(task["id"], "review", author, body, task["state"])
        if state != "changes_requested":
            return "recorded"
        if State(task["state"]) is not State.IN_REVIEW:
            return "ignored"
        lines = [f"Reviewer @{author} requested changes on PR #{number}:", body.strip()]
        if review_id:
            try:
                for c in self.github.list_review_comments(repo, number, review_id):
                    lines.append(f"- {c.get('path', '?')}:{c.get('line') or c.get('original_line') or '?'}: {c.get('body', '').strip()}")
            except GitHubAPIError:
                pass
        self.store.update_task(task["id"], guidance="\n".join(lines)[:4000], verify_status="")
        ok = self.store.transition(task["id"], State.READY, "changes requested", expected=State.IN_REVIEW)
        if ok:
            self.store.add_event(task["id"], "changes_requested", f"by {author}")
        return "changes_requested" if ok else "ignored"

    # ============================================================================
    # jobs: scan (M7) and learn (M8)
    # ============================================================================
    def _job_guard(self, kind: str) -> dict[str, Any] | None:
        running = [j for j in self.store.running_jobs() if j["kind"] == kind]
        if running:
            return {"status": "already_running", "job": running[0]["id"]}
        if self.sessions_last_24h() >= self.settings.max_sessions_per_day:
            return {"status": "limit_reached", "limit": self.settings.max_sessions_per_day}
        return None

    def start_scan(self) -> dict[str, Any]:
        blocked = self._job_guard("scan")
        if blocked:
            return blocked
        prompt = build_scan_prompt(self.settings.target_repo, self._knowledge_text("scan"), self.settings.scan_max_findings)
        return self._start_job("scan", prompt, f"[scan] {self.settings.target_repo} sweep", SCAN_SCHEMA)

    def start_learn(self) -> dict[str, Any]:
        blocked = self._job_guard("learn")
        if blocked:
            return blocked
        last = self.store.last_job("learn")
        since = last["created_at"] if last else 0
        feedback = [f for f in self.store.feedback_since(since) if f["text"].strip()]
        if not feedback:
            return {"status": "nothing_new"}
        for f in feedback:
            t = self.store.get_task(f["task_id"]) if f["task_id"] else None
            f["task_title"] = f"#{t['issue_number']} {t['title']}" if t else ""
        try:
            rejected = [
                f"{p.get('title', '')}: {(p.get('body') or '')[:300]}"
                for p in self.github.list_closed_unmerged_prs(self.settings.solution_repo, self.settings.knowledge_label)
            ]
        except GitHubAPIError:
            rejected = []
        prompt = build_learn_prompt(
            self.settings.solution_repo, self.settings.knowledge_label, self._knowledge_text("all"), feedback, rejected
        )
        return self._start_job("learn", prompt, f"[learn] knowledge update ({len(feedback)} feedback items)", LEARN_SCHEMA)

    def _start_job(self, kind: str, prompt: str, title: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.devin.create_session(
                prompt=prompt,
                title=title[:120],
                tags=["superset-helper-eng", kind],
                structured_output_schema=schema,
                max_acu_limit=self.settings.max_acu_per_session,
            )
        except DevinAPIError as e:
            return {"status": "error", "detail": str(e)}
        job = self.store.create_job(kind, resp["session_id"], resp.get("url", ""), now=int(self.now()))
        return {"status": "started", "job": job["id"], "url": resp.get("url", "")}

    def poll_jobs(self) -> None:
        for job in self.store.running_jobs():
            try:
                snap = self.devin.get_session(job["session_id"])
            except DevinAPIError as e:
                log.warning("job poll failed: %s", e)
                continue
            r = interpret(snap, require_pr=False)
            if r.kind == "working":
                continue
            if r.kind == "done":
                result = self._finish_scan(r) if job["kind"] == "scan" else self._finish_learn(r)
                self.store.update_job(
                    job["id"], status="done", finished_at=int(self.now()), summary=r.summary, result_json=result
                )
                self._end_session(job["session_id"], wrap_up=True)
            else:
                why = r.question or r.summary or r.reason or "job did not complete"
                self.store.update_job(job["id"], status="failed", finished_at=int(self.now()), summary=why)
                self._end_session(job["session_id"], wrap_up=False)

    def _finish_scan(self, r: Interpretation) -> dict[str, Any]:
        findings = [f for f in (r.data.get("findings") or []) if isinstance(f, dict) and f.get("title")]
        cap = self.settings.scan_max_findings
        repo = self.settings.target_repo
        filed: list[dict[str, Any]] = []
        errors: list[str] = []
        for f in findings[:cap]:
            title = str(f["title"]).strip()[:200]
            body = render_issue_body(f)
            labels = [self.settings.proposed_label]
            if f.get("type") in FINDING_TYPES:
                labels.append(f"type:{f['type']}")
            try:
                issue = self.github.create_issue(repo, title, body, labels)
            except GitHubAPIError as e:
                errors.append(f"{title}: {e}")
                continue
            self.register_proposed(repo, issue["number"], title, body, source="scan")
            filed.append({"issue": issue["number"], "title": title, "url": issue.get("html_url", "")})
        return {
            "filed": filed,
            "skipped": r.data.get("skipped") or [],
            "truncated": max(0, len(findings) - cap),
            "errors": errors,
        }

    def _finish_learn(self, r: Interpretation) -> dict[str, Any]:
        labeled: list[str] = []
        for url in r.pr_urls:
            parsed = parse_pr_url(url)
            if not parsed:
                continue
            repo, number = parsed
            try:
                self.github.ensure_label(repo, self.settings.knowledge_label, "c2e0c6", "Proposed rule changes for knowledge/")
                self.github.add_labels(repo, number, [self.settings.knowledge_label])
                labeled.append(url)
            except GitHubAPIError as e:
                log.warning("could not label %s: %s", url, e)
        return {
            "prs": r.pr_urls,
            "labeled": labeled,
            "rules_proposed": r.data.get("rules_proposed", 0),
            "rules_skipped": r.data.get("rules_skipped") or [],
        }

    # ============================================================================
    # reporting
    # ============================================================================
    def status_summary(self) -> dict[str, Any]:
        tasks = self.store.list_tasks(State.IN_PROGRESS)
        return {
            "counts": self.store.counts_by_state(),
            "verifying": sum(1 for t in tasks if t["verify_status"] == "pending"),
            "sessions_last_24h": self.sessions_last_24h(),
            "max_sessions_per_day": self.settings.max_sessions_per_day,
            "github_sync": "dry-run" if getattr(self.github, "dry_run", False) else "live",
            "verify_mode": self.settings.verify_mode,
            "devin_mode": self.settings.devin_mode,
        }
