"""M3: mirroring task state onto GitHub labels and one-time comments."""
import unittest

from app.github_client import GitHubAPIError, RecordingGitHub
from tests.helpers import REPO, build, run_ticks, state_of, task, to_review


def comments(gh):
    return [kw["body"] for name, kw in gh.calls if name == "comment"]


class SyncTests(unittest.TestCase):
    def test_labels_follow_the_state_and_only_one_status_label_remains(self):
        orch, store, _, gh, _ = build()
        to_review(orch, store, 1)
        self.assertEqual(gh.get_labels(REPO, 1), ["devin:in-review"])

    def test_each_transition_posts_one_comment_with_the_session_link(self):
        orch, store, _, gh, _ = build()
        to_review(orch, store, 1)
        bodies = comments(gh)
        self.assertTrue(any("started working" in b and "https://app.devin.ai/sessions/fake0001" in b for b in bodies))
        self.assertTrue(any("ready for review" in b for b in bodies))
        n = len(bodies)
        orch.tick()  # a further tick does not repeat anything
        self.assertEqual(len(comments(gh)), n)

    def test_every_comment_carries_the_bot_marker_in_the_real_client_only(self):
        # The marker is added by GitHubClient.comment (covered in test_github_client);
        # here we just confirm bodies are non-empty text.
        orch, store, _, gh, _ = build()
        to_review(orch, store, 1)
        self.assertTrue(all(b.strip() for b in comments(gh)))

    def test_blocked_comment_states_the_question_and_how_to_unblock(self):
        orch, store, _, gh, _ = build()
        orch.request_ready(REPO, 2, "[sim:blocked] Paramiko", "")
        run_ticks(orch)
        body = next(b for b in comments(gh) if "needs your input" in b)
        self.assertIn("Simulated question: which mitigation do you prefer?", body)
        self.assertIn("re-apply the `devin:ready` label", body)
        self.assertEqual(gh.get_labels(REPO, 2), ["devin:blocked"])

    def test_failed_comment_explains_the_retry_path(self):
        orch, store, _, gh, _ = build()
        orch.request_ready(REPO, 3, "[sim:fail] Too broad", "")
        run_ticks(orch)
        body = next(b for b in comments(gh) if "could not complete" in b)
        self.assertIn("re-apply `devin:ready`", body)

    def test_completed_removes_all_status_labels(self):
        orch, store, _, gh, _ = build()
        to_review(orch, store, 1)
        orch.handle_pr_closed(REPO, 101, merged=True)
        orch.sync()
        self.assertEqual(gh.get_labels(REPO, 1), [])

    def test_a_github_outage_delays_the_mirror_and_the_next_tick_catches_up(self):
        class Flaky(RecordingGitHub):
            fail = True

            def set_status_label(self, repo, number, target):
                if self.fail:
                    raise GitHubAPIError(502, "bad gateway")
                super().set_status_label(repo, number, target)

        orch, store, _, _, _ = build()
        gh = Flaky()
        orch.github = gh
        orch.syncer.github = gh
        orch.request_ready(REPO, 1, "x", "")
        orch.tick()
        self.assertEqual(task(store, 1)["synced_state"], "")  # not synced while GitHub is down
        self.assertTrue(any(e["kind"] == "github_sync_failed" for e in store.list_events(task(store, 1)["id"])))
        gh.fail = False
        orch.tick()
        self.assertEqual(task(store, 1)["synced_state"], task(store, 1)["state"])

    def test_a_failed_comment_is_retried_not_lost(self):
        class CommentFlaky(RecordingGitHub):
            fail = True

            def comment(self, repo, number, body):
                if self.fail:
                    raise GitHubAPIError(500, "boom")
                return super().comment(repo, number, body)

        orch, store, _, _, _ = build()
        gh = CommentFlaky()
        orch.github = orch.syncer.github = gh
        orch.request_ready(REPO, 1, "x", "")
        orch.tick()
        self.assertEqual(comments(gh), [])
        gh.fail = False
        orch.tick()
        self.assertEqual(len(comments(gh)), 1)  # posted exactly once after recovery

    def test_in_progress_sync_waits_until_the_session_exists(self):
        orch, store, *_ = build()
        orch.request_ready(REPO, 1, "x", "")
        t = task(store, 1)
        store.transition(t["id"], __import__("app.states", fromlist=["State"]).State.IN_PROGRESS, "manual")
        self.assertFalse(orch.syncer.sync_task(task(store, 1)))  # no session yet: do not comment or mark synced

    def test_requeue_after_blocked_posts_a_new_started_comment_for_the_new_attempt(self):
        orch, store, _, gh, _ = build()
        orch.request_ready(REPO, 2, "[sim:blocked] P", "")
        run_ticks(orch)
        orch.request_ready(REPO, 2, "P (answered)", "answer")
        run_ticks(orch, 2)
        started = [b for b in comments(gh) if "started working" in b]
        self.assertEqual(len(started), 2)
        self.assertIn("attempt 2", started[1])


if __name__ == "__main__":
    unittest.main()
