"""Prompt and structured-output schema for one Devin session (one issue per session)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

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

WRAP_UP_MESSAGE = "Thanks, that is all. You are done, please finish the session."


def load_knowledge(directory: str) -> str:
    """Concatenate knowledge/*.md (except README.md) so the rules live in version control."""
    d = Path(directory)
    if not d.is_dir():
        return ""
    parts = []
    for f in sorted(d.glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        parts.append(f"### {f.stem}\n{f.read_text().strip()}")
    return "\n\n".join(parts)


def build_task_prompt(task: dict[str, Any], knowledge: str = "", guidance: str = "") -> str:
    n = task["issue_number"]
    guidance_block = (
        f"\n## Human guidance from earlier attempts\n{guidance.strip()}\n" if guidance.strip() else ""
    )
    knowledge_block = f"\n## Repository knowledge (follow these)\n{knowledge}\n" if knowledge.strip() else ""
    return f"""You are fixing ONE GitHub issue in the repository {task['repo']}. Work on this issue only. Do not fix anything else.

## Issue #{n}: {task['title']}

{task['body'].strip() or '(no description)'}
{guidance_block}
## How to work (follow these steps in order)

1. TRIAGE FIRST. Before changing any code, run the issue's verify command (the fenced `verify` block in the issue) on the unmodified default branch.
   Stop and ask a human, without writing any fix, if ANY of these is true: the verify command already passes (the problem does not exist); the verify command is missing or cannot be run; the issue is unclear or too broad for a single pull request; or the finding looks like a false positive. When you stop, set outcome to "blocked" and put exactly what you need in "question".
2. FIX. Create a branch, make the smallest change that resolves the issue.
3. VERIFY. Run the verify command again and confirm it now passes. Also run the existing tests for the files you touched.
4. OPEN A PULL REQUEST against the default branch. The PR description must include "Closes #{n}", the verify command, and a short summary of its output. Do NOT merge the pull request.
5. FINISH. Fill in the structured output: outcome ("done" with pull_request_urls and verify_passed, "blocked" with a question, or "failed" with a summary of what you tried). Update the structured output whenever your state changes, then wait.

## Rules
- One issue, one session, one pull request.
- Never use `npm audit fix --force`.
- If you are unsure or blocked, say so with outcome "blocked" instead of guessing.
{knowledge_block}"""
