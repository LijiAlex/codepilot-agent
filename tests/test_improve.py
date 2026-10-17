# tests/test_improve.py
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from codepilot.audit import AuditLog
from codepilot.config import RepoConfig
from codepilot.dashboard.app import DashboardDeps, create_app
from codepilot.db import connect
from codepilot.events import EventRecorder, RunEvent
from codepilot.handoffs import HandoffStore
from codepilot.improve.gaps import GapReport, ImprovementStore, Improver, Proposal, build_report
from codepilot.improve.repo_skill import RepoSkillStore
from codepilot.improve.trace import analyse_run
from codepilot.repomap.build import FileEntry
from codepilot.runs import RunStore
from codepilot.states import RunState
from tests.fakes import FakeClaude, tool_call

CFG = RepoConfig(owner="o", name="r", install_command="pip install -e .", test_command="python -m pytest -q",
                 protected_paths=[".claude-plugin/**"])
FILES = [FileEntry("pkg/slugs.py", "python", "slug helpers"), FileEntry("README.md", "markdown", "readme"),
         FileEntry("pkg/dates.py", "python", "date parsing")]


def test_repo_skill_v1_render_and_delta_versions(tmp_path):
    st = RepoSkillStore(connect(tmp_path / "cp.db"))
    assert st.render("o/r") == (0, "")
    assert st.ensure_v1(CFG, FILES) == 1 and st.ensure_v1(CFG, FILES) == 1                # created once
    v, text = st.render("o/r")
    assert v == 1 and "`pip install -e .`" in text and "`pkg/slugs.py`: slug helpers" in text and "README.md" not in text
    assert ".claude-plugin/**" in text
    assert st.apply("o/r", "add", "gotchas", "Tests need PYTHONPATH=src.", None, "run1") == 2
    _, steps = st.current("o/r")
    sid = next(s.id for s in steps if s.text.startswith("Tests need"))
    assert st.apply("o/r", "correct", "gotchas", "Tests need `pip install -e .` first.", sid, "run2") == 3
    _, text3 = st.render("o/r")
    assert "Tests need PYTHONPATH" not in text3 and "first." in text3                      # corrected step replaced, old kept retired
    assert st.apply("o/r", "retire", "locations", "", "s3", "run3") == 4
    assert st.rollback("o/r", 2) == 5 and "Tests need PYTHONPATH" in st.render("o/r")[1]
    assert [h["version"] for h in st.history("o/r")] == [1, 2, 3, 4, 5]
    with pytest.raises(ValueError):
        st.apply("o/r", "correct", "gotchas", "x", "s99", "r")


def seeded_run(tmp_path):
    conn = connect(tmp_path / "cp.db")
    runs, handoffs = RunStore(conn), HandoffStore(conn)
    rid = runs.claim("o/r", "issue", 7, "slugify crashes"); runs.update(rid, task_type="bug_fix", tokens_in=1000, tokens_out=200)
    rec = EventRecorder(tmp_path / "runs" / rid / "events.jsonl")
    ev = [RunEvent(rid, "notice", "worker", "context", {"kind": "context", "repo_skill_version": 1, "map_tokens": 500}, ts=100.0),
          RunEvent(rid, "tool_call", "repo-explorer", "read_file", {"name": "read_file", "args": {"file_path": "/sandbox/pkg/dates.py"}, "id": "a"}, ts=101.0),
          RunEvent(rid, "tool_call", "repo-explorer", "read_file", {"name": "read_file", "args": {"file_path": "/sandbox/pkg/slugs.py"}, "id": "b"}, ts=102.0),
          RunEvent(rid, "tool_call", "coder", "execute", {"name": "execute", "args": {"command": "pytest -q"}, "id": "c"}, ts=103.0),
          RunEvent(rid, "tool_result", "coder", "No module named pkg", {"name": "execute", "status": "success", "tool_call_id": "c", "exit_code": 2}, ts=104.0),
          RunEvent(rid, "tool_call", "test-agent", "run_tests", {"name": "run_tests", "args": {"purpose": "verify"}, "id": "d"}, ts=105.0),
          RunEvent(rid, "tool_result", "test-agent", '{"outcome": "passed", "passed": 3}\n', {"name": "run_tests", "status": "success", "tool_call_id": "d"}, ts=220.0)]
    for e in ev:
        rec.write(e)
    runs.set_state(rid, RunState.AWAITING_APPROVAL)
    handoffs.create(rid, "o/r", "#7", "publish", {"saved": [{"path": "pkg/slugs.py"}]})
    AuditLog(conn).log("agent:coder", "command_refused", run_id=rid, target="pip install x", result="refused")
    return conn, rid


