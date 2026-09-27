"""Runtime settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class ConfigError(Exception):
    pass


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    return int(raw) if raw else default


def _opt_int(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name, "").strip()
    return int(raw) if raw else None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Devin API
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_api_base: str = "https://api.devin.ai/v3"
    devin_mode: str = "real"  # "real" talks to Devin; "fake" simulates sessions (free)

    # GitHub
    github_webhook_secret: str = ""
    github_token: str = ""  # empty = dry run: GitHub writes are recorded, not sent
    github_api_base: str = "https://api.github.com"
    target_repo: str = "french-fries-and-sundaes/superset"
    solution_repo: str = "french-fries-and-sundaes/superset-helper-eng"
    ready_label: str = "devin:ready"
    proposed_label: str = "devin:proposed"
    knowledge_label: str = "devin:knowledge-base-improvement"

    # Verification: "actions" holds a finished session until the GitHub Actions
    # workflow on the fork reports; "off" moves straight to in-review.
    verify_mode: str = "actions"
    verify_workflow_name: str = "devin-verify"
    verify_timeout_minutes: int = 45  # a PR check that never reports moves the task to failed after this long
    devin_branch_prefix: str = "devin/"  # branch prefix of Devin's PRs; recognises its merged fixes in history

    # Guards
    max_acu_per_session: int | None = None  # optional Devin-side cap per session
    scan_max_findings: int = 7  # per sweep, so a scan cannot flood the backlog

    # Safety: allow running against a database created in a different mode (see app/guards.py).
    allow_mode_change: bool = False

    # Dashboard
    dashboard_password: str = ""  # if set, everything except /webhook/github and /healthz needs it

    # Runtime
    db_path: str = "data/tasks.db"
    poll_interval_seconds: int = 15
    knowledge_dir: str = "knowledge"
    run_worker: bool = True

    @property
    def github_dry_run(self) -> bool:
        return not self.github_token

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls(
            devin_api_key=env.get("DEVIN_API_KEY", "").strip(),
            devin_org_id=env.get("DEVIN_ORG_ID", "").strip(),
            devin_api_base=env.get("DEVIN_API_BASE", cls.devin_api_base).strip(),
            devin_mode=env.get("DEVIN_MODE", "real").strip().lower(),
            github_webhook_secret=env.get("GITHUB_WEBHOOK_SECRET", "").strip(),
            github_token=env.get("GITHUB_TOKEN", "").strip(),
            github_api_base=env.get("GITHUB_API_BASE", cls.github_api_base).strip(),
            target_repo=env.get("TARGET_REPO", cls.target_repo).strip(),
            solution_repo=env.get("SOLUTION_REPO", cls.solution_repo).strip(),
            ready_label=env.get("READY_LABEL", cls.ready_label).strip(),
            proposed_label=env.get("PROPOSED_LABEL", cls.proposed_label).strip(),
            knowledge_label=env.get("KNOWLEDGE_LABEL", cls.knowledge_label).strip(),
            verify_mode=env.get("VERIFY_MODE", "actions").strip().lower(),
            verify_workflow_name=env.get("VERIFY_WORKFLOW_NAME", cls.verify_workflow_name).strip(),
            verify_timeout_minutes=int(env.get("VERIFY_TIMEOUT_MINUTES", cls.verify_timeout_minutes)),
            devin_branch_prefix=env.get("DEVIN_BRANCH_PREFIX", cls.devin_branch_prefix).strip() or cls.devin_branch_prefix,
            max_acu_per_session=_opt_int(env, "MAX_ACU_PER_SESSION"),
            scan_max_findings=_int(env, "SCAN_MAX_FINDINGS", 7),
            allow_mode_change=_bool(env, "ALLOW_MODE_CHANGE", False),
            dashboard_password=env.get("DASHBOARD_PASSWORD", "").strip(),
            db_path=env.get("DB_PATH", cls.db_path).strip(),
            poll_interval_seconds=_int(env, "POLL_INTERVAL_SECONDS", 15),
            knowledge_dir=env.get("KNOWLEDGE_DIR", cls.knowledge_dir).strip(),
            run_worker=_bool(env, "RUN_WORKER", True),
        )
        if s.devin_mode not in ("real", "fake"):
            raise ConfigError("DEVIN_MODE must be 'real' or 'fake'")
        if s.verify_mode not in ("actions", "off"):
            raise ConfigError("VERIFY_MODE must be 'actions' or 'off'")
        return s
