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
    assert out.startswith("Failed (setup: box command failed (1): cd")   # status() uses sentence case and out.endswith("×1 | PR opened ×2") and "FAILED" not in out


def test_cost_comes_from_langsmith_traces_matched_by_run_id(env):
    from codepilot.dashboard import data
    class FakeLS:
        def list_runs(self, **kw):
            assert kw["project_name"] == "codepilot-plus" and kw["is_root"] is True and kw["limit"] <= 100   # LangSmith max
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


def test_usage_from_langsmith_splits_agents_screen_and_reviewer(env):
    from codepilot.dashboard import data
    class FakeLS:
        def list_runs(self, project_name, is_root, limit):
            if project_name == "codepilot-plus":
                return [NS(extra={"metadata": {"run_id": env.rid}}, total_cost=0.2, prompt_tokens=1000, completion_tokens=50),
                        NS(extra={"metadata": {"kind": "injection-screen"}}, total_cost=0.001, prompt_tokens=300, completion_tokens=2)]
            return [NS(extra={"metadata": {"pr": 14}}, total_cost=0.05, prompt_tokens=4000, completion_tokens=400)]
    u = data.langsmith_usage(FakeLS(), "codepilot-plus", "codepilot-reviewer")
    assert u["runs"][env.rid] == {"count": 1, "cost": 0.2, "tokens_in": 1000, "tokens_out": 50}
    assert u["screen"] == {"count": 1, "cost": 0.001, "tokens_in": 300, "tokens_out": 2}
    assert u["reviewer"] == {"count": 1, "cost": 0.05, "tokens_in": 4000, "tokens_out": 400}
    ov = data.overview(env.conn, trace_usage=u)
    assert ov["usage"]["reviewer"]["cost"] == 0.05 and ov["cost_usd"] == 0.25 and ov["cost_source"] == "LangSmith"


REVIEW_BODY = ("### Independent review\n\nLooks right.\n\n**Score:** 6/10 · **Review effort:** 3/5\n**Matches the issue:** Mostly.\n\n"
               "**Security concerns:**\n- reads a token from env\n\n**Merge-ready:** no — score 6 < 7; needs a human approval\n\n"
               "> Tests not run: code from a fork is never executed by this reviewer, so this PR is never marked merge-ready automatically.")


def test_review_body_is_split_into_fields():
    from codepilot.dashboard.data import parse_review
    f = parse_review(REVIEW_BODY)
    assert (f["score"], f["effort"], f["merge_ready"]) == (6, 3, False)
    assert f["matches"] == "Mostly." and f["security"] == ["reads a token from env"] and f["tests_not_run"] is True
    assert f["why_not"] == "score 6 < 7; needs a human approval"


def test_prs_needing_a_person_are_flagged_with_reasons():
    from codepilot.dashboard.data import attention
    fork = {"from_fork": True, "codepilot": False, "review": {"current": True, "fields": parse(REVIEW_BODY)}}
    assert {"from a fork", "security concerns", "low score (6/10)"} <= set(attention(fork))
    assert "not reviewed yet" in attention({"from_fork": False, "codepilot": True, "review": None})
    clean = {"from_fork": False, "codepilot": True, "review": {"current": True, "fields": {"score": 10, "security": [], "merge_ready": True}}}
    assert attention(clean) == []


def parse(body):
    from codepilot.dashboard.data import parse_review
    return parse_review(body)


def _untested_run(env, tmp_path, passed: bool):
    runs = RunStore(env.conn)
    rid = runs.claim("o/r", "issue", 5, "raise the pydantic floor")
    runs.update(rid, task_type="dependency_update")
    rec = EventRecorder(tmp_path / "runs" / rid / "events.jsonl")
    rec.write(RunEvent(rid, "state", "worker", "TESTING"))
    if passed:
        rec.write(RunEvent(rid, "tool_result", "test-agent", '{"outcome": "passed", "passed": 193}', {"name": "run_tests"}))
    runs.set_state(rid, RunState.FAILED, reason="untested")
    return rid


def test_tested_mark_says_tests_passed_but_verification_was_voided(env, tmp_path):
    voided, never = _untested_run(env, tmp_path, True), _untested_run(env, tmp_path, False)
    rows = {r["run_id"]: r for r in env.client.get("/api/matrix", headers=H).json()["rows"]}
    assert rows[voided]["marks"]["Tested"] == "voided"           # tests ran and passed; a later change was not re-tested
    assert rows[never]["marks"]["Tested"] == "failed"
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert "voided:" in js and "not re-tested" in js


