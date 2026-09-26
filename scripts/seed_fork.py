#!/usr/bin/env python3
"""Set up the Superset fork for the bot in one command, using the GitHub CLI (`gh`).

  python3 scripts/seed_fork.py                         # labels + issues (as devin:proposed)
  python3 scripts/seed_fork.py --dry-run               # show what would happen
  python3 scripts/seed_fork.py --label ready           # also approve them (starts Devin!)
  python3 scripts/seed_fork.py --install-files         # copy fork-files/.github into the fork
  python3 scripts/seed_fork.py --disable-other-workflows

Needs: `gh auth login` done, and write access to the fork. Safe to re-run: labels are upserted and
issues are skipped when one with the same title already exists.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.github_client import LABEL_COLORS  # noqa: E402


def gh(*args: str, dry: bool = False, check: bool = True) -> str:
    cmd = ["gh", *args]
    if dry:
        print("  [dry-run]", " ".join(cmd[:8]), "..." if len(cmd) > 8 else "")
        return ""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise SystemExit(f"gh failed: {' '.join(cmd[:6])}\n{res.stderr.strip()}")
    return res.stdout


def parse_issue(path: Path) -> tuple[str, list[str], str]:
    head, _, body = path.read_text().partition("\n---\n")
    meta = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    return meta["title"], [x.strip() for x in meta.get("labels", "").split(",") if x.strip()], body.strip() + "\n"


def make_labels(repo: str, dry: bool) -> None:
    print("Labels")
    for name, (color, desc) in LABEL_COLORS.items():
        gh("label", "create", name, "--repo", repo, "--color", color, "--description", desc, "--force", dry=dry)
        print(f"  {name}")


def make_issues(repo: str, status_label: str | None, dry: bool) -> None:
    print("Issues")
    existing = set()
    if not dry:
        out = gh("issue", "list", "--repo", repo, "--state", "all", "--limit", "300", "--json", "title")
        existing = {i["title"] for i in json.loads(out or "[]")}
    for path in sorted((ROOT / "seed" / "issues").glob("*.md")):
        title, labels, body = parse_issue(path)
        if title in existing:
            print(f"  skip (exists): {title}")
            continue
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for lab in labels + ([status_label] if status_label else []):
            args += ["--label", lab]
        out = gh(*args, dry=dry)
        print(f"  created: {title} -> {out.strip() or '(dry-run)'}")


def install_files(repo: str, dry: bool) -> None:
    """Push fork-files/.github/** to the fork's default branch through the contents API."""
    print("Fork files (workflow, script, issue template)")
    base = ROOT / "fork-files"
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        rel = path.relative_to(base).as_posix()
        content = base64.b64encode(path.read_bytes()).decode()
        sha = ""
        if not dry:
            cur = subprocess.run(["gh", "api", f"repos/{repo}/contents/{rel}"], capture_output=True, text=True)
            if cur.returncode == 0:
                sha = json.loads(cur.stdout).get("sha", "")
        args = ["api", "-X", "PUT", f"repos/{repo}/contents/{rel}", "-f", f"message=Add {rel} (superset-helper-eng)", "-f", f"content={content}"]
        if sha:
            args += ["-f", f"sha={sha}"]
        gh(*args, dry=dry)
        print(f"  {'updated' if sha else 'added'}: {rel}")


def disable_other_workflows(repo: str, dry: bool) -> None:
    """The fork inherits all of Superset's CI, which would run on every Devin PR. Keep only devin-verify."""
    print("Workflows")
    out = gh("workflow", "list", "--repo", repo, "--all", "--json", "name,id,state", dry=dry)
    for wf in json.loads(out or "[]"):
        if wf["name"] == "devin-verify":
            if wf["state"] != "active":
                gh("workflow", "enable", str(wf["id"]), "--repo", repo, dry=dry)
            continue
        if wf["state"] == "active":
            gh("workflow", "disable", str(wf["id"]), "--repo", repo, dry=dry)
            print(f"  disabled: {wf['name']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="french-fries-and-sundaes/superset")
    ap.add_argument("--label", choices=["proposed", "ready", "none"], default="proposed",
                    help="status label for the seeded issues (default proposed: nothing starts until approved)")
    ap.add_argument("--labels-only", action="store_true")
    ap.add_argument("--issues-only", action="store_true")
    ap.add_argument("--install-files", action="store_true", help="add the verification workflow and issue template to the fork")
    ap.add_argument("--disable-other-workflows", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    status = None if args.label == "none" else f"devin:{args.label}"
    if not args.issues_only:
        make_labels(args.repo, args.dry_run)
    if not args.labels_only:
        make_issues(args.repo, status, args.dry_run)
    if args.install_files:
        install_files(args.repo, args.dry_run)
    if args.disable_other_workflows:
        disable_other_workflows(args.repo, args.dry_run)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
