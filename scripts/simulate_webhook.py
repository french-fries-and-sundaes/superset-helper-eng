#!/usr/bin/env python3
"""Send a correctly signed, GitHub-shaped webhook to the running service.

No GitHub, no tunnel needed. Uses only the standard library.

    python scripts/simulate_webhook.py --issue 1 --title "Override underscore"
    python scripts/simulate_webhook.py --issue 2 --title "[sim:blocked] Paramiko SHA-1"
    python scripts/simulate_webhook.py --issue 3 --title "[sim:fail] Too broad" --twice   # tests idempotency
    python scripts/simulate_webhook.py --issue 4 --title "Scanner finding" --label devin:proposed

The secret is read from GITHUB_WEBHOOK_SECRET (same value the service uses).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid


def send(url: str, secret: str, event: str, payload: dict, delivery: str) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000/webhook/github")
    ap.add_argument("--repo", default=os.environ.get("TARGET_REPO", "french-fries-and-sundaes/superset"))
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="Simulated issue.\n\n```verify\nnpm audit\n```")
    ap.add_argument("--label", default="devin:ready")
    ap.add_argument("--action", choices=["labeled", "opened"], default="labeled")
    ap.add_argument("--twice", action="store_true", help="resend the SAME delivery to test idempotency")
    ap.add_argument("--bad-signature", action="store_true", help="sign with the wrong secret (expect 401)")
    args = ap.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("Set GITHUB_WEBHOOK_SECRET (same value the service uses).", file=sys.stderr)
        return 2

    issue = {
        "number": args.issue,
        "title": args.title,
        "body": args.body,
        "labels": [{"name": args.label}],
        "state": "open",
    }
    payload = {"action": args.action, "issue": issue, "repository": {"full_name": args.repo}}
    if args.action == "labeled":
        payload["label"] = {"name": args.label}

    delivery = str(uuid.uuid4())
    use_secret = "wrong-secret" if args.bad_signature else secret
    for i in range(2 if args.twice else 1):
        status, text = send(args.url, use_secret, "issues", payload, delivery)
        print(f"[{i + 1}] HTTP {status}: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