def test_standalone_dashboard_lists_lessons_read_only_from_chromas_file(tmp_path):
    from codepilot.dashboard.data import ReadOnlyLessons
    from codepilot.memory.semantic import LessonStore
    store = LessonStore(tmp_path / "chroma", lambda texts: [[float(len(t)), 1.0] for t in texts])
    store.add("o/r", "bug_fix", "run the slug tests first", "merged", "abc123")
    store.add("o/x", "bug_fix", "other repo", "failure", "def456")
    got = ReadOnlyLessons(tmp_path / "chroma").all("o/r")
    assert [(l["text"], l["source"], l["task_type"], l["run_id"]) for l in got] == [("run the slug tests first", "merged", "bug_fix", "abc123")]
    assert ReadOnlyLessons(tmp_path / "missing").all("o/r") == []          # no Chroma folder yet: empty, not an error


def test_cli_dashboard_wires_read_only_lessons(tmp_path, monkeypatch):
    import codepilot.cli as cli
    from codepilot.dashboard.data import ReadOnlyLessons
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text(f"repos:\n  - owner: o\n    name: r\nhome: {tmp_path / 'home'}\n")
    seen = {}
    monkeypatch.setattr("codepilot.dashboard.app.create_app", lambda deps, token, port: seen.setdefault("deps", deps))
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, log_level: None)
    cli.main(["--config", str(cfg), "dashboard", "--port", "8799"])
    assert isinstance(seen["deps"].lessons, ReadOnlyLessons)


def test_improvements_trend_labels_runs_without_a_skill():
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert "ended before the skill loaded" in js


def test_overview_summarises_skills_per_task_and_repo_skill_versions(env):
    from codepilot.improve.gaps import ImprovementStore
    from codepilot.improve.repo_skill import RepoSkillStore
    sk = env.client.get("/api/overview", headers=H).json()["skills"]
    assert sk["by_task"] == [{"task_type": "bug_fix", "runs": 1, "outcomes": {"PR_OPENED": 1}}]
    assert sk["repos"] == [{"repo": "o/r", "version": 0, "waiting": 0, "approved": 0}]      # no repo skill yet
    ImprovementStore(env.conn, RepoSkillStore(env.conn))
    env.conn.execute("insert into repo_skill_versions(repo, version, steps_json, change) values('o/r', 2, '[]', 'x')")
    env.conn.executemany("insert into repo_skill_proposals(id, repo, key, op, section, text, evidence, status) values(?, 'o/r', ?, 'add', 's', 't', 'e', ?)",
                         [("p1", "k1", "pending"), ("p2", "k2", "approved"), ("p3", "k3", "merged")])
    assert env.client.get("/api/overview", headers=H).json()["skills"]["repos"] == [{"repo": "o/r", "version": 2, "waiting": 1, "approved": 1}]


def test_runs_page_url_is_runs_and_old_matrix_links_still_work():
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert '["runs", "Runs"' in js and 'matrix: "runs"' in js and '"#matrix"' not in js


def test_reviewer_usage_counts_reviews_and_splits_them_per_pr():
    from codepilot.dashboard import data
    class FakeLS:
        def list_runs(self, project_name, is_root, limit):
            assert limit <= 100
            if project_name == "codepilot-plus":
                return [NS(extra={"metadata": {"kind": "injection-screen"}}, total_cost=0.001, prompt_tokens=300, completion_tokens=2)]
            review = lambda pr, cost: NS(name="independent-review", extra={"metadata": {"repo": "o/r", "pr": pr, "head": "h"}},  # noqa: E731
                                         total_cost=cost, prompt_tokens=1000, completion_tokens=100)
            return [review(16, 0.02), review(16, 0.03), review(15, 0.01)]
    u = data.langsmith_usage(FakeLS(), "codepilot-plus", "codepilot-reviewer")
    assert u["reviewer"]["count"] == 3 and u["screen"]["count"] == 1
    assert u["reviewer_by_pr"]["o/r#16"] == {"count": 2, "cost": 0.05, "tokens_in": 2000, "tokens_out": 200}
    assert u["reviewer_by_pr"]["o/r#15"]["count"] == 1


