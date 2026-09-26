"""Database migration and the prompt builders."""
import os
import pathlib
import sqlite3
import tempfile
import unittest

from app.db import Store
from app.prompts import build_learn_prompt, build_scan_prompt, build_task_prompt, load_knowledge
from app.states import State

OLD_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'human', attempt INTEGER NOT NULL DEFAULT 0, current_session_id TEXT,
    pr_urls TEXT NOT NULL DEFAULT '[]', blocked_question TEXT NOT NULL DEFAULT '',
    last_summary TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE (repo, issue_number));
"""


class MigrationTests(unittest.TestCase):
    def test_an_existing_database_from_the_first_release_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tasks.db")
            conn = sqlite3.connect(path)
            conn.executescript(OLD_SCHEMA)
            conn.execute("INSERT INTO tasks (repo, issue_number, title, state, created_at, updated_at) VALUES ('o/r', 1, 'old', 'ready', 1, 1)")
            conn.commit()
            conn.close()
            store = Store(path)  # must not raise
            t = store.get_task_by_issue("o/r", 1)
            self.assertEqual((t["title"], t["guidance"], t["verify_status"], t["synced_state"]), ("old", "", "", ""))
            store.update_task(t["id"], guidance="g")  # new columns are usable
            self.assertEqual(store.get_task(t["id"])["guidance"], "g")
            Store(path)  # and reopening is idempotent


class StoreExtras(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.t = self.s.create_task("o/r", 1, "t", "b", State.IN_PROGRESS)

    def test_active_sessions_excludes_finished_ones(self):
        self.s.add_session("s1", self.t["id"], 1)
        self.s.update_task(self.t["id"], current_session_id="s1")
        self.assertEqual(len(self.s.active_sessions()), 1)
        self.s.update_session("s1", finished_at=5)
        self.assertEqual(self.s.active_sessions(), [])

    def test_pr_upsert_and_lookup(self):
        self.s.upsert_pr(self.t["id"], 9, "https://github.com/o/r/pull/9", "sha")
        self.s.upsert_pr(self.t["id"], 9, state="merged")  # partial update keeps the rest
        pr = self.s.prs_for_task(self.t["id"])[0]
        self.assertEqual((pr["pr_url"], pr["head_sha"], pr["state"]), ("https://github.com/o/r/pull/9", "sha", "merged"))
        self.assertEqual(self.s.find_task_by_pr("o/r", 9)["id"], self.t["id"])
        self.assertIsNone(self.s.find_task_by_pr("other/repo", 9))
        self.assertEqual(len(self.s.find_prs_by_sha("o/r", "sha")), 1)
        self.assertEqual(self.s.find_prs_by_sha("o/r", ""), [])

    def test_sync_keys_are_claimed_once(self):
        self.assertTrue(self.s.claim_sync_key(1, "blocked:1"))
        self.assertFalse(self.s.claim_sync_key(1, "blocked:1"))
        self.s.release_sync_key(1, "blocked:1")
        self.assertTrue(self.s.claim_sync_key(1, "blocked:1"))

    def test_unsynced_tasks(self):
        self.assertEqual(len(self.s.unsynced_tasks()), 1)
        self.s.update_task(self.t["id"], synced_state="in-progress")
        self.assertEqual(self.s.unsynced_tasks(), [])

    def test_jobs_roundtrip(self):
        j = self.s.create_job("scan", "sess", "u", now=100)
        self.s.update_job(j["id"], status="done", result_json={"filed": [1]})
        got = self.s.get_job(j["id"])
        self.assertEqual((got["status"], got["result"]), ("done", {"filed": [1]}))
        self.assertEqual(self.s.last_job("scan")["id"], j["id"])
        self.assertIsNone(self.s.last_job("learn"))
        self.assertEqual(self.s.jobs_started_since(50), 1)


class PromptTests(unittest.TestCase):
    TASK = {"repo": "o/r", "issue_number": 5, "title": "Fix it", "body": "```verify\nnpm audit\n```"}

    def test_fix_prompt_has_triage_first_and_the_one_issue_rule(self):
        p = build_task_prompt(self.TASK)
        for needle in ("TRIAGE FIRST", "Closes #5", "One issue, one session", "Never use `npm audit fix --force`", "Do NOT merge"):
            self.assertIn(needle, p)
        self.assertNotIn("REVISION", p)

    def test_guidance_and_knowledge_are_included_when_given(self):
        p = build_task_prompt(self.TASK, knowledge="RULE-ONE", guidance="Try the config option")
        self.assertIn("RULE-ONE", p)
        self.assertIn("Try the config option", p)

    def test_revision_prompt_skips_triage_and_reuses_the_pr(self):
        p = build_task_prompt(self.TASK, guidance="fix the test", open_prs=["https://github.com/o/r/pull/9"])
        self.assertIn("This is a REVISION", p)
        self.assertNotIn("TRIAGE FIRST", p)
        self.assertIn("pull/9", p)

    def test_scan_prompt_forbids_changes_and_states_the_verify_rules(self):
        p = build_scan_prompt("o/r", "KNOW", 5)
        for needle in ("Do NOT change any code", "at most 5", "KNOW", "Only these programs are allowed", "skipped"):
            self.assertIn(needle, p)

    def test_learn_prompt_lists_feedback_existing_rules_and_rejections(self):
        fb = [{"kind": "issue_comment", "author": "al", "text": "prefer overrides", "task_title": "#1 x", "task_state": "blocked", "task_id": 1}]
        p = build_learn_prompt("o/sol", "devin:knowledge-base-improvement", "EXISTING", fb, ["old proposal"])
        for needle in ("prefer overrides", "(state then: blocked)", "EXISTING", "old proposal", "o/sol", "ONE pull request", "knowledge/"):
            self.assertIn(needle, p)

    def test_knowledge_loader_kinds(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("README", "ignore me"), ("scan", "SCANRULES"), ("triage-and-fix", "FIXRULES"), ("extra", "EXTRA")):
                pathlib.Path(d, f"{name}.md").write_text(text)
            fix, scan, allk = load_knowledge(d, "fix"), load_knowledge(d, "scan"), load_knowledge(d, "all")
            self.assertIn("FIXRULES", fix); self.assertIn("EXTRA", fix); self.assertNotIn("SCANRULES", fix)
            self.assertIn("SCANRULES", scan); self.assertIn("FIXRULES", scan); self.assertNotIn("EXTRA", scan)
            self.assertIn("SCANRULES", allk); self.assertIn("EXTRA", allk)
            self.assertNotIn("ignore me", allk)
        self.assertEqual(load_knowledge("/nonexistent/dir"), "")


if __name__ == "__main__":
    unittest.main()
