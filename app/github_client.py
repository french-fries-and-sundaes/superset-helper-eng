"""GitHub REST client, plus a recording stand-in used when no token is configured.

The bot writes to GitHub in a few places: it mirrors task state as one `devin:*`
status label per issue, posts explanatory comments, files scan findings as issues,
and labels knowledge PRs. With no GITHUB_TOKEN every write is recorded (and logged)
instead of sent, so the whole workflow can be demonstrated without touching GitHub.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

import httpx

from .states import STATUS_LABELS

log = logging.getLogger(__name__)

# Every comment the bot posts ends with this hidden marker. A personal access token posts
# as the human who owns it, so the marker is how we recognise (and ignore) our own comments.
BOT_MARKER = "<!-- superset-helper-eng:bot -->"

LABEL_COLORS = {
    "devin:proposed": ("d4c5f9", "Scanner filed it; awaiting a human to approve"),
    "devin:ready": ("0e8a16", "Approved: Devin will work on this"),
    "devin:in-progress": ("1d76db", "A Devin session is working on this"),
    "devin:blocked": ("d93f0b", "Devin needs a human answer"),
    "devin:in-review": ("fbca04", "Pull request open and verified; awaiting review"),
    "devin:failed": ("b60205", "Devin could not complete this"),
    "devin:rejected": ("6a737d", "A human decided not to do this"),
    "devin:knowledge-base-improvement": ("c2e0c6", "Proposed rule changes for knowledge/"),
    "type:dependency": ("ededed", ""),
    "type:lint": ("ededed", ""),
    "type:tests": ("ededed", ""),
    "type:bug": ("ededed", ""),
    "type:other": ("ededed", ""),
}


class GitHubAPIError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"GitHub API error {status}: {detail}")
        self.status = status
        self.detail = detail


PR_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


def parse_pr_url(url: str) -> tuple[str, int] | None:
    m = PR_URL_RE.search(url or "")
    return (m.group(1), int(m.group(2))) if m else None


class GitHubClient:
    dry_run = False

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def _req(self, method: str, path: str, ok: tuple[int, ...] = (), **kw: Any) -> Any:
        resp = self._http.request(method, path.lstrip("/"), **kw)
        if resp.status_code in ok:
            return None
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("message", resp.text[:200]))
            except Exception:
                detail = resp.text[:200]
            raise GitHubAPIError(resp.status_code, detail)
        return resp.json() if resp.content else None

    # ---- issues ----------------------------------------------------------------
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._req("POST", f"repos/{repo}/issues", json={"title": title, "body": body, "labels": labels})

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._req("GET", f"repos/{repo}/issues/{number}")

    def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return self._req("POST", f"repos/{repo}/issues/{number}/comments", json={"body": body + "\n\n" + BOT_MARKER})

    def list_issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._req("GET", f"repos/{repo}/issues/{number}/comments", params={"per_page": 100}) or []

    def close_issue(self, repo: str, number: int, reason: str = "not_planned") -> None:
        self._req("PATCH", f"repos/{repo}/issues/{number}", json={"state": "closed", "state_reason": reason})

    def reopen_issue(self, repo: str, number: int) -> None:
        self._req("PATCH", f"repos/{repo}/issues/{number}", json={"state": "open"})

    # ---- labels (issues and PRs share the issues endpoint) ---------------------
    def get_labels(self, repo: str, number: int) -> list[str]:
        data = self._req("GET", f"repos/{repo}/issues/{number}/labels", params={"per_page": 100}) or []
        return [d["name"] for d in data]

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._req("POST", f"repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        name = urllib.parse.quote(label, safe="")
        self._req("DELETE", f"repos/{repo}/issues/{number}/labels/{name}", ok=(404,))

    def set_status_label(self, repo: str, number: int, target: str | None) -> None:
        """Make `target` the only devin status label on the issue (None removes them all)."""
        current = self.get_labels(repo, number)
        for lab in current:
            if lab in STATUS_LABELS and lab != target:
                self.remove_label(repo, number, lab)
        if target and target not in current:
            self.add_labels(repo, number, [target])

    def ensure_label(self, repo: str, name: str, color: str = "ededed", description: str = "") -> None:
        self._req(
            "POST", f"repos/{repo}/labels", ok=(422,), json={"name": name, "color": color, "description": description}
        )

    # ---- pull requests / actions -----------------------------------------------
    def list_review_comments(self, repo: str, pr_number: int, review_id: int) -> list[dict[str, Any]]:
        return self._req("GET", f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments") or []

    def list_closed_unmerged_prs(self, repo: str, label: str) -> list[dict[str, Any]]:
        prs = self._req("GET", f"repos/{repo}/pulls", params={"state": "closed", "per_page": 50}) or []
        return [
            p
            for p in prs
            if not p.get("merged_at") and any(lb.get("name") == label for lb in p.get("labels", []))
        ]

    def job_log_excerpt(self, repo: str, run_id: int, lines: int = 40) -> str:
        """Tail of the first failed job's log for a workflow run ('' if unavailable)."""
        try:
            jobs = (self._req("GET", f"repos/{repo}/actions/runs/{run_id}/jobs") or {}).get("jobs", [])
            failed = next((j for j in jobs if j.get("conclusion") == "failure"), None)
            if not failed:
                return ""
            resp = self._http.get(f"repos/{repo}/actions/jobs/{failed['id']}/logs")
            if resp.status_code >= 400:
                return ""
            return "\n".join(resp.text.splitlines()[-lines:])
        except Exception:  # excerpt is a nicety, never fatal
            return ""


