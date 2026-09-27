import json
import unittest

import httpx

from app.github_client import BOT_MARKER, GitHubAPIError, GitHubClient, RecordingGitHub, make_github, parse_pr_url


def make(handler):
    return GitHubClient("ghp_test", transport=httpx.MockTransport(handler))


class ClientTests(unittest.TestCase):
    def test_comment_appends_the_bot_marker_and_sends_auth(self):
        seen = {}

        def handler(req):
            seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
            return httpx.Response(201, json={"id": 1})

        make(handler).comment("o/r", 7, "hello")
        self.assertEqual(seen["url"], "https://api.github.com/repos/o/r/issues/7/comments")
        self.assertEqual(seen["auth"], "Bearer ghp_test")
        self.assertTrue(seen["body"]["body"].startswith("hello"))
        self.assertIn(BOT_MARKER, seen["body"]["body"])

    def test_set_status_label_removes_other_status_labels_keeps_unrelated_and_adds_target(self):
        calls = []

        def handler(req):
            calls.append((req.method, req.url.raw_path.decode()))
            if req.method == "GET":
                return httpx.Response(200, json=[{"name": "devin:blocked"}, {"name": "type:tests"}, {"name": "bug"}])
            return httpx.Response(200, json=[])

        make(handler).set_status_label("o/r", 3, "devin:ready")
        self.assertIn(("DELETE", "/repos/o/r/issues/3/labels/devin%3Ablocked"), calls)
        self.assertIn(("POST", "/repos/o/r/issues/3/labels"), calls)
        self.assertFalse(any("type" in path or "bug" in path for m, path in calls if m == "DELETE"))

    def test_set_status_label_is_a_noop_when_already_correct(self):
        calls = []

        def handler(req):
            calls.append(req.method)
            return httpx.Response(200, json=[{"name": "devin:ready"}])

        make(handler).set_status_label("o/r", 3, "devin:ready")
        self.assertEqual(calls, ["GET"])

    def test_set_status_label_none_clears_status_labels(self):
        calls = []

        def handler(req):
            calls.append(req.method)
            return httpx.Response(200, json=[{"name": "devin:in-review"}])

        make(handler).set_status_label("o/r", 3, None)
        self.assertEqual(calls, ["GET", "DELETE"])

    def test_remove_missing_label_is_fine(self):
        make(lambda req: httpx.Response(404, json={"message": "Label does not exist"})).remove_label("o/r", 1, "x")

    def test_ensure_label_tolerates_already_exists(self):
        make(lambda req: httpx.Response(422, json={"message": "Validation Failed"})).ensure_label("o/r", "devin:ready")

    def test_errors_carry_status_and_message(self):
        with self.assertRaises(GitHubAPIError) as cm:
            make(lambda req: httpx.Response(403, json={"message": "Resource not accessible by personal access token"})).create_issue("o/r", "t", "b", [])
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("not accessible", cm.exception.detail)

    def test_close_issue_sends_reason(self):
        seen = {}

        def handler(req):
            seen.update(method=req.method, body=json.loads(req.content))
            return httpx.Response(200, json={})

        make(handler).close_issue("o/r", 4, "not_planned")
        self.assertEqual((seen["method"], seen["body"]), ("PATCH", {"state": "closed", "state_reason": "not_planned"}))

    def test_closed_unmerged_prs_filters_by_label_and_merge(self):
        prs = [
            {"number": 1, "merged_at": None, "labels": [{"name": "devin:knowledge-base-improvement"}]},
            {"number": 2, "merged_at": "2026-01-01", "labels": [{"name": "devin:knowledge-base-improvement"}]},
            {"number": 3, "merged_at": None, "labels": [{"name": "other"}]},
        ]
        got = make(lambda req: httpx.Response(200, json=prs)).list_closed_unmerged_prs("o/s", "devin:knowledge-base-improvement")
        self.assertEqual([p["number"] for p in got], [1])

    def test_log_excerpt_never_raises(self):
        self.assertEqual(make(lambda req: httpx.Response(500, json={})).job_log_excerpt("o/r", 1), "")

    def test_parse_pr_url(self):
        self.assertEqual(parse_pr_url("https://github.com/o/r/pull/12"), ("o/r", 12))
        self.assertIsNone(parse_pr_url("https://example.com/x"))


class RecordingTests(unittest.TestCase):
    def test_no_token_means_dry_run(self):
        gh = make_github("")
        self.assertIsInstance(gh, RecordingGitHub)
        self.assertTrue(gh.dry_run)
        self.assertFalse(make_github("ghp_x").dry_run)

    def test_create_issue_returns_increasing_numbers(self):
        gh = RecordingGitHub()
        a = gh.create_issue("o/r", "t", "b", ["devin:proposed"])["number"]
        b = gh.create_issue("o/r", "t2", "b", [])["number"]
        self.assertEqual(b, a + 1)


if __name__ == "__main__":
    unittest.main()


class MergedPrListingTests(unittest.TestCase):
    def test_only_merged_pull_requests_from_devin_branches_are_returned(self):
        def pr(n, ref, merged):
            return {"number": n, "html_url": f"u{n}", "title": f"t{n}", "body": f"Closes #{n}",
                    "head": {"ref": ref, "sha": f"sha{n}"}, "merged_at": merged}

        def handler(req):
            assert req.url.params["state"] == "closed"
            return httpx.Response(200, json=[
                pr(1, "devin/123-fix", "2026-09-27T00:10:00Z"),
                pr(2, "devin/456-closed-unmerged", None),
                pr(3, "feature/human", "2026-09-27T00:11:00Z"),
            ])

        out = make(handler).list_merged_prs_from_branch("o/r", "devin/")
        self.assertEqual([p["number"] for p in out], [1])
        self.assertEqual(out[0]["merged_at"], 1790467800)
        self.assertEqual(out[0]["head_sha"], "sha1")
