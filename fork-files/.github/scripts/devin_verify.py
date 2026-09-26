#!/usr/bin/env python3
"""Independent verification for a Devin pull request.

Runs in GitHub Actions on the fork. It reads the linked issue ("Closes #N" in the PR body),
extracts the fenced ```verify block, checks EVERY command against a strict allowlist, and only
then runs it. Devin saying "done" is a claim; this exit code is the evidence.

Why the allowlist: the issue text is untrusted input. Nothing from it is ever interpolated into
the workflow YAML or passed to a shell unvalidated. Anything unexpected fails the check.

Environment: PR_BODY, REPO (owner/name), GH_TOKEN (read access to issues).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

ALLOWED_PROGRAMS = {"npm", "npx", "pip", "pip3", "pip-audit", "pytest", "ruff", "mypy", "python", "python3", "cd"}
ALLOWED_NPX = {"eslint", "tsc", "oxlint"}
ALLOWED_PYTHON_MODULES = {"pytest", "mypy", "ruff", "pip", "pip_audit"}
BLOCKED_NPM = {"publish", "login", "logout", "token", "adduser", "owner", "access", "deprecate", "unpublish"}
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_./:=@,+ \[\]~-]+$")  # no ; | & $ ` ( ) < > or quotes
CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
VERIFY_BLOCK = re.compile(r"```verify\s*\n(.*?)```", re.S)


def segment_ok(seg: str) -> tuple[bool, str]:
    seg = seg.strip()
    if not seg:
        return False, "empty command"
    if not SAFE_SEGMENT.match(seg):
        return False, "contains characters that are not allowed (pipes, redirects, quotes, $, ;, backticks, parentheses)"
    if "://" in seg or "git+" in seg:
        return False, "URLs are not allowed"
    words = seg.split()
    prog = words[0]
    if prog not in ALLOWED_PROGRAMS:
        return False, f"program '{prog}' is not on the allowlist"
    if prog == "cd" and (len(words) != 2 or ".." in words[1] or words[1].startswith("/")):
        return False, "cd must go to one relative directory"
    if prog == "npx" and (len(words) < 2 or words[1] not in ALLOWED_NPX):
        return False, f"npx is only allowed for {sorted(ALLOWED_NPX)}"
    if prog == "npm" and (len(words) < 2 or words[1] in BLOCKED_NPM):
        return False, "that npm subcommand is not allowed"
    if prog in ("python", "python3") and not (len(words) >= 3 and words[1] == "-m" and words[2] in ALLOWED_PYTHON_MODULES):
        return False, f"python is only allowed as: python -m {sorted(ALLOWED_PYTHON_MODULES)}"
    return True, ""


def validate(command_block: str) -> list[str]:
    """Return the list of command LINES if every segment is allowed; raise ValueError otherwise."""
    lines = [ln.strip() for ln in command_block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ValueError("the verify block is empty")
    for ln in lines:
        for seg in ln.split("&&"):
            ok, why = segment_ok(seg)
            if not ok:
                raise ValueError(f"rejected `{ln}`: {why}")
    return lines


def fetch_issue_body(repo: str, number: int, token: str) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("body") or ""


def run_line(line: str) -> int:
    """Run one validated line. `cd x && cmd` chains are executed segment by segment so the working
    directory carries over, without ever giving a shell the raw text."""
    cwd = os.getcwd()
    for seg in (s.strip() for s in line.split("&&")):
        words = seg.split()
        print(f"$ {seg}", flush=True)
        if words[0] == "cd":
            cwd = os.path.join(cwd, words[1])
            continue
        code = subprocess.call(words, cwd=cwd)
        if code != 0:
            print(f"::error::command failed with exit code {code}: {seg}", flush=True)
            return code
    return 0


def main() -> int:
    body = os.environ.get("PR_BODY", "")
    repo = os.environ.get("REPO", "")
    token = os.environ.get("GH_TOKEN", "")
    m = CLOSES.search(body)
    if not m:
        print("::error::the pull request does not say `Closes #N`")
        return 1
    issue_body = fetch_issue_body(repo, int(m.group(1)), token)
    block = VERIFY_BLOCK.search(issue_body)
    if not block:
        print(f"::error::issue #{m.group(1)} has no ```verify block")
        return 1
    try:
        lines = validate(block.group(1))
    except ValueError as e:
        print(f"::error::{e}")
        return 1
    for line in lines:
        code = run_line(line)
        if code != 0:
            return code
    print("verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
