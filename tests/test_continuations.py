# tests/test_continuations.py
from types import SimpleNamespace as NS

import pytest
from langchain_core.messages import AIMessage

from codepilot.audit import AuditLog
from codepilot.config import RepoConfig
from codepilot.continuations import ContinuationServices, decide, describe, skip
from codepilot.db import connect
from codepilot.handoffs import HandoffStore
from codepilot.publish import PublishServices
from codepilot.runs import RunStore
from codepilot.states import RunState
from tests.fakes import FakeClaude
from tests.test_publish import PubRepo, payload

REPO = RepoConfig(owner="o", name="r")


class Lessons:
    def __init__(self): self.added = []
    def add(self, *a): self.added.append(a)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "cp.db")
    e = NS(conn=conn, runs=RunStore(conn), handoffs=HandoffStore(conn), audit=AuditLog(conn), repo=PubRepo(), lessons=Lessons(),
           cancelled=[])
    ps = PublishServices(runs=e.runs, handoffs=e.handoffs, audit=e.audit, repo_factory=lambda n: e.repo,
                         make_model=lambda: FakeClaude(responses=[AIMessage(content="opened")]))
    e.cs = ContinuationServices(publish=ps, repo_config=lambda n: REPO, lessons=e.lessons, cancel_run=e.cancelled.append)
    return e


def waiting(env, entry, extra=None, state_path=(RunState.EXPLORING, RunState.IMPLEMENTING, RunState.TESTING)):
    rid = env.runs.claim("o/r", "issue", 7, "t")
    for st in state_path:
        env.runs.set_state(rid, st)
    env.runs.set_state(rid, RunState.AWAITING_APPROVAL)
    return rid, env.handoffs.create(rid, "o/r", "#7", entry, {**payload(), **(extra or {})})


def test_approve_publish_opens_pr_and_second_decision_is_stale(env):
    rid, hid = waiting(env, "publish")
    r = decide(env.cs, hid, "approve", "liji")
    assert r.status == "published" and env.runs.get(rid)["state"] == "PR_OPENED"
    assert decide(env.cs, hid, "approve", "liji").status == "stale"


def test_reject_publish_closes_and_records_a_lesson(env):
    rid, hid = waiting(env, "publish")
    r = decide(env.cs, hid, "reject", "liji", "wrong file changed")
    assert r.status == "closed" and env.runs.get(rid)["reason"] == "rejected"
    assert env.lessons.added and env.lessons.added[0][3] == "rejection"


def test_continue_fix_starts_a_third_attempt_with_the_saved_change(env):
    rid, hid = waiting(env, "continue_fix", {"prior_failures": 2, "test_report": {"failing_ids": ["tests/t.py::x"], "output": "boom"}})
    r = decide(env.cs, hid, "approve", "liji")
    assert r.status == "continued" and env.runs.get(rid)["reason"] == "continued"
    c = r.continuation
    assert c.seed.prior_failures == 2 and c.seed.saved[0]["path"] == "a.py" and "tests/t.py::x" in c.seed.notes
    assert env.runs.get(c.run_id)["state"] == "TRIAGED" and c.item.issue.number == 7


def test_unexpected_command_approve_and_reject_both_continue(env):
    gate = {"gate": "unexpected_command", "command": "make build", "agent": "coder", "reason": "unknown program"}
    _, hid = waiting(env, "continue_with_decision", {"gate": gate, "prior_failures": 1})
    ok = decide(env.cs, hid, "approve", "liji")
    assert ok.continuation.seed.approved_command == "make build" and ok.continuation.seed.prior_failures == 1
    env.runs.set_state(ok.continuation.run_id, RunState.FAILED, "x")
    _, hid2 = waiting(env, "continue_with_decision", {"gate": gate})
    no = decide(env.cs, hid2, "reject", "liji")
    assert no.continuation.seed.approved_command is None and "refused the command `make build`" in no.continuation.seed.notes


def test_complex_plan_approve_seeds_plan_and_time_cap_reject_skips(env):
    plan = {"score": 8, "reasons": ["x"], "steps": ["add column", "migrate"], "likely_files": ["a.py"], "risks": ["r"],
            "est_cost_usd": 2.0, "est_minutes": 20}
    rid, hid = waiting(env, "continue_complex", {"plan": plan}, state_path=())
    r = decide(env.cs, hid, "approve", "liji")
    s = r.continuation.seed
    assert s.plan_approved and "1. add column" in s.plan_text and s.time_cap_minutes == 30 and s.base_sha is None
    env.runs.set_state(r.continuation.run_id, RunState.FAILED, "x")
    rid2, hid2 = waiting(env, "continue_complex", {"plan": plan}, state_path=())
    assert decide(env.cs, hid2, "reject", "liji").status == "skipped" and env.runs.get(rid2)["state"] == "SKIPPED"


