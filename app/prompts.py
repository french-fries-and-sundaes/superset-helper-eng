"""Prompts and structured-output schemas for the three kinds of Devin session:
fix (one issue per session), scan (find problems, file nothing), learn (propose knowledge rules)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

WRAP_UP_MESSAGE = "Thanks, that is all. You are done, please finish the session."

FINDING_TYPES = ("dependency", "lint", "tests", "bug", "other")
SEVERITIES = ("critical", "high", "medium", "low")

# ---- schemas ------------------------------------------------------------------------
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["done", "blocked", "failed"],
            "description": "done = PR opened and verify command passed; blocked = need a human answer; failed = tried and could not complete",
        },
        "summary": {"type": "string", "description": "One or two sentences on what happened"},
        "question": {"type": "string", "description": "If blocked: exactly what you need from a human"},
        "pull_request_urls": {"type": "array", "items": {"type": "string"}},
        "verify_passed": {"type": "boolean"},
    },
    "required": ["outcome", "summary"],
}

SCAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "failed"]},
        "summary": {"type": "string", "description": "What you ran and what you found, briefly"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string", "description": "What is wrong and why it matters"},
                    "how_to_triage": {"type": "string", "description": "How a human or agent can confirm the problem exists"},
                    "verify_command": {"type": "string", "description": "Runnable command(s), one per line, that exit 0 only once fixed"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "type": {"type": "string", "enum": list(FINDING_TYPES)},
                    "source": {"type": "string", "description": "Tool and command that found it"},
                },
                "required": ["title", "description", "how_to_triage", "verify_command"],
            },
        },
        "skipped": {
            "type": "array",
            "description": "Findings you did NOT report because an existing issue already covers them",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["title", "reason"],
            },
        },
    },
    "required": ["outcome", "summary"],
}

LEARN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "failed"]},
        "summary": {"type": "string", "description": "What rules you proposed, or why you proposed none"},
        "pull_request_urls": {"type": "array", "items": {"type": "string"}},
        "rules_proposed": {"type": "integer"},
        "rules_skipped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"rule": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["rule", "reason"],
            },
        },
    },
    "required": ["outcome", "summary"],
}

VERIFY_COMMAND_RULES = (
    "Verify commands: one command per line. Only these programs are allowed: npm, npx, pip, pip-audit, pytest, "
    "ruff, mypy, python, python3, cd. Chain steps with && only. No pipes, redirects, semicolons, or subshells."
)


# ---- knowledge files ------------------------------------------------------------------
def load_knowledge(directory: str, kind: str = "fix") -> str:
    """Rules that live in version control. kind='fix' skips scan.md; 'scan' uses scan.md plus
    triage-and-fix.md (environment gotchas); 'all' returns everything except README."""
    d = Path(directory)
    if not d.is_dir():
        return ""
    parts = []
    for f in sorted(d.glob("*.md")):
        name = f.stem.lower()
        if name == "readme":
            continue
        if kind == "fix" and name == "scan":
            continue
        if kind == "scan" and name not in ("scan", "triage-and-fix"):
            continue
        parts.append(f"### {f.stem}\n{f.read_text().strip()}")
    return "\n\n".join(parts)


# ---- fix session ----------------------------------------------------------------------
def build_task_prompt(
    task: dict[str, Any], knowledge: str = "", guidance: str = "", open_prs: list[str] | None = None
) -> str:
    n = task["issue_number"]
    guidance_block = (
        f"\n## Human guidance and history from earlier attempts\n{guidance.strip()}\n" if guidance.strip() else ""
    )
    knowledge_block = f"\n## Repository knowledge (follow these)\n{knowledge}\n" if knowledge.strip() else ""
    if open_prs:
        pr_list = "\n".join(f"- {u}" for u in open_prs)
        revise_block = (
            "\n## This is a REVISION\n"
            f"A pull request already exists for this issue:\n{pr_list}\n"
            "Address the reviewer feedback above by pushing new commits to that pull request's branch. "
            "Do NOT open a new pull request. Skip triage: the problem was already confirmed.\n"
        )
        steps = (
            "1. Read the feedback and the existing pull request.\n"
            "2. Push commits to the existing branch that address every point of feedback.\n"
            "3. VERIFY. Run the issue's verify command and the tests for the files you touched.\n"
            "4. FINISH. Fill in the structured output (outcome, pull_request_urls with the existing PR, verify_passed), then wait."
        )
    else:
        revise_block = ""
        steps = (
            "1. TRIAGE FIRST. Before changing any code, run the issue's verify command (the fenced `verify` block in the issue) on the unmodified default branch.\n"
            "   Stop and ask a human, without writing any fix, if ANY of these is true: the verify command already passes (the problem does not exist); the verify command is missing or cannot be run; the issue is unclear or too broad for a single pull request; or the finding looks like a false positive. When you stop, set outcome to \"blocked\" and put exactly what you need in \"question\".\n"
            "2. FIX. Create a branch and make the smallest change that resolves the issue.\n"
            "3. VERIFY. Run the verify command again and confirm it now passes. Also run the existing tests for the files you touched.\n"
            f"4. OPEN A PULL REQUEST against the default branch. The PR description must include \"Closes #{n}\", the verify command, and a short summary of its output. Do NOT merge the pull request.\n"
            "5. FINISH. Fill in the structured output: outcome (\"done\" with pull_request_urls and verify_passed, \"blocked\" with a question, or \"failed\" with a summary of what you tried). Update the structured output whenever your state changes, then wait."
        )
    return f"""You are fixing ONE GitHub issue in the repository {task['repo']}. Work on this issue only. Do not fix anything else.

