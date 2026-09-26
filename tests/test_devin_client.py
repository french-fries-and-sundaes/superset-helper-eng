import json
import unittest

import httpx

from app.devin_client import DevinAPIError, DevinClient, api_session_id, bare_session_id


def make(handler, **kw):
    return DevinClient("cog_test", "org-1", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


class ClientTests(unittest.TestCase):
    def test_session_id_forms(self):
        self.assertEqual(api_session_id("abc"), "devin-abc")
        self.assertEqual(api_session_id("devin-abc"), "devin-abc")
        self.assertEqual(bare_session_id("devin-abc"), "abc")

    def test_create_session_sends_expected_body_and_auth(self):
        seen = {}

        def handler(req: httpx.Request):
            seen["url"] = str(req.url)
            seen["auth"] = req.headers["authorization"]
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"session_id": "abc", "url": "u", "status": "running"})

        c = make(handler)
        out = c.create_session("p", "t", tags=["a"], structured_output_schema={"type": "object"}, max_acu_limit=5)
        self.assertEqual(out["session_id"], "abc")
        self.assertEqual(seen["url"], "https://api.devin.ai/v3/organizations/org-1/sessions")
        self.assertEqual(seen["auth"], "Bearer cog_test")
        self.assertEqual(seen["body"]["max_acu_limit"], 5)
        self.assertTrue(seen["body"]["structured_output_required"])
        self.assertNotIn("repos", seen["body"])

    def test_get_session_uses_prefixed_path(self):
        seen = {}

        def handler(req):
            seen["path"] = req.url.path
            return httpx.Response(200, json={"status": "running"})

        make(handler).get_session("abc123")
        self.assertEqual(seen["path"], "/v3/organizations/org-1/sessions/devin-abc123")

    def test_terminate_already_exited_is_success(self):
        def handler(req):
            return httpx.Response(400, json={"title": "Bad Request", "status": 400, "detail": "Devin session already exited"})

        self.assertEqual(make(handler).terminate("abc"), {"already_exited": True})

    def test_terminate_other_400_raises(self):
        def handler(req):
            return httpx.Response(400, json={"detail": "something else"})

        with self.assertRaises(DevinAPIError):
            make(handler).terminate("abc")

    def test_retries_on_429_then_succeeds(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, json={"detail": "slow down"})
            return httpx.Response(200, json={"status": "running"})

        self.assertEqual(make(handler).get_session("abc")["status"], "running")
        self.assertEqual(calls["n"], 3)

    def test_403_raises_with_detail_and_is_not_retryable(self):
        def handler(req):
            return httpx.Response(403, json={"title": "Forbidden", "status": 403, "detail": "Unauthorized"})

        with self.assertRaises(DevinAPIError) as cm:
            make(handler).get_session("abc")
        self.assertEqual(cm.exception.status, 403)
        self.assertFalse(cm.exception.retryable)
        self.assertIn("Unauthorized", cm.exception.detail)

    def test_list_messages_follows_cursor(self):
        pages = [
            {"items": [{"source": "user", "message": "a"}], "end_cursor": "c1", "has_next_page": True},
            {"items": [{"source": "devin", "message": "b"}], "end_cursor": None, "has_next_page": False},
        ]
        seen = []

        def handler(req):
            seen.append(dict(req.url.params))
            return httpx.Response(200, json=pages[len(seen) - 1])

        msgs = make(handler).list_messages("abc")
        self.assertEqual([m["message"] for m in msgs], ["a", "b"])
        self.assertEqual(seen[1], {"after": "c1"})


if __name__ == "__main__":
    unittest.main()
