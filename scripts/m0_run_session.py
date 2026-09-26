#!/usr/bin/env python3
"""M0: talk to the real Devin API from a script. No database, no webhook.

Creates ONE session for a prompt, polls it, prints every status change, and (once Devin
reports an outcome) sends the wrap-up message so the session exits.

    export DEVIN_API_KEY=cog_...  DEVIN_ORG_ID=org-...
    python scripts/m0_run_session.py --title "probe" --prompt "Do not change code. Ask me red or blue."
    python scripts/m0_run_session.py --issue-file issue1.md --title "#1 underscore override"

Tip: cap spend while experimenting with --max-acu 2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.devin_client import DevinClient, TERMINAL_STATUSES  # noqa: E402
from app.outcome import interpret  # noqa: E402
from app.prompts import OUTPUT_SCHEMA, WRAP_UP_MESSAGE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt")
    g.add_argument("--issue-file", help="file whose text becomes the prompt")
    ap.add_argument("--max-acu", type=int, default=None)
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--timeout", type=int, default=1800, help="stop polling after this many seconds")
    args = ap.parse_args()

    s = Settings.from_env(os.environ)
    if not s.devin_api_key or not s.devin_org_id:
        print("Set DEVIN_API_KEY and DEVIN_ORG_ID first.", file=sys.stderr)
        return 2
    client = DevinClient(s.devin_api_key, s.devin_org_id, s.devin_api_base)
    prompt = args.prompt or Path(args.issue_file).read_text()

    sess = client.create_session(prompt, args.title, tags=["m0"], structured_output_schema=OUTPUT_SCHEMA,
                                 max_acu_limit=args.max_acu)
    sid = sess["session_id"]
    print(f"created {sid}\n{sess.get('url')}")

    last = None
    started = time.time()
    wrapped = False
    while time.time() - started < args.timeout:
        snap = client.get_session(sid)
        key = (snap.get("status"), snap.get("status_detail"))
        r = interpret(snap)
        if key != last:
            print(f"{time.strftime('%H:%M:%S')}  status={key[0]} detail={key[1]} -> {r.kind}")
            last = key
        if r.kind in ("done", "blocked", "failed") and not wrapped:
            print(json.dumps(snap.get("structured_output"), indent=2))
            print("PRs:", r.pr_urls)
            if r.kind == "done":
                client.send_message(sid, WRAP_UP_MESSAGE)
                wrapped = True
            else:
                print("Session needs a human or failed; stopping here.")
                return 0
        if snap.get("status") in TERMINAL_STATUSES:
            print("final:", json.dumps({k: snap.get(k) for k in ("status", "status_detail", "pull_requests")}))
            return 0
        time.sleep(args.interval)
    print("timed out while polling")
    return 1


if __name__ == "__main__":
    sys.exit(main())
