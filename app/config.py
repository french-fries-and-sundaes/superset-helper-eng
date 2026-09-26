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
    target_repo: str = "french-fries-and-sundaes/superset"
    ready_label: str = "devin:ready"
    proposed_label: str = "devin:proposed"

    # Guards
    max_sessions_per_day: int = 20  # rolling 24h; the primary spend guard
    max_acu_per_session: int | None = None  # optional Devin-side cap per session

    # Runtime
    db_path: str = "data/tasks.db"
    poll_interval_seconds: int = 15
    knowledge_dir: str = "knowledge"
    run_worker: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls(
            devin_api_key=env.get("DEVIN_API_KEY", "").strip(),
            devin_org_id=env.get("DEVIN_ORG_ID", "").strip(),
            devin_api_base=env.get("DEVIN_API_BASE", cls.devin_api_base).strip(),
            devin_mode=env.get("DEVIN_MODE", "real").strip().lower(),
            github_webhook_secret=env.get("GITHUB_WEBHOOK_SECRET", "").strip(),
            target_repo=env.get("TARGET_REPO", cls.target_repo).strip(),
            ready_label=env.get("READY_LABEL", cls.ready_label).strip(),
            proposed_label=env.get("PROPOSED_LABEL", cls.proposed_label).strip(),
            max_sessions_per_day=_int(env, "MAX_SESSIONS_PER_DAY", 20),
            max_acu_per_session=_opt_int(env, "MAX_ACU_PER_SESSION"),
            db_path=env.get("DB_PATH", cls.db_path).strip(),
            poll_interval_seconds=_int(env, "POLL_INTERVAL_SECONDS", 15),
            knowledge_dir=env.get("KNOWLEDGE_DIR", cls.knowledge_dir).strip(),
            run_worker=_bool(env, "RUN_WORKER", True),
        )
        if s.devin_mode not in ("real", "fake"):
            raise ConfigError("DEVIN_MODE must be 'real' or 'fake'")
        return s
