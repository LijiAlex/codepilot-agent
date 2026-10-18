# tests/test_dashboard.py
import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from codepilot.audit import AuditLog
from codepilot.dashboard.app import DashboardDeps, create_app
from codepilot.db import connect
from codepilot.events import EventRecorder, RunEvent
from codepilot.handoffs import HandoffStore
from codepilot.runs import RunStore
from codepilot.skills import load_skills
from codepilot.states import RunState

PORT, TOKEN = 8765, "t0k3n"
H = {"host": f"127.0.0.1:{PORT}", "x-codepilot-token": TOKEN}
POST_H = {**H, "origin": f"http://127.0.0.1:{PORT}"}


class FakePR:
    def __init__(self, n, head="h1", labels=("codepilot-generated", "merge-ready"), review_sha="h1", mergeable=True,
                 verdict="**Merge-ready:** yes"):
        self.number, self.title, self.html_url = n, "<img src=x onerror=alert(1)>", f"https://github.com/o/r/pull/{n}"
        self.user, self.head = NS(login="alice"), NS(sha=head)
        self.labels = [NS(name=l) for l in labels]
        self.mergeable, self.merged_with, self.state = mergeable, None, "open"
        self._reviews = [NS(user=NS(login="rev[bot]"), commit_id=review_sha, body=f"**Score:** 9/10\n{verdict}", state="COMMENTED")]
    def get_reviews(self): return self._reviews
    def merge(self, sha, merge_method):
        if self.number == 6:
            from github import GithubException
            raise GithubException(405, {"message": "Pull Request is not mergeable"})
        self.merged_with = (sha, merge_method); return NS(merged=True, sha="m1", message="merged")
    def edit(self, state): self.state = state


class FakeRepo:
    def __init__(self, prs): self.prs = {p.number: p for p in prs}
    def get_pulls(self, state): return list(self.prs.values())
    def get_pull(self, n): return self.prs[n]


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "cp.db")
    runs, handoffs, audit = RunStore(conn), HandoffStore(conn), AuditLog(conn)
    rid = runs.claim("o/r", "issue", 7, "slugify crashes")
    runs.update(rid, task_type="bug_fix", complexity=3)
    rec = EventRecorder(tmp_path / "runs" / rid / "events.jsonl")
    for st in ("EXPLORING", "IMPLEMENTING", "TESTING"):
        runs.set_state(rid, RunState(st)); rec.write(RunEvent(rid, "state", "worker", st))
    rec.write(RunEvent(rid, "notice", "worker", "context", {"kind": "context", "map_tokens": 812, "map_files": 9, "lessons": 2, "skill": "bug_fix_skill"}))
    runs.set_state(rid, RunState.AWAITING_APPROVAL)
    hid = handoffs.create(rid, "o/r", "#7", "publish", {"diff": "+fix", "saved": [{"content_b64": "c2VjcmV0"}]})
    handoffs.decide(hid, "approve", "liji")
    runs.update(rid, pr_url="https://github.com/o/r/pull/3", pr_number=3); runs.set_state(rid, RunState.PR_OPENED)
    audit.log("agent:coder", "command_refused", run_id=rid, target="git push origin main", result="refused")
    repo = FakeRepo([FakePR(3), FakePR(4, labels=("codepilot-generated",)), FakePR(5, head="h2", review_sha="h1"), FakePR(6)])
    deps = DashboardDeps(conn=conn, runs_dir=tmp_path / "runs", results_dir=tmp_path / "results", repos=["o/r"],
                         repo_factory=lambda n: repo, audit=audit, reviewer_login="rev[bot]", skills=load_skills())
    return NS(client=TestClient(create_app(deps, TOKEN, PORT)), rid=rid, repo=repo, conn=conn)


