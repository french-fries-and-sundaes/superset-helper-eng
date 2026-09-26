import sqlite3
import unittest

from app.db import InvalidTransition, Store
from app.states import State


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.t = self.s.create_task("o/r", 1, "title", "body", State.READY)

    def test_unique_per_issue(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.create_task("o/r", 1, "again", "", State.READY)

    def test_transition_is_compare_and_set(self):
        tid = self.t["id"]
        self.assertTrue(self.s.transition(tid, State.IN_PROGRESS, expected=State.READY))
        # A second caller that still thinks it is READY loses the race.
        self.assertFalse(self.s.transition(tid, State.IN_PROGRESS, expected=State.READY))
        self.assertEqual(self.s.get_task(tid)["state"], "in-progress")

    def test_illegal_transition_raises(self):
        with self.assertRaises(InvalidTransition):
            self.s.transition(self.t["id"], State.COMPLETED)  # ready -> completed is not allowed

    def test_events_record_transitions(self):
        self.s.transition(self.t["id"], State.IN_PROGRESS, "claimed")
        kinds = [e["kind"] for e in self.s.list_events(self.t["id"])]
        self.assertIn("state_changed", kinds)

    def test_pr_urls_roundtrip(self):
        self.s.update_task(self.t["id"], pr_urls=["https://x/pull/1"])
        self.assertEqual(self.s.get_task(self.t["id"])["pr_urls"], ["https://x/pull/1"])

    def test_update_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            self.s.update_task(self.t["id"], state="completed")  # must go through transition()

    def test_sessions_started_since(self):
        self.s.add_session("s1", self.t["id"], 1, now=1000)
        self.s.add_session("s2", self.t["id"], 2, now=5000)
        self.assertEqual(self.s.sessions_started_since(2000), 1)
        self.assertEqual(self.s.sessions_started_since(0), 2)

    def test_deliveries(self):
        self.assertFalse(self.s.has_delivery("d1"))
        self.s.record_delivery("d1")
        self.assertTrue(self.s.has_delivery("d1"))

    def test_counts_include_every_state(self):
        counts = self.s.counts_by_state()
        self.assertEqual(counts["ready"], 1)
        self.assertEqual(counts["completed"], 0)


if __name__ == "__main__":
    unittest.main()