def test_continuation_on_closed_issue_is_stale(env):
    env.repo.issue.state = "closed"
    rid, hid = waiting(env, "continue_fix", {"prior_failures": 2})
    assert decide(env.cs, hid, "approve", "liji").status == "stale"
    assert env.runs.get(rid)["state"] == "CLOSED" and env.handoffs.get(hid)["status"] == "expired"


def test_describe_shows_what_a_person_needs(env):
    _, hid = waiting(env, "continue_with_decision", {"gate": {"gate": "unexpected_command", "command": "make build",
                                                               "agent": "coder", "reason": "unknown program"}, "diff": "+x"})
    text = describe(env.handoffs, hid)
    assert "Command: make build" in text and "Asked by: coder" in text and "+x" in text


def test_skip_in_flight_cancels_and_skip_waiting_expires(env):
    rid = env.runs.claim("o/r", "issue", 8, "t")
    assert skip(env.cs, "o/r", "#8", "liji") == "stopping the run" and env.cancelled == [rid]
    env.runs.set_state(rid, RunState.FAILED, "x")
    rid2, hid = waiting(env, "publish")
    assert skip(env.cs, "o/r", "#7", "liji") == "skipped"
    assert env.runs.get(rid2)["state"] == "SKIPPED" and env.handoffs.get(hid)["status"] == "expired"
    assert skip(env.cs, "o/r", "#99", "liji") == "nothing active for this issue"


def test_rejecting_a_retry_fails_the_run_with_a_lesson(env):
    rid, hid = waiting(env, "continue_fix", {"prior_failures": 2})
    r = decide(env.cs, hid, "reject", "liji", "approach is wrong")
    assert r.status == "failed" and env.runs.get(rid)["state"] == "FAILED" and env.runs.get(rid)["reason"] == "rejected"
    assert env.lessons.added[-1][3] == "failure" and "approach is wrong" in env.lessons.added[-1][2]


def test_unexpected_error_puts_the_item_back(env):
    rid, hid = waiting(env, "publish")
    def boom(name): raise ConnectionError("github unreachable")
    env.cs.publish.repo_factory = boom
    r = decide(env.cs, hid, "approve", "liji")
    assert r.status == "error" and env.handoffs.get(hid)["status"] == "pending" and env.runs.get(rid)["state"] == "AWAITING_APPROVAL"


def test_continuation_superseded_when_the_poller_claimed_first(env, monkeypatch):
    rid, hid = waiting(env, "continue_fix", {"prior_failures": 2})
    monkeypatch.setattr(env.runs, "claim", lambda *a, **k: None)
    r = decide(env.cs, hid, "approve", "liji")
    assert r.status == "superseded" and r.continuation is None and env.runs.get(rid)["state"] == "CLOSED"


def test_approved_command_parts_carry_through_later_continuations(env):
    # live 2026-10-19, issue #9: the approval must cover `make lint` for the rest of the run, not one exact string
    gate = {"gate": "unexpected_command", "command": "cd sandbox && make lint; echo exit=$?", "agent": "coder", "reason": "x"}
    _, hid = waiting(env, "continue_with_decision", {"gate": gate, "approved": ["make build"]})
    ok = decide(env.cs, hid, "approve", "liji")
    assert ok.continuation.seed.approved == ["make build", "make lint"]
    env.runs.set_state(ok.continuation.run_id, RunState.FAILED, "x")
    _, hid2 = waiting(env, "continue_with_decision", {"gate": {**gate, "command": "make docs"}, "approved": ["make lint"]})
    no = decide(env.cs, hid2, "reject", "liji")
    assert no.continuation.seed.approved == ["make lint"]                          # a refusal keeps earlier approvals
    env.runs.set_state(no.continuation.run_id, RunState.FAILED, "x")
    _, hid3 = waiting(env, "continue_fix", {"prior_failures": 2, "approved": ["make lint"], "test_report": {}})
    assert decide(env.cs, hid3, "approve", "liji").continuation.seed.approved == ["make lint"]
