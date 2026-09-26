"""Command line helpers.

  python3 -m app.cli status
  python3 -m app.cli enqueue --issue 3 --title "Override underscore" --body "..." [--proposed]
  python3 -m app.cli tick        # one worker pass (poll sessions, then dispatch)
  python3 -m app.cli worker      # run the worker loop in the foreground
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import Settings
from .db import Store
from .devin_client import make_client
from .orchestrator import Orchestrator


def build(settings: Settings) -> Orchestrator:
    return Orchestrator(Store(settings.db_path), make_client(settings), settings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show counts and tasks that need a human")
    e = sub.add_parser("enqueue", help="add a task without GitHub (same as applying the label)")
    e.add_argument("--issue", type=int, required=True)
    e.add_argument("--title", required=True)
    e.add_argument("--body", default="")
    e.add_argument("--proposed", action="store_true", help="file as devin:proposed instead of ready")
    sub.add_parser("tick", help="run one worker pass")
    sub.add_parser("worker", help="run the worker loop until interrupted")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    orch = build(settings)

    if args.cmd == "status":
        out = orch.status_summary()
        out["tasks"] = [
            {"issue": t["issue_number"], "state": t["state"], "title": t["title"], "prs": t["pr_urls"]}
            for t in orch.store.list_tasks()
        ]
        print(json.dumps(out, indent=2))
    elif args.cmd == "enqueue":
        fn = orch.register_proposed if args.proposed else orch.request_ready
        print(fn(settings.target_repo, args.issue, args.title, args.body, "cli"))
    elif args.cmd == "tick":
        orch.tick()
        print(json.dumps(orch.status_summary(), indent=2))
    elif args.cmd == "worker":
        try:
            while True:
                orch.tick()
                time.sleep(settings.poll_interval_seconds)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