def test_token_host_and_origin_are_enforced(env):
    c = env.client
    assert c.get("/api/overview", headers={"host": H["host"]}).status_code == 401
    assert c.get("/api/overview", headers={**H, "x-codepilot-token": "wrong"}).status_code == 401
    assert c.get("/api/overview", headers={**H, "host": "evil.example:8765"}).status_code == 400
    body = {"expected_head": "h1", "confirm": True}
    assert c.post("/api/prs/o/r/3/merge", headers={**H, "origin": "https://evil.example"}, json=body).status_code == 403
    r = c.get("/", headers={"host": H["host"]})
    assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_read_pages(env):
    c = env.client
    ov = c.get("/api/overview", headers=H).json()
    assert ov["runs"] == 1 and ov["prs_opened"] == 1 and ov["commands_refused"] == 1
    mx = c.get("/api/matrix", headers=H).json()
    marks = mx["rows"][0]["marks"]
    assert marks["Explored"] == marks["Tested"] == marks["File check"] == marks["Approved"] == marks["PR opened"] == "done" and marks["Merged"] == ""
    run = c.get(f"/api/runs/{env.rid}", headers=H).json()
    assert run["context"]["map_tokens"] == 812 and "saved" not in run["handoffs"][0]["payload"]      # file contents never sent
    assert c.get("/api/runs/nope", headers=H).status_code == 404
    assert c.get("/api/safety", headers=H).json()["commands_refused"][0]["target"] == "git push origin main"
    assert c.get("/api/context", headers=H).json()["runs"][0]["context"]["lessons"] == 2
    assert len(c.get("/api/skills", headers=H).json()["brief_skills"]) == 5
    assert c.get("/api/audit", headers=H).json()["events"]
    assert c.get("/api/improvements", headers=H).json() == {"proposals": [], "reports": [], "trend": []}
    prs = c.get("/api/prs", headers=H).json()["o/r"]
    assert prs[0]["title"] == "<img src=x onerror=alert(1)>"        # returned as data; the page uses textContent


def test_merge_only_when_merge_ready_current_and_unchanged(env):
    c = env.client
    ok = c.post("/api/prs/o/r/3/merge", headers=POST_H, json={"expected_head": "h1", "confirm": True})
    assert ok.status_code == 200 and env.repo.prs[3].merged_with == ("h1", "squash")
    assert c.post("/api/prs/o/r/4/merge", headers=POST_H, json={"expected_head": "h1", "confirm": True}).status_code == 409
    stale = c.post("/api/prs/o/r/5/merge", headers=POST_H, json={"expected_head": "h2", "confirm": True})
    assert stale.status_code == 409 and stale.json()["detail"] == "the independent review is not for the current commit"
    moved = c.post("/api/prs/o/r/3/merge", headers=POST_H, json={"expected_head": "zzz", "confirm": True})
    assert moved.status_code == 409 and "changed" in moved.json()["detail"]
    assert c.post("/api/prs/o/r/3/close", headers=POST_H, json={"expected_head": "h1", "confirm": False}).status_code == 400
    assert c.post("/api/prs/x/y/3/merge", headers=POST_H, json={"expected_head": "h1", "confirm": True}).status_code == 404
    gh_refused = c.post("/api/prs/o/r/6/merge", headers=POST_H, json={"expected_head": "h1", "confirm": True})
    assert gh_refused.status_code == 409 and gh_refused.json()["detail"] == "GitHub refused: GithubException"
    actions = [r[0] for r in env.conn.execute("select action from events where actor='human:dashboard'")]
    assert "pr_merge" in actions and "pr_merge_refused" in actions


