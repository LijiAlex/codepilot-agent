# tests/test_store.py
import sqlite3
import pytest
from codepilot.db import connect
from codepilot.states import RunState, ACTIVE, TERMINAL, can_transition
from codepilot.runs import RunStore
from codepilot.handoffs import HandoffStore

@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "cp.db")

def test_claim_is_exclusive_while_active(conn):
    runs = RunStore(conn)
    r1 = runs.claim("o/r", "issue", 12, "slugify")
    assert r1 is not None
    assert runs.claim("o/r", "issue", 12, "slugify") is None          # in flight
    runs.set_state(r1, RunState.EXPLORING)
    runs.set_state(r1, RunState.IMPLEMENTING)
    runs.set_state(r1, RunState.TESTING)
    runs.set_state(r1, RunState.AWAITING_APPROVAL)
    assert runs.claim("o/r", "issue", 12, "slugify") is None          # pending handoff still active
    runs.set_state(r1, RunState.CLOSED, "rejected")
    assert runs.claim("o/r", "issue", 12, "slugify") is not None      # terminal → claimable

def test_illegal_transition_rejected(conn):
    runs = RunStore(conn)
    r = runs.claim("o/r", "issue", 1, "t")
    with pytest.raises(ValueError):
        runs.set_state(r, RunState.PR_OPENED)

def test_recover_crashed_marks_in_flight_failed(conn):
    runs = RunStore(conn)
    r = runs.claim("o/r", "issue", 2, "t")
    runs.set_state(r, RunState.EXPLORING)
    crashed = runs.recover_crashed()
    assert crashed == [r]
    row = runs.get(r)
    assert row["state"] == RunState.FAILED and row["reason"] == "crashed"

def test_one_pending_handoff_per_issue(conn):
    runs, hs = RunStore(conn), HandoffStore(conn)
    r = runs.claim("o/r", "issue", 3, "t")
    h = hs.create(r, "o/r", "#3", "publish", {"files": []})
    with pytest.raises(sqlite3.IntegrityError):
        hs.create(r, "o/r", "#3", "continue_fix", {})
    hs.decide(h, "approve", by="liji")
    assert hs.get(h)["status"] == "approved"
    assert hs.pending() == []

def test_states_sets():
    assert RunState.AWAITING_APPROVAL in ACTIVE and RunState.CLOSED in TERMINAL
    assert can_transition(RunState.TESTING, RunState.AWAITING_APPROVAL)
    assert can_transition(RunState.IMPLEMENTING, RunState.AWAITING_APPROVAL)
    assert not can_transition(RunState.AWAITING_APPROVAL, RunState.IMPLEMENTING)
