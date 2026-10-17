# src/codepilot/states.py
from enum import StrEnum


class RunState(StrEnum):
    TRIAGED = "TRIAGED"
    EXPLORING = "EXPLORING"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PR_OPENED = "PR_OPENED"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CLOSED = "CLOSED"


IN_FLIGHT = frozenset({RunState.TRIAGED, RunState.EXPLORING, RunState.IMPLEMENTING, RunState.TESTING})
ACTIVE = IN_FLIGHT | {RunState.AWAITING_APPROVAL, RunState.PR_OPENED}
TERMINAL = frozenset({RunState.DONE, RunState.FAILED, RunState.SKIPPED, RunState.CLOSED})

_NEXT: dict[RunState, frozenset[RunState]] = {
    RunState.TRIAGED: frozenset({RunState.EXPLORING, RunState.AWAITING_APPROVAL}),
    # An unexpected command or the retry gate can pause a run in any working phase.
    RunState.EXPLORING: frozenset({RunState.IMPLEMENTING, RunState.AWAITING_APPROVAL}),
    RunState.IMPLEMENTING: frozenset({RunState.TESTING, RunState.EXPLORING, RunState.AWAITING_APPROVAL}),
    RunState.TESTING: frozenset({RunState.IMPLEMENTING, RunState.AWAITING_APPROVAL}),
    # Continuations never reuse a run: the waiting run is CLOSED("continued")
    # and a new run is claimed with the saved work as its seed.
    RunState.AWAITING_APPROVAL: frozenset({RunState.PR_OPENED}),
    RunState.PR_OPENED: frozenset({RunState.DONE}),
}


def can_transition(a: RunState, b: RunState) -> bool:
    if b in TERMINAL and a not in TERMINAL:
        return True
    return b in _NEXT.get(a, frozenset())