def test_page_never_builds_html_from_data():
    js = (Path(__file__).parent.parent / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|eval\(|new Function", js)
    assert "X-CodePilot-Token" in js


def test_cli_dashboard_binds_localhost_and_prints_token_link(tmp_path, monkeypatch, capsys):
    import codepilot.cli as cli
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text(f"repos:\n  - owner: o\n    name: r\nhome: {tmp_path / 'home'}\nreviewer_login: rev[bot]\n")
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, log_level: seen.update(host=host, port=port, app=app))
    assert cli.main(["--config", str(cfg), "dashboard", "--port", "8799"]) == 0
    out = capsys.readouterr().out
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8799
    assert re.search(r"http://127\.0\.0\.1:8799/#token=[A-Za-z0-9_-]{20,}", out)


def test_dashboard_thread_shares_the_engine_services(tmp_path, monkeypatch):
    import codepilot.cli as cli
    from codepilot.config import RepoConfig, Settings
    started = {}
    class FakeServer:
        def __init__(self, config): started["host"], started["port"] = config.host, config.port
        def run(self): started["ran"] = True
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    conn = connect(tmp_path / "cp.db")
    eng = NS(worker=NS(s=NS(runs=RunStore(conn), audit=AuditLog(conn), lessons="LESSONS", episodic=None)),
             cs=NS(publish=NS(repo_factory=lambda n: None)), improver=None)
    url, thread = cli.start_dashboard_thread(eng, Settings(repos=[RepoConfig(owner="o", name="r")], home=tmp_path), tmp_path, 8771)
    thread.join(2)
    assert url.startswith("http://127.0.0.1:8771/#token=") and started == {"host": "127.0.0.1", "port": 8771, "ran": True}


def test_merge_needs_the_current_review_to_say_merge_ready_and_a_configured_reviewer():
    from codepilot.dashboard.data import MergeRefused, merge_pr
    stale_label = FakeRepo([FakePR(8, verdict="**Merge-ready:** no — score 4 < 7")])
    with pytest.raises(MergeRefused):
        merge_pr(stale_label, 8, "h1", "rev[bot]")
    with pytest.raises(MergeRefused):
        merge_pr(FakeRepo([FakePR(9)]), 9, "h1", None)
    assert merge_pr(FakeRepo([FakePR(10)]), 10, "h1", "rev[bot]")["merged"]


def test_overview_says_cost_is_not_tracked_and_counts_waiting_skill_proposals(env):
    ov = env.client.get("/api/overview", headers=H).json()
    assert ov["cost_usd"] is None                                   # no prices configured: not "$0"
    assert ov["proposals_waiting"] == 0 and "pending_decisions" in ov
    from codepilot.dashboard.data import overview
    env.conn.execute("update runs set cost_usd=0.42 where id=?", (env.rid,))
    assert overview(env.conn, cost_tracked=True)["cost_usd"] == 0.42  # prices set in codepilot.yml


def test_context_reports_the_repo_map_budget(env):
    assert env.client.get("/api/context", headers=H).json()["budget"] == 4000


def test_page_has_friendly_labels_loading_state_and_no_html_from_data():
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert '"Pull requests"' in js and "Loading" in js and "None so far" in js and "not tracked" in js
    assert "needs an independent review" in js and "toLocaleString" in js


def test_dashboard_token_survives_a_restart_and_is_private(tmp_path, monkeypatch, capsys):
    import os, stat
    import codepilot.cli as cli
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text(f"repos:\n  - owner: o\n    name: r\nhome: {tmp_path / 'home'}\n")
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, log_level: None)
    cli.main(["--config", str(cfg), "dashboard", "--port", "8799"]); first = capsys.readouterr().out
    cli.main(["--config", str(cfg), "dashboard", "--port", "8799"]); second = capsys.readouterr().out
    tok = lambda s: re.search(r"#token=([A-Za-z0-9_-]+)", s).group(1)
    assert tok(first) == tok(second)                                 # an open tab keeps working after a restart
    f = tmp_path / "home" / "dashboard-token"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_page_explains_an_out_of_date_link():
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert "access link is out of date" in js