def test_trace_analysis_from_audit_log_and_events(tmp_path):
    conn, rid = seeded_run(tmp_path)
    t = analyse_run(conn, tmp_path / "runs", rid)
    assert t["failed_commands"] == [{"agent": "coder", "command": "pytest -q", "exit_code": 2}]
    assert t["refused_commands"] == ["pip install x"] and t["files_read_before_right_one"] == 1
    assert t["first_try_test_pass"] is True and t["repo_skill_version"] == 1 and t["minutes"] == 2.0 and t["tokens"] == 1200


GAP = {"followed": ["s2"], "missing": ["how to make pkg importable"], "wrong": [], "unused": [],
       "proposals": [{"op": "add", "section": "gotchas", "text": "Run tests from the repository folder: `cd sandbox && python -m pytest -q`.",
                      "evidence": "pytest -q exit 2: No module named pkg"},
                     {"op": "add", "section": "setup", "text": "Put the API token in .env", "evidence": "x"}]}


def test_report_drops_forbidden_proposals_and_recurrence_raises_confidence(tmp_path):
    conn, rid = seeded_run(tmp_path)
    t = analyse_run(conn, tmp_path / "runs", rid)
    rep = build_report(FakeClaude(responses=[tool_call("GapReport", GAP, "1")]), t, "skill text")
    assert [p.section for p in rep.proposals] == ["gotchas"]                         # the .env/token one is filtered by code
    bad = {**GAP, "proposals": [{"op": "add", "section": "setup", "text": "Run `pip install requests` first", "evidence": "x"}]}
    assert build_report(FakeClaude(responses=[tool_call("GapReport", bad, "2")]), t, "").proposals == []   # agents never install
    skills = RepoSkillStore(conn); skills.ensure_v1(CFG, FILES)
    store = ImprovementStore(conn, skills)
    a = store.record_report("o/r", rid, rep)
    b = store.record_report("o/r", "run2", rep)
    assert a == b
    view = store.view(["o/r"])
    assert view["proposals"][0]["hits"] == 2 and view["proposals"][0]["confidence"] == 0.67
    assert store.decide(a[0], "approve", "dashboard") == {"status": "approved", "version": 2}
    assert store.decide(a[0], "approve", "dashboard") == {"status": "stale"}
    store.record_trace(t)
    assert store.trend(["o/r"]) == [{"repo": "o/r", "version": 1, "runs": 1, "failed_commands": 1.0, "tool_calls": 4.0, "first_try_pass": 1.0}]


def test_end_to_end_worker_run_then_improver_then_dashboard_approval(tmp_path):
    from tests.test_worker import base_scripts, claim, item, services
    from codepilot.worker import Worker
    import tests.test_worker as tw
    env = tw.env.__wrapped__(tmp_path)
    s = services(env, base_scripts())
    skills = RepoSkillStore(s.runs.conn)
    s.repo_skill = lambda cfg, files: (skills.ensure_v1(cfg, files), skills.render(cfg.full_name))[1]
    it = item(env); rid = claim(env, it)
    assert Worker(s).run(it, rid).reason == "publish"
    store = ImprovementStore(s.runs.conn, skills)
    imp = Improver(s.runs.conn, env["home"] / "runs", store, lambda: FakeClaude(responses=[tool_call("GapReport", GAP, "1")]), s.audit)
    out = imp.after_run(rid)
    assert out["trace"]["repo_skill_version"] == 1 and (env["home"] / "runs" / rid / "skill_gap_report.json").exists()
    deps = DashboardDeps(conn=s.runs.conn, runs_dir=env["home"] / "runs", results_dir=tmp_path, repos=["o/r"],
                         repo_factory=lambda n: None, audit=s.audit, improvements=store)
    c = TestClient(create_app(deps, "t", 8765))
    h = {"host": "127.0.0.1:8765", "x-codepilot-token": "t", "origin": "http://127.0.0.1:8765"}
    pid = c.get("/api/improvements", headers=h).json()["proposals"][0]["id"]
    assert c.post(f"/api/improvements/{pid}/approve", headers=h, json={"confirm": True}).json()["version"] == 2
    assert [v["version"] for v in c.get("/api/skills", headers=h).json()["repo_skills"]["o/r"]] == [1, 2]
    assert "cd sandbox && python -m pytest -q" in skills.render("o/r")[1]


def test_a_double_click_applies_an_approved_change_once(tmp_path):
    conn = connect(tmp_path / "cp.db")
    skills = RepoSkillStore(conn); skills.ensure_v1(CFG, FILES)
    store = ImprovementStore(conn, skills)
    rep = GapReport(summary="s", proposals=[Proposal(op="add", section="gotchas", text="Run tests from the repo root", evidence="e")])
    pid = store.record_report("o/r", "run1", rep)[0]
    applied, real_apply, nested = [], skills.apply, []

    def apply(*a):                                   # the second click lands while the first is applying
        applied.append(a)
        nested.append(store.decide(pid, "approve", "dashboard"))
        return real_apply(*a)
    skills.apply = apply
    assert store.decide(pid, "approve", "dashboard")["status"] == "approved"
    assert len(applied) == 1 and nested == [{"status": "stale"}]
