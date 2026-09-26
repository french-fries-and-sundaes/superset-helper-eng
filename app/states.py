"""Task states. Each state maps to a GitHub label `devin:<state>` (completed has none:
the issue is simply closed by the merged PR)."""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    PROPOSED = "proposed"  # scanner filed it; a human must approve
    READY = "ready"  # approved; backlog until a session exists
    IN_PROGRESS = "in-progress"  # a Devin session is working (triage first), or its PR is being verified
    BLOCKED = "blocked"  # needs a human answer (unclear / false positive / stuck)
    IN_REVIEW = "in-review"  # PR open and verified, waiting for human review
    FAILED = "failed"  # Devin tried and could not finish, or verification failed
    REJECTED = "rejected"  # a human said no
    COMPLETED = "completed"  # PR merged
    NOT_NEEDED = "not-needed"  # Devin pushed back and a human agreed and closed it: no fix was needed

    @property
    def label(self) -> str | None:
        # Terminal outcomes have no label: the issue is simply closed (Devin's comment explains why).
        return None if self in (State.COMPLETED, State.NOT_NEEDED) else f"devin:{self.value}"


ALLOWED: dict[State, set[State]] = {
    State.PROPOSED: {State.READY, State.REJECTED},
    State.READY: {State.IN_PROGRESS, State.REJECTED},
    # IN_PROGRESS -> READY releases a claim when starting a session failed for a retryable reason.
    # IN_PROGRESS -> COMPLETED: a human merged early. (Rejections can happen from any open state.)
    State.IN_PROGRESS: {State.READY, State.BLOCKED, State.IN_REVIEW, State.FAILED, State.REJECTED, State.COMPLETED},
    State.BLOCKED: {State.READY, State.REJECTED, State.COMPLETED, State.NOT_NEEDED},
    State.FAILED: {State.READY, State.REJECTED, State.COMPLETED},
    # IN_REVIEW -> READY: a reviewer requested changes; the worker starts a revision session.
    State.IN_REVIEW: {State.COMPLETED, State.IN_PROGRESS, State.READY, State.REJECTED},
    State.REJECTED: {State.READY},  # human reopens the issue and re-applies devin:ready
    State.COMPLETED: set(),
    State.NOT_NEEDED: {State.READY},  # a human reopens the issue and re-applies devin:ready
}

# States where the ball is in a human's court (for the "needs a human" dashboard list).
HUMAN_NEEDED = {State.PROPOSED, State.BLOCKED, State.IN_REVIEW, State.FAILED}

STATUS_LABELS = [s.label for s in State if s.label]