def test_prs_carry_review_usage_and_the_codepilot_run_that_opened_them(env, tmp_path):
    usage = {"runs": {}, "screen": {}, "reviewer": {"count": 2}, "reviewer_by_pr": {"o/r#3": {"count": 2, "cost": 0.05, "tokens_in": 9, "tokens_out": 1}}}
    deps = DashboardDeps(conn=env.conn, runs_dir=tmp_path, results_dir=tmp_path, repos=["o/r"], repo_factory=lambda n: env.repo,
                         audit=None, reviewer_login="rev[bot]", trace_usage=lambda: usage)
    prs = {p["number"]: p for p in TestClient(create_app(deps, TOKEN, PORT)).get("/api/prs", headers=H).json()["o/r"]}
    assert prs[3]["review_usage"]["count"] == 2 and prs[4]["review_usage"] is None
    assert prs[3]["run_id"] == env.rid and prs[4]["run_id"] is None          # the run's pr_url is .../pull/3


REAL_PR15_BODY = ("### Independent review\n\nThe README adds the requested library-usage section with policy loading, guardrail construction, "
                  "input checks, and supporting operational guidance.\n\n**Score:** 10/10 · **Review effort:** 1/5\n**Matches the issue:** "
                  "The change satisfies the linked issue by adding a section titled \"Using it as a library\".\n\n**Merge-ready:** yes\n\n"
                  "<sub>Reviewed by the CodePilot+ Independent Reviewer. It never merges.</sub>")


def test_real_reviewer_body_parses_score_effort_and_merge_ready():
    f = parse(REAL_PR15_BODY)                    # copied from PR #15 on the playground repo (middle dot, curly quotes elsewhere)
    assert (f["score"], f["effort"], f["merge_ready"], f["security"]) == (10, 1, True, [])


def test_run_detail_has_cost_and_runs_have_start_and_end_times(env, tmp_path):
    usage = {"runs": {env.rid: {"count": 1, "cost": 0.25, "tokens_in": 9, "tokens_out": 1}}, "screen": {}, "reviewer": {}, "reviewer_by_pr": {}}
    deps = DashboardDeps(conn=env.conn, runs_dir=tmp_path / "runs", results_dir=tmp_path, repos=["o/r"], repo_factory=lambda n: env.repo,
                         audit=None, trace_usage=lambda: usage)
    c = TestClient(create_app(deps, TOKEN, PORT))
    assert c.get(f"/api/runs/{env.rid}", headers=H).json()["cost_usd"] == 0.25
    row = c.get("/api/matrix", headers=H).json()["rows"][0]
    assert row["started"] and "ended" in row


def test_audit_links_pr_rows_to_the_run_that_opened_the_pr(env):
    AuditLog(env.conn).log("human:dashboard", "pr_merge", target="o/r#3", detail={"merged": True})
    ev = env.client.get("/api/audit", headers=H).json()["events"][0]
    assert ev["action"] == "pr_merge" and ev["run_id"] is None and ev["linked_run"] == env.rid


RUBRIC_BODY = ("### Independent review\n\nAdds the section.\n\n**Score:** 6/10 · **Review effort:** 1/5 (capped at 6: tests not run)\n"
               "**Matches the issue:** Yes.\n"
               "**Rubric:** correctness 2/2 · tests 1/2 · matches the issue 2/2 · risk 2/2 · scope 1/2\n"
               "- correctness: the example runs as written\n- tests: no test covers the README\n- matches the issue: adds the asked section\n"
               "- risk: docs only\n- scope: also reflows an unrelated paragraph\n\n**Merge-ready:** no — fork PR: tests not run; needs a human approval\n\n"
               "<sub>Reviewed by the CodePilot+ Independent Reviewer. It never merges.</sub>")


def test_rubric_points_reasons_and_cap_are_parsed():
    f = parse(RUBRIC_BODY)
    assert (f["score"], f["effort"], f["cap"]) == (6, 1, "capped at 6: tests not run")
    assert f["rubric"] == {"correctness": 2, "tests": 1, "matches the issue": 2, "risk": 2, "scope": 1}
    assert f["rubric_reasons"]["scope"] == "also reflows an unrelated paragraph" and f["matches"] == "Yes."
    assert parse(REAL_PR15_BODY)["rubric"] == {} and parse(REAL_PR15_BODY)["cap"] == ""        # older reviews have no rubric


def test_page_has_the_qa_review_improvements():
    static = Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static"
    js, css = (static / "app.js").read_text(), (static / "style.css").read_text()
    assert "Needs you" in js and "Outcomes" in js and "Learning" in js and "issues solved" in js and "Recent activity" not in js   # overview
    assert '"PR review")' in js and '"Review cost"' in js and "rubricCell" in js and "No open pull requests" in js  # reviewer + PRs
    assert "older" in js and "Never reached" in js and "duration(" in js and "filterInput" in js                  # runs
    assert "logRows" in js and "toLocaleTimeString" in js and "Jump to the diff" in js                             # run detail
    assert "Commands that differ only in spacing are grouped" in js and '"What", "Result"' not in js
    assert "person (dashboard)" in js and "Older →" in js and "linked_run" in js                                  # audit
    assert "Past decisions" in js and 'el("summary", "evidence")' in js                                          # improvements
    assert "Last refreshed" in js and "Auto-refresh" in js and js.count("local time") == 1                        # header, once
    assert "--link" in css and "@keyframes spin" in css


