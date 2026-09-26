"""SQLite store: tasks, Devin sessions, PRs, an append-only event log, human feedback,
background jobs (scan / learn), and webhook deliveries.

All task state changes go through `transition()`, which is a compare-and-set: it only
applies if the task is still in the state the caller expected. That makes the webhook
thread and the worker loop safe to run concurrently without long locks.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .states import ALLOWED, State


class InvalidTransition(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'human',
    attempt INTEGER NOT NULL DEFAULT 0,
    current_session_id TEXT,
    pr_urls TEXT NOT NULL DEFAULT '[]',
    blocked_question TEXT NOT NULL DEFAULT '',
    last_summary TEXT NOT NULL DEFAULT '',
    guidance TEXT NOT NULL DEFAULT '',
    verify_status TEXT NOT NULL DEFAULT '',
    synced_state TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (repo, issue_number)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    status_detail TEXT,
    outcome TEXT,
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    last_polled_at INTEGER,
    raw_json TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_prs (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    pr_number INTEGER NOT NULL,
    pr_url TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    verify_status TEXT NOT NULL DEFAULT '',
    verify_url TEXT NOT NULL DEFAULT '',
    verify_run_id INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (task_id, pr_number)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    task_state TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    session_id TEXT,
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sync_log (
    task_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (task_id, key)
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
"""

# Columns added after the first release; added to existing databases on startup.
TASK_MIGRATIONS = {
    "guidance": "TEXT NOT NULL DEFAULT ''",
    "verify_status": "TEXT NOT NULL DEFAULT ''",
    "synced_state": "TEXT NOT NULL DEFAULT ''",
}

TASK_FIELDS = {
    "title",
    "body",
    "source",
    "attempt",
    "current_session_id",
    "pr_urls",
    "blocked_question",
    "last_summary",
    "guidance",
    "verify_status",
    "synced_state",
}


def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["pr_urls"] = json.loads(d.get("pr_urls") or "[]")
    return d