def test_run_detail_includes_the_logged_error_and_safety_names_missed_attacks(env, tmp_path):
    env.client.app  # noqa: B018
    from codepilot.audit import AuditLog
    AuditLog(env.conn).log("worker", "run_error", run_id=env.rid, result="error", detail={"type": "SandboxSetupError", "output_tail": "ERROR: requires a different Python"})
    run = env.client.get(f"/api/runs/{env.rid}", headers=H).json()
    assert any("requires a different Python" in e for e in run["errors"])
    res = tmp_path / "results"; res.mkdir(exist_ok=True)
    (res / "guardrail.json").write_text(json.dumps({"detected": 1, "malicious": 2, "false_alarms": 0, "benign": 1, "failed_closed": 0,
        "p50_ms": 5, "rows": [{"id": "m1", "label": "malicious", "blocked": True}, {"id": "m2", "label": "malicious", "blocked": False}]}))
    from codepilot.dashboard.data import safety
    g = safety(env.conn, res)["guardrail_measurement"]
    assert g["missed"] == ["m2"] and g["measured_at"]


def test_page_routes_by_url_and_shows_local_times():
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert 'addEventListener("hashchange", route)' in js and "local time" in js and "friendlySummary" in js


def test_page_files_are_not_cached_so_updates_show(env):
    r = env.client.get("/static/app.js", headers={"host": H["host"]})
    assert r.status_code == 200 and r.headers.get("cache-control", "") in ("no-store", "no-cache")
    assert env.client.get("/", headers={"host": H["host"]}).headers.get("cache-control", "") in ("no-store", "no-cache")


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
def test_friendly_summary_handles_nested_brackets_and_newlines():
    import subprocess
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    grab = lambda name: re.search(r"(function " + name + r"\(.*?\n\})", js, re.S).group(1)
    prog = "const STATUS = {FAILED: 'failed', PR_OPENED: 'PR opened', AWAITING_APPROVAL: 'waiting for approval'};\n" + \
        grab("status") + "\n" + grab("friendlySummary") + "\n" + \
        "console.log(friendlySummary('FAILED(setup: box command failed (1): cd …\\nObtaining file:///x\\n  Checking if build backen): 1 | PR_OPENED: 2'))"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout.strip()
    assert out.startswith("failed (setup: box command failed (1): cd") and out.endswith("×1 | PR opened ×2") and "FAILED" not in out


def test_cost_comes_from_langsmith_traces_matched_by_run_id(env):
    from codepilot.dashboard import data
    class FakeLS:
        def list_runs(self, **kw):
            assert kw["project_name"] == "codepilot-plus" and kw["is_root"] is True
            return [NS(extra={"metadata": {"run_id": env.rid}}, total_cost=0.1234),
                    NS(extra={"metadata": {"run_id": env.rid}}, total_cost=0.0100),    # a continuation of the same run
                    NS(extra={"metadata": {}}, total_cost=9.0)]                       # not ours: ignored
    costs = data.langsmith_costs(FakeLS(), "codepilot-plus")
    assert costs == {env.rid: 0.1334}
    ov = data.overview(env.conn, cost_tracked=False, trace_costs=costs)
    assert ov["cost_usd"] == 0.13 and ov["cost_source"] == "LangSmith"
    row = data.matrix(env.conn, env.runs_dir if hasattr(env, "runs_dir") else Path("/nonexistent"), trace_costs=costs)["rows"][0]
    assert row["cost_usd"] == 0.1334


def test_dashboard_uses_trace_costs_when_available_and_survives_langsmith_errors(env, tmp_path):
    from codepilot.dashboard.app import DashboardDeps, create_app
    base = dict(conn=env.conn, runs_dir=tmp_path, results_dir=tmp_path, repos=["o/r"], repo_factory=lambda n: env.repo, audit=None)
    ok = TestClient(create_app(DashboardDeps(**base, trace_costs=lambda: {env.rid: 0.5}), TOKEN, PORT))
    assert ok.get("/api/overview", headers=H).json()["cost_usd"] == 0.5
    def down(): raise RuntimeError("langsmith unreachable")
    bad = TestClient(create_app(DashboardDeps(**base, trace_costs=down), TOKEN, PORT))
    assert bad.get("/api/overview", headers=H).json()["cost_usd"] is None      # page still loads
