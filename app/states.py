"""Task states. Each state maps to a GitHub label `devin:<state>` (completed has none:
the issue is simply closed by the merged PR)."""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    PROPOSED = "proposed"  # scanner filed it; a human must approve
    READY = "ready"  # approved; backlog until a session exists
    IN_PROGRESS = "in-progress"  # a Devin session is working (triage first)
    BLOCKED = "blocked"  # needs a human answer (unclear / false positive / stuck)
    IN_REVIEW = "in-review"  # PR open, waiting for human review
    FAILED = "failed"  # Devin tried and could not finish
    REJECTED = "rejected"  # a human said no
    COMPLETED = "completed"  # PR merged

    @property
    def label(self) -> str | None:
        return None if self is State.COMPLETED else f"devin:{self.value}"


ALLOWED: dict[State, set[State]] = {
    State.PROPOSED: {State.READY, State.REJECTED},
    State.READY: {State.IN_PROGRESS, State.REJECTED},
    # IN_PROGRESS -> READY is used only to release a claim when starting a session
    # failed for a retryable reason (rate limit / server error).
    State.IN_PROGRESS: {State.READY, State.BLOCKED, State.IN_REVIEW, State.FAILED, State.REJECTED},
    State.BLOCKED: {State.READY, State.REJECTED},
    State.FAILED: {State.READY, State.REJECTED},
    State.IN_REVIEW: {State.COMPLETED, State.IN_PROGRESS, State.REJECTED},
    State.REJECTED: {State.READY},  # human reopens the issue and re-applies devin:ready
    State.COMPLETED: set(),
}

# States where the ball is in a human's court (for the "needs a human" dashboard list).
HUMAN_NEEDED = {State.PROPOSED, State.BLOCKED, State.IN_REVIEW, State.FAILED}