class Store:
    def __init__(self, path: str = "data/tasks.db") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
            for name, ddl in TASK_MIGRATIONS.items():
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")

    # ---- tasks -----------------------------------------------------------------
    def create_task(
        self,
        repo: str,
        issue_number: int,
        title: str,
        body: str,
        state: State,
        source: str = "human",
        now: int | None = None,
    ) -> dict[str, Any]:
        """Insert a task. Raises sqlite3.IntegrityError if (repo, issue) already exists."""
        ts = int(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (repo, issue_number, title, body, state, source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (repo, issue_number, title, body, State(state).value, source, ts, ts),
            )
            task_id = cur.lastrowid
            self._event(task_id, "task_created", f"state={State(state).value} source={source}", ts)
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _task(self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def get_task_by_issue(self, repo: str, issue_number: int) -> dict[str, Any] | None:
        with self._lock:
            return _task(
                self._conn.execute(
                    "SELECT * FROM tasks WHERE repo=? AND issue_number=?", (repo, issue_number)
                ).fetchone()
            )

    def list_tasks(self, state: State | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if state is None:
                rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE state=? ORDER BY created_at, id", (State(state).value,)
                ).fetchall()
        return [_task(r) for r in rows]  # type: ignore[misc]

    def unsynced_tasks(self) -> list[dict[str, Any]]:
        """Tasks whose GitHub labels/comments have not caught up with their state."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tasks WHERE synced_state != state ORDER BY id").fetchall()
        return [_task(r) for r in rows]  # type: ignore[misc]

    def update_task(self, task_id: int, **fields: Any) -> None:
        bad = set(fields) - TASK_FIELDS
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if "pr_urls" in fields and not isinstance(fields["pr_urls"], str):
            fields["pr_urls"] = json.dumps(list(fields["pr_urls"]))
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                (*fields.values(), int(time.time()), task_id),
            )

    def transition(
        self,
        task_id: int,
        new_state: State,
        note: str = "",
        expected: State | None = None,
        now: int | None = None,
    ) -> bool:
        """Compare-and-set state change. Returns False if the task was not in `expected`
        (someone else moved it) or is already in `new_state`. Raises InvalidTransition
        for a move the state machine does not allow."""
        new = State(new_state)
        ts = int(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no task {task_id}")
            current = State(row["state"])
            if expected is not None and current != State(expected):
                return False
            if new == current:
                return False
            if new not in ALLOWED[current]:
                raise InvalidTransition(f"{current.value} -> {new.value} is not allowed")
            self._conn.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE id=? AND state=?",
                (new.value, ts, task_id, current.value),
            )
            self._event(task_id, "state_changed", f"{current.value} -> {new.value}: {note}".strip(), ts)
        return True

    def counts_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
        counts = {s.value: 0 for s in State}
        counts.update({r["state"]: r["n"] for r in rows})
        return counts

    # ---- sessions --------------------------------------------------------------
    def add_session(
        self,
        session_id: str,
        task_id: int,
        attempt: int,
        url: str = "",
        status: str = "",
        status_detail: str | None = None,
        now: int | None = None,
    ) -> None:
        ts = int(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, task_id, attempt, url, status, status_detail, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (session_id, task_id, attempt, url, status, status_detail, ts),
            )

    def update_session(self, session_id: str, **fields: Any) -> None:
        allowed = {"status", "status_detail", "outcome", "finished_at", "last_polled_at", "raw_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update session fields: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE sessions SET {sets} WHERE session_id=?", (*fields.values(), session_id))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def sessions_for_task(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE task_id=? ORDER BY created_at, attempt", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def active_sessions(self) -> list[dict[str, Any]]:
        """Unfinished sessions that are the current session of an in-progress task."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.* FROM sessions s JOIN tasks t ON t.current_session_id = s.session_id"
                " WHERE t.state = ? AND s.finished_at IS NULL ORDER BY s.created_at",
                (State.IN_PROGRESS.value,),
            ).fetchall()
        return [dict(r) for r in rows]

    def sessions_started_since(self, ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE created_at >= ?", (int(ts),)
            ).fetchone()
        return int(row["n"])

    # ---- pull requests ---------------------------------------------------------
    def upsert_pr(
        self, task_id: int, pr_number: int, pr_url: str = "", head_sha: str | None = None, state: str | None = None
    ) -> None:
        ts = int(time.time())
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM task_prs WHERE task_id=? AND pr_number=?", (task_id, pr_number)
            ).fetchone()
            if not exists:
                self._conn.execute(
                    "INSERT INTO task_prs (task_id, pr_number, pr_url, head_sha, state, updated_at) VALUES (?,?,?,?,?,?)",
                    (task_id, pr_number, pr_url, head_sha or "", state or "open", ts),
                )
                return
            sets, vals = ["updated_at=?"], [ts]
            if pr_url:
                sets.append("pr_url=?"); vals.append(pr_url)
            if head_sha is not None:
                sets.append("head_sha=?"); vals.append(head_sha)
            if state is not None:
                sets.append("state=?"); vals.append(state)
            self._conn.execute(
                f"UPDATE task_prs SET {', '.join(sets)} WHERE task_id=? AND pr_number=?", (*vals, task_id, pr_number)
            )

    def set_pr_verify(
        self, task_id: int, pr_number: int, status: str, url: str = "", run_id: int | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE task_prs SET verify_status=?, verify_url=?, verify_run_id=?, updated_at=?"
                " WHERE task_id=? AND pr_number=?",
                (status, url, run_id, int(time.time()), task_id, pr_number),
            )

    def prs_for_task(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_prs WHERE task_id=? ORDER BY pr_number", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def find_task_by_pr(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM tasks t JOIN task_prs p ON p.task_id = t.id"
                " WHERE t.repo=? AND p.pr_number=? ORDER BY t.id DESC LIMIT 1",
                (repo, pr_number),
            ).fetchone()
        return _task(row)

    def find_prs_by_sha(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.* FROM task_prs p JOIN tasks t ON t.id = p.task_id"
                " WHERE t.repo=? AND p.head_sha=? AND p.head_sha != ''",
                (repo, head_sha),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- events / feedback / deliveries ----------------------------------------
    def _event(self, task_id: int | None, kind: str, detail: str, ts: int | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events (task_id, ts, kind, detail) VALUES (?,?,?,?)",
            (task_id, int(ts if ts is not None else time.time()), kind, detail),
        )

    def add_event(self, task_id: int | None, kind: str, detail: str = "") -> None:
        with self._lock:
            self._event(task_id, kind, detail)

    def list_events(
        self, task_id: int | None = None, limit: int = 100, ascending: bool = False
    ) -> list[dict[str, Any]]:
        order = "ASC" if ascending else "DESC"
        with self._lock:
            if task_id is None:
                rows = self._conn.execute(f"SELECT * FROM events ORDER BY id {order} LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT * FROM events WHERE task_id=? ORDER BY id {order} LIMIT ?", (task_id, limit)
                ).fetchall()
        return [dict(r) for r in rows]

    def add_feedback(
        self, task_id: int | None, kind: str, author: str, text: str, task_state: str = "", now: int | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO feedback (task_id, ts, kind, author, text, task_state) VALUES (?,?,?,?,?,?)",
                (task_id, int(now if now is not None else time.time()), kind, author, text, task_state),
            )

    def feedback_since(self, ts: int, task_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if task_id is None:
                rows = self._conn.execute("SELECT * FROM feedback WHERE ts > ? ORDER BY ts, id", (int(ts),)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM feedback WHERE ts > ? AND task_id=? ORDER BY ts, id", (int(ts), task_id)
                ).fetchall()
        return [dict(r) for r in rows]

    def has_delivery(self, delivery_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
                is not None
            )

    def record_delivery(self, delivery_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries (delivery_id, ts) VALUES (?,?)",
                (delivery_id, int(time.time())),
            )

    # ---- one-time GitHub comments ----------------------------------------------
    def claim_sync_key(self, task_id: int, key: str) -> bool:
        """True the first time (task, key) is claimed; used so each comment is posted once."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO sync_log (task_id, key, ts) VALUES (?,?,?)", (task_id, key, int(time.time()))
            )
            return cur.rowcount == 1

    def release_sync_key(self, task_id: int, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sync_log WHERE task_id=? AND key=?", (task_id, key))

    # ---- jobs (scan / learn) ---------------------------------------------------
    def create_job(self, kind: str, session_id: str | None, url: str = "", now: int | None = None) -> dict[str, Any]:
        ts = int(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (kind, session_id, url, created_at) VALUES (?,?,?,?)", (kind, session_id, url, ts)
            )
            return self.get_job(cur.lastrowid)  # type: ignore[arg-type,return-value]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row)

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {"status", "finished_at", "summary", "result_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update job fields: {sorted(bad)}")
        if "result_json" in fields and not isinstance(fields["result_json"], str):
            fields["result_json"] = json.dumps(fields["result_json"])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def list_jobs(self, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._job(r) for r in rows]  # type: ignore[misc]

    def running_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE status='running' ORDER BY id").fetchall()
        return [self._job(r) for r in rows]  # type: ignore[misc]

    def last_job(self, kind: str, statuses: tuple[str, ...] = ("running", "done")) -> dict[str, Any] | None:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM jobs WHERE kind=? AND status IN ({marks}) ORDER BY id DESC LIMIT 1", (kind, *statuses)
            ).fetchone()
        return self._job(row)

    def jobs_started_since(self, ts: float) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE created_at >= ?", (int(ts),)).fetchone()
        return int(row["n"])

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        try:
            d["result"] = json.loads(d.get("result_json") or "{}")
        except json.JSONDecodeError:
            d["result"] = {}
        return d
