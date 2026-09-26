#!/usr/bin/env python3
"""Send correctly signed, GitHub-shaped webhooks to the running service, so the WHOLE workflow
can be demonstrated with no GitHub, no tunnel, and (in fake mode) no Devin. Standard library only.

  label        a human applies a label to an issue (default command)
  comment      a human comments on an issue or PR
  close-issue  a human closes an issue ("not planned" = reject)
  pr-open      a pull request is opened (links to the issue via "Closes #N")
  pr-close     a pull request is merged or closed without merging
  review       a reviewer submits a review (changes_requested sends feedback back to Devin)
  workflow-run the GitHub Actions verification finishes (success / failure)

Examples (fake mode: fix PRs are numbered issue+100, so issue 1 -> PR 101):
  python3 scripts/simulate_webhook.py label --issue 1 --title "Override underscore"
  python3 scripts/simulate_webhook.py label --issue 2 --title "[sim:blocked] Paramiko SHA-1"
  python3 scripts/simulate_webhook.py label --issue 4 --title "Scanner finding" --label devin:proposed
  python3 scripts/simulate_webhook.py workflow-run --pr 101 --conclusion success
  python3 scripts/simulate_webhook.py review --pr 101 --state changes_requested --body "Please add a test"
  python3 scripts/simulate_webhook.py pr-close --pr 101 --merged
  python3 scripts/simulate_webhook.py comment --issue 2 --body "Use the config mitigation"
  python3 scripts/simulate_webhook.py close-issue --issue 3 --reason not_planned
  python3 scripts/simulate_webhook.py label --issue 1 --title x --twice        # idempotency
  python3 scripts/simulate_webhook.py label --issue 1 --title x --bad-signature  # expect 401

Old-style calls without a command (`--issue 1 --title ...`) still work and mean `label`.
The secret comes from GITHUB_WEBHOOK_SECRET (the same value the service uses).
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

BOT_MARKER = "<!-- superset-helper-eng:bot -->"


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


def build_payload(args: argparse.Namespace) -> tuple[str, dict]:
    repo = {"full_name": args.repo}
    cmd = args.cmd
    if cmd == "label":
        issue = {"number": args.issue, "title": args.title, "body": args.body, "labels": [{"name": args.label}], "state": "open"}
        payload = {"action": args.action, "issue": issue, "repository": repo}
        if args.action == "labeled":
            payload["label"] = {"name": args.label}
        return "issues", payload
    if cmd == "comment":
        number = args.pr if args.pr else args.issue
        issue = {"number": number}
        if args.pr:
            issue["pull_request"] = {"url": "x"}
        text = args.body + (("\n\n" + BOT_MARKER) if args.as_bot else "")
        return "issue_comment", {"action": "created", "issue": issue, "repository": repo,
                                 "comment": {"body": text, "user": {"login": args.author, "type": "User"}}}
    if cmd == "close-issue":
        issue = {"number": args.issue, "title": "", "body": "", "labels": [], "state": "closed", "state_reason": args.reason}
        return "issues", {"action": "closed", "issue": issue, "repository": repo}
    if cmd == "pr-open":
        body = args.body or f"Closes #{args.issue}"
        pr = {"number": args.pr, "html_url": f"https://github.com/{args.repo}/pull/{args.pr}", "body": body,
              "merged": False, "head": {"sha": args.sha}}
        return "pull_request", {"action": args.action, "number": args.pr, "pull_request": pr, "repository": repo}
    if cmd == "pr-close":
        pr = {"number": args.pr, "html_url": f"https://github.com/{args.repo}/pull/{args.pr}", "body": "",
              "merged": args.merged, "head": {"sha": args.sha}}
        return "pull_request", {"action": "closed", "number": args.pr, "pull_request": pr, "repository": repo}
    if cmd == "review":
        return "pull_request_review", {
            "action": "submitted", "repository": repo, "pull_request": {"number": args.pr},
            "review": {"id": 1, "state": args.state, "body": args.body, "user": {"login": args.author}},
        }
    if cmd == "workflow-run":
        run = {"id": args.run_id, "name": args.workflow, "conclusion": args.conclusion, "head_sha": args.sha,
               "html_url": f"https://github.com/{args.repo}/actions/runs/{args.run_id}",
               "pull_requests": [{"number": args.pr}] if args.pr else []}
        return "workflow_run", {"action": "completed", "workflow_run": run, "repository": repo}
    raise SystemExit(f"unknown command {cmd}")


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default="http://localhost:8000/webhook/github")
    common.add_argument("--repo", default=os.environ.get("TARGET_REPO", "french-fries-and-sundaes/superset"))
    common.add_argument("--twice", action="store_true", help="resend the SAME delivery to test idempotency")
    common.add_argument("--bad-signature", action="store_true", help="sign with the wrong secret (expect 401)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = lambda name: sub.add_parser(name, parents=[common])  # noqa: E731

    p = add("label")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="Simulated issue.\n\n```verify\nnpm audit\n```")
    p.add_argument("--label", default="devin:ready")
    p.add_argument("--action", choices=["labeled", "opened"], default="labeled")

    p = add("comment")
    p.add_argument("--issue", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--body", required=True)
    p.add_argument("--author", default="alice")
    p.add_argument("--as-bot", action="store_true", help="include the bot marker (should be ignored)")

    p = add("close-issue")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reason", choices=["not_planned", "completed"], default="not_planned")

    p = add("pr-open")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--body", default="")
    p.add_argument("--sha", default="sha1")
    p.add_argument("--action", choices=["opened", "reopened", "synchronize"], default="opened")

    p = add("pr-close")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--merged", action="store_true")
    p.add_argument("--sha", default="sha1")

    p = add("review")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--state", choices=["approved", "changes_requested", "commented"], default="changes_requested")
    p.add_argument("--body", default="Please address this.")
    p.add_argument("--author", default="reviewer")

    p = add("workflow-run")
    p.add_argument("--pr", type=int)
    p.add_argument("--conclusion", choices=["success", "failure", "cancelled"], default="success")
    p.add_argument("--sha", default="sha1")
    p.add_argument("--run-id", type=int, default=1001)
    p.add_argument("--workflow", default="devin-verify")
    return ap


def main() -> int:
    argv = sys.argv[1:]
    known = {"label", "comment", "close-issue", "pr-open", "pr-close", "review", "workflow-run", "-h", "--help"}
    if not argv or argv[0] not in known:
        argv = ["label"] + argv  # backward compatible: no command means `label`
    args = parser().parse_args(argv)

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("Set GITHUB_WEBHOOK_SECRET (same value the service uses).", file=sys.stderr)
        return 2
    event, payload = build_payload(args)
    delivery = str(uuid.uuid4())
    use_secret = "wrong-secret" if args.bad_signature else secret
    for i in range(2 if args.twice else 1):
        status, text = send(args.url, use_secret, event, payload, delivery)
        print(f"[{i + 1}] {event} -> HTTP {status}: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