def test_self_improvement_calls_have_their_own_usage_line(env):
    from codepilot.dashboard import data
    class FakeLS:
        def list_runs(self, project_name, is_root, limit):
            if project_name == "codepilot-plus":
                return [NS(extra={"metadata": {"kind": "skill-gap-report", "for_run": env.rid}}, total_cost=0.03, prompt_tokens=900, completion_tokens=120)]
            return []
    u = data.langsmith_usage(FakeLS(), "codepilot-plus", "codepilot-reviewer")
    assert u["improvement"] == {"count": 1, "cost": 0.03, "tokens_in": 900, "tokens_out": 120} and u["runs"] == {}
    assert data.overview(env.conn, trace_usage=u)["cost_usd"] == 0.03
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert "Solver agents" in js and "self-improvement" in js.lower()


def test_safety_rows_name_the_guard_and_rule(env):
    from codepilot.audit import AuditLog
    from codepilot.dashboard.data import safety
    a = AuditLog(env.conn)
    a.log("agent:coder", "command_refused", run_id=env.rid, target="python -m pip show x", result="refused",
          detail={"reason": "python -m pip is not allowed"})                                   # old row: no rule stored
    a.log("agent:coder", "tool_refused", run_id=env.rid, target="task", result="refused")
    a.log("guardrail", "issue_blocked", run_id=env.rid, target="#9", result="blocked", detail={"reasons": ["regex check input"]})
    d = safety(env.conn, Path("/nonexistent"))
    assert d["commands_refused"][0]["guard"] == "Shell guard · pip-install"
    assert d["tools_refused"][0]["guard"] == "Tool allow-list"
    assert d["issues_blocked"][0]["guard"] == "Injection screen · NeMo regex rail"
    js = (Path(__file__).parents[1] / "src" / "codepilot" / "dashboard" / "static" / "app.js").read_text()
    assert '"Guard"' in js and "inline-python" in js                                          # column + rule legend


def test_old_unparseable_rows_show_the_real_rule(env):
    from codepilot.audit import AuditLog
    from codepilot.dashboard.data import safety
    AuditLog(env.conn).log("agent:coder", "command_refused", run_id=env.rid, target='python -c "import a;print(a)"',
                           result="refused", detail={"reason": "unparseable command"})          # saved by the old guard
    assert safety(env.conn, Path("/nonexistent"))["commands_refused"][0]["guard"] == "Shell guard · inline-python"


