"""Startup safety checks: never let simulated data touch the real repository.

Two accidents this prevents:
  1. A database full of simulated tasks (from DEVIN_MODE=fake) reused in real mode. Its task numbers
     (#1, #2, ...) collide with real issue numbers, so a real approval could be ignored, and the bot
     would try to label and comment on real issues on behalf of fake tasks.
  2. A simulated Devin combined with a real GITHUB_TOKEN, which would file fake findings as real issues.
"""
from __future__ import annotations

from .config import ConfigError, Settings
from .db import Store

FRESH = "To start fresh: stop the service, delete the data/ folder (`rm -rf data`), and start again."


def check_safety(store: Store, settings: Settings) -> None:
    if settings.allow_mode_change:
        store.set_meta("devin_mode", settings.devin_mode)
        return
    if settings.devin_mode == "fake" and settings.github_token:
        raise ConfigError(
            "SAFETY CHECK: DEVIN_MODE=fake together with a GITHUB_TOKEN would write simulated tasks to the real "
            "repository. Leave GITHUB_TOKEN empty while simulating (GitHub writes are then only logged), or set "
            "DEVIN_MODE=real."
        )
    recorded = store.get_meta("devin_mode")
    if recorded is None:
        if store.count_tasks() > 0 and settings.devin_mode == "real":
            raise ConfigError(
                "SAFETY CHECK: this database already contains tasks but was not created in real mode; they are "
                "probably simulated demo tasks whose numbers would collide with real issues. " + FRESH
                + " (Set ALLOW_MODE_CHANGE=1 only if you are sure they are real.)"
            )
        store.set_meta("devin_mode", settings.devin_mode)
    elif recorded != settings.devin_mode:
        raise ConfigError(
            f"SAFETY CHECK: this database was created with DEVIN_MODE={recorded}, but you are running "
            f"DEVIN_MODE={settings.devin_mode}. Simulated and real tasks must not share a database. " + FRESH
            + " Or point DB_PATH at a different file."
        )