class RecordingGitHub:
    """Dry-run GitHub: remembers every write instead of sending it."""

    dry_run = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_issue = 9000
        self._labels: dict[tuple[str, int], set[str]] = {}

    def _rec(self, _op: str, **kw: Any) -> None:
        self.calls.append((_op, kw))
        log.info("[github dry-run] %s %s", _op, {k: (v if len(str(v)) < 120 else str(v)[:117] + "...") for k, v in kw.items()})

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        self._next_issue += 1
        n = self._next_issue
        self._labels[(repo, n)] = set(labels)
        self._rec("create_issue", repo=repo, number=n, title=title, labels=labels)
        return {"number": n, "html_url": f"https://github.com/{repo}/issues/{n}"}

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return {"number": number, "state": "open"}

    def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        self._rec("comment", repo=repo, number=number, body=body)
        return {}

    def list_issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return []

    def close_issue(self, repo: str, number: int, reason: str = "not_planned") -> None:
        self._rec("close_issue", repo=repo, number=number, reason=reason)

    def reopen_issue(self, repo: str, number: int) -> None:
        self._rec("reopen_issue", repo=repo, number=number)

    def get_labels(self, repo: str, number: int) -> list[str]:
        return sorted(self._labels.get((repo, number), set()))

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._labels.setdefault((repo, number), set()).update(labels)
        self._rec("add_labels", repo=repo, number=number, labels=labels)

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self._labels.setdefault((repo, number), set()).discard(label)
        self._rec("remove_label", repo=repo, number=number, label=label)

    def set_status_label(self, repo: str, number: int, target: str | None) -> None:
        cur = self._labels.setdefault((repo, number), set())
        cur.difference_update(STATUS_LABELS)
        if target:
            cur.add(target)
        self._rec("set_status_label", repo=repo, number=number, label=target)

    def ensure_label(self, repo: str, name: str, color: str = "ededed", description: str = "") -> None:
        self._rec("ensure_label", repo=repo, name=name)

    def list_review_comments(self, repo: str, pr_number: int, review_id: int) -> list[dict[str, Any]]:
        return []

    def list_closed_unmerged_prs(self, repo: str, label: str) -> list[dict[str, Any]]:
        return []

    def job_log_excerpt(self, repo: str, run_id: int, lines: int = 40) -> str:
        return ""


def make_github(token: str, base_url: str = "https://api.github.com") -> GitHubClient | RecordingGitHub:
    return GitHubClient(token, base_url) if token else RecordingGitHub()
