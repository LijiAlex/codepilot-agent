# tests/test_triage.py
import pytest
from langchain_core.messages import AIMessage

from codepilot.agents.models import make_model, register_profiles
from codepilot.agents.triage import (ComplexPlan, TriageError, TriageResult, needs_plan, plan_as_todos, propose_plan,
                                     triage)
from tests.fakes import FakeClaude, tool_call

TRIAGE = {"task_type": "bug_fix", "complexity": 3, "complexity_reasons": ["one function"],
          "summary": "slugify must handle empty titles", "identifiers": ["slugify", "pkg/slugs.py"]}
PLAN = {"score": 1, "reasons": ["touches storage and API"], "steps": ["add column", "migrate", "expose in API"],
        "likely_files": ["pkg/store.py"], "risks": ["data migration"], "est_cost_usd": 2.5, "est_minutes": 25}


def test_triage_parses_structured_output():
    m = FakeClaude(responses=[tool_call("TriageResult", TRIAGE, "1")])
    r = triage(m, "slugify('') crashes", "pkg/slugs.py: slugify")
    assert r == TriageResult(**TRIAGE)


def test_triage_retries_once_then_fails():
    bad = {**TRIAGE, "task_type": "refactor"}
    ok = FakeClaude(responses=[tool_call("TriageResult", bad, "1"), tool_call("TriageResult", TRIAGE, "2")])
    assert triage(ok, "x", "").task_type == "bug_fix"
    never = FakeClaude(responses=[AIMessage(content="I think it is a bug"), AIMessage(content="still prose")])
    with pytest.raises(TriageError):
        triage(never, "x", "")


def test_threshold_and_plan_bypass():
    r = TriageResult(**{**TRIAGE, "complexity": 8})
    assert needs_plan(r, threshold=6) and not needs_plan(r, threshold=6, plan_approved=True)
    assert not needs_plan(TriageResult(**TRIAGE), threshold=6)


def test_plan_proposal_takes_score_from_triage_and_seeds_todos():
    r = TriageResult(**{**TRIAGE, "complexity": 8})
    p = propose_plan(FakeClaude(responses=[tool_call("ComplexPlan", PLAN, "1")]), "add tags", "map", r)
    assert isinstance(p, ComplexPlan) and p.score == 8
    t = plan_as_todos(p)
    assert "write_todos" in t and "1. add column" in t and "pkg/store.py" in t


def test_real_model_construction_and_profile_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    m = make_model("anthropic:claude-sonnet-5-5")
    assert register_profiles(m) == "anthropic"
    assert register_profiles(FakeClaude(responses=[])) == "anthropic"     # fakes resolve the same profile


def test_triage_and_gap_report_calls_carry_the_tracer_and_a_kind():
    from langchain_core.callbacks import BaseCallbackHandler
    from codepilot.agents.triage import triage
    from codepilot.improve.gaps import build_report

    class Seen(BaseCallbackHandler):
        def __init__(self): self.kinds = []
        def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kw):
            self.kinds.append((metadata or {}).get("kind"))
    seen = Seen()
    tr = {"task_type": "bug_fix", "complexity": 2, "skill": "bug_fix_skill", "reasons": ["x"], "summary": "s"}
    try:
        triage(FakeClaude(responses=[tool_call("TriageResult", tr, "1")]), "issue", "map",
               config={"callbacks": [seen], "metadata": {"kind": "triage", "run_id": "r1"}})
    except Exception:
        pass                                                         # schema details are covered elsewhere; only the call matters
    gap = {"summary": "s", "proposals": []}
    trace = {"outcome": "x", "reason": None, "first_try_test_pass": None, "changed_files": []}
    build_report(FakeClaude(responses=[tool_call("GapReport", gap, "1")]), trace, "",
                 config={"callbacks": [seen], "metadata": {"kind": "skill-gap-report", "run_id": "r1"}})
    assert "triage" in seen.kinds and "skill-gap-report" in seen.kinds