def test_success_rate_is_per_issue_on_the_latest_run(tmp_path):
    from codepilot.dashboard.data import issue_outcomes
    from codepilot.db import connect
    conn = connect(tmp_path / "cp.db")
    def run(i, key, state, reason=None):
        conn.execute("insert into runs(id, repo, kind, issue_key, title, state, reason) values(?,?,?,?,?,?,?)",
                     (f"r{i}", "o/r", "issue", key, "t", state, reason))
    run(1, "#1", "FAILED", "setup: box command failed"); run(2, "#1", "DONE")             # solved on the 2nd attempt
    run(3, "#2", "AWAITING_APPROVAL", "publish")                                           # solved first time
    run(4, "#3", "AWAITING_APPROVAL", "continue_fix")                                      # still open: not solved
    run(5, "#4", "FAILED", "no change made — conflicts with a protected test")             # declined
    run(6, "#5", "FAILED", "untested")                                                     # failed
    run(7, "#6", "FAILED", "crashed")                                                      # infrastructure
    o = issue_outcomes(conn)
    assert (o["issues"], o["solved"], o["declined"], o["failed"], o["infrastructure"], o["open"]) == (6, 2, 1, 1, 1, 1)
    assert o["rate"] == round(2 / 5, 2) and o["first_attempt"] == 1                        # 6 issues − 1 infrastructure


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
def test_every_page_renders_without_a_javascript_error(env, tmp_path):
    # live 2026-10-19: Overview showed "st is not defined" — syntax checks and API tests cannot catch runtime errors
    import subprocess
    _write_eval(tmp_path / "results")
    c, pages = env.client, ["overview", "matrix", "prs", "safety", "context", "memory", "skills", "audit", "improvements", "eval"]
    resp = {p: c.get(f"/api/{p}", headers=H).json() for p in pages}
    resp["run"] = c.get(f"/api/runs/{env.rid}", headers=H).json()
    (tmp_path / "r.json").write_text(json.dumps(resp))
    root = Path(__file__).parents[1]
    out = subprocess.run(["node", str(root / "tests" / "js_smoke.js"), str(root / "src/codepilot/dashboard/static/app.js"),
                          str(tmp_path / "r.json")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {}


def test_overview_summary_has_safety_and_learning_panels(env):
    from codepilot.audit import AuditLog
    from codepilot.dashboard.data import overview_summary
    a = AuditLog(env.conn)
    for cmd in ('python -c "import x"', 'python -c "import y"', "python -m pip show x"):
        a.log("agent:coder", "command_refused", run_id=env.rid, target=cmd, result="refused", detail={"reason": "x"})
    class Lessons:
        def all(self, repo): return [{"text": "a"}, {"text": "b"}]
    class Improvements:
        def trend(self, repos): return [{"repo": "o/r", "version": 1, "runs": 3, "failed_commands": 1.5},
                                        {"repo": "o/r", "version": 2, "runs": 2, "failed_commands": 0.5}]
        def view(self, repos): return {"proposals": [{"id": "p1"}]}
    s = overview_summary(env.conn, ["o/r"], Lessons(), Improvements(), Path("/nonexistent"))
    assert s["safety"]["refused"] == 4 and s["safety"]["top_rules"][0] == ["inline-python", 2]   # fixture has 1 git push
    assert s["learning"]["lessons"] == 2 and s["learning"]["waiting"] == 1
    assert s["learning"]["failed_commands"] == {"o/r": {"from": 1.5, "to": 0.5, "versions": [1, 2]}}


def _write_eval(results):
    from codepilot.evals import heuristics, report
    rep = report.build({"pr_labelled": {"pass": 2, "fail": 1, "n/a": 0}}, [{"met": True, "ended": True, "key": "k", "issue": 1,
                       "type": "bug_fix", "expect": "pr", "resolved": True}], [True], [9], [{"behaved": True}], None, {})
    rep |= {"meaning": heuristics.MEANING, "judge": [{"repo": "o/r", "pr": 3, "run_id": "r1", "score": 9,
                                                      "rubric": {"tests": 2}, "merge_ready": True, "cap": ""}],
            "probe_rows": [{"key": "deleted-test", "expect": "reject", "score": 1, "merge_ready": False, "behaved": True,
                            "rubric": {"tests": 0}, "security": [], "why": ["score 1 < 8"], "cap": ""}],
            "golden_rows": [{"key": "k", "issue": 1, "type": "bug_fix", "expect": "pr", "ended": True, "met": True, "resolved": True}],
            "runs": [{"run_id": "r1", "repo": "o/r", "issue": "#1", "state": "DONE", "pr": 3,
                      "checks": [{"name": "pr_labelled", "status": "fail", "detail": "x"}]}]}
    results.mkdir(parents=True, exist_ok=True)
    (results / "eval.json").write_text(json.dumps(rep))


def test_eval_page_reads_the_last_report_or_says_how_to_make_one(env, tmp_path):
    missing = env.client.get("/api/eval", headers=H).json()
    assert missing["missing"] is True and "scripts/evaluate.py" in missing["how"]
    _write_eval(tmp_path / "results")
    d = env.client.get("/api/eval", headers=H).json()
    assert d["verdict"] == "FAIL" and d["probe_rows"][0]["key"] == "deleted-test" and "measured_at" in d
    assert env.client.get("/api/eval", headers={"host": H["host"]}).status_code == 401                      # same token rule as every page


def test_explored_reads_skipped_when_the_orchestrator_went_straight_to_the_coder(tmp_path):
    from codepilot.dashboard.data import _stage_marks
    conn = connect(tmp_path / "cp.db")
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps({"kind": "state", "text": s}) for s in ("IMPLEMENTING", "TESTING")) + "\n")
    run = {"id": "r1", "task_type": "bug_fix", "reason": "", "pr_url": None, "state": "AWAITING_APPROVAL"}
    assert _stage_marks(conn, tmp_path / "runs", run)["Explored"] == "skipped"
    run_dir.joinpath("events.jsonl").write_text(json.dumps({"kind": "state", "text": "TRIAGED"}) + "\n")
    assert _stage_marks(conn, tmp_path / "runs", run)["Explored"] == ""      # never got that far: not "skipped"