## Issue #{n}: {task['title']}

{task['body'].strip() or '(no description)'}
{guidance_block}{revise_block}
## How to work (follow these steps in order)

{steps}

## Rules
- One issue, one session, one pull request.
- Never use `npm audit fix --force`.
- If you are unsure or blocked, say so with outcome "blocked" instead of guessing.
{knowledge_block}"""


# ---- scan session ---------------------------------------------------------------------
def build_scan_prompt(repo: str, knowledge: str, max_findings: int) -> str:
    return f"""You are running a sweep for security and code-quality problems in the repository {repo}.

Do NOT change any code, open pull requests, or create issues. Your only output is the structured output.

## What to do
1. Run the checks described in the knowledge section below (dependency audits, linters, type checks, test coverage), from a clean checkout of the default branch.
2. Turn the most valuable, well-scoped problems into findings. Report at most {max_findings}, most important first. Prefer small, verifiable fixes over broad refactors.
3. Skip anything already covered by an existing issue in {repo}, open or closed. For each finding you skip, add it to `skipped` with the reason.
4. Every finding needs: a clear title; a description (what is wrong and why it matters); how_to_triage; a runnable verify_command that exits 0 only once the problem is fixed; a severity; a type; and the source (tool and command that found it). Only report problems you have actually observed.
5. Finish by setting outcome to "done" (even with zero findings) or "failed" if you could not run the checks. Then wait.

## {VERIFY_COMMAND_RULES}

## Knowledge
{knowledge or '(none)'}
"""


# ---- learn session --------------------------------------------------------------------
def build_learn_prompt(
    solution_repo: str,
    knowledge_label: str,
    knowledge: str,
    feedback: list[dict[str, Any]],
    rejected_proposals: list[str],
) -> str:
    lines = []
    for f in feedback:
        who = f.get("author") or "someone"
        lines.append(
            f"- [{f.get('task_title') or 'task ' + str(f.get('task_id'))}] (state then: {f.get('task_state') or 'unknown'}) "
            f"{who} ({f['kind']}): {f['text'].strip()[:600]}"
        )
    feedback_block = "\n".join(lines) or "(none)"
    rejected_block = "\n".join(f"- {r}" for r in rejected_proposals) or "(none)"
    return f"""You maintain the rulebook (`knowledge/*.md`) that a bot injects into every Devin session that fixes issues in a Superset fork. Below is human feedback collected since the last run. Turn it into rule changes, if it teaches something general.

## Human feedback since the last run
{feedback_block}

## Current rulebook (files under knowledge/ in {solution_repo})
{knowledge or '(empty)'}

## Proposals a human already rejected (do not re-suggest these)
{rejected_block}

## What to do
1. Decide which lessons are GENERAL (would help on future, different issues). Ignore feedback specific to one issue.
2. Check every candidate rule against the current rulebook: skip duplicates and anything that contradicts an existing rule (note it in `rules_skipped` with the reason). Also skip anything matching a rejected proposal.
3. If at least one rule survives, open ONE pull request in {solution_repo} that edits files under `knowledge/` only. Use branch name `knowledge/learn-<date>`. The PR description must list each new rule with the feedback that motivated it. Do NOT merge it.
4. If nothing survives, do not open a pull request: set outcome to "done" and explain in summary.
5. Fill in the structured output (outcome, summary, pull_request_urls, rules_proposed, rules_skipped), then wait. The bot will add the label `{knowledge_label}` to the pull request.

Rules: keep each rule short and actionable; never include secrets; do not edit code or workflows.
"""
