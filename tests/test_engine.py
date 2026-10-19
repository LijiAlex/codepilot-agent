# tests/test_engine.py
import time
from types import SimpleNamespace as NS

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from codepilot.continuations import ContinuationServices
from codepilot.engine import Engine, Job, UiCallbacks
from codepilot.publish import PublishServices
from codepilot.states import RunState
from codepilot.worker import Outcome, Worker
from tests.fakes import FakeClaude
from tests.test_publish import PubRepo
from tests.test_worker import base_scripts, claim, env, item, services  # noqa: F401  (env is a fixture)


class NoPoll:
    def poll(self): return NS(claimed=[], skipped={}, expired=[])


class NoWatch:
    def __init__(self): self.checks = 0
    def check(self): self.checks += 1


def engine(env, scripts=None, selfcheck=lambda: [], **svc):
    ui = NS(events=[], notices=[], changes=0)
    s = services(env, scripts or base_scripts(), **svc)
    repo = PubRepo()
    ps = PublishServices(runs=s.runs, handoffs=s.handoffs, audit=s.audit, repo_factory=lambda n: repo,
                         make_model=lambda: FakeClaude(responses=[AIMessage(content="opened")]))
    w = Worker(s)
    cs = ContinuationServices(publish=ps, repo_config=lambda n: env["repo"], cancel_run=w.cancel)
    cb = UiCallbacks(on_event=ui.events.append, on_notice=ui.notices.append,
                     on_change=lambda: setattr(ui, "changes", ui.changes + 1))
    e = Engine(env["settings"], w, NoPoll(), NoWatch(), cs, s.episodic, cb, selfcheck)
    return e, ui


def test_startup_refuses_on_tool_drift_and_recovers_crashes(env):
    e, ui = engine(env, selfcheck=lambda: ["coder: unexpected ['delete']"])
    with pytest.raises(RuntimeError, match="tool sets"):
        e.startup()
    e2, ui2 = engine(env)
    e2.worker.s.runs.claim("o/r", "issue", 9, "t")
    assert len(e2.startup()) == 1 and "FAILED(crashed)" in ui2.notices[0] and e2.watcher.checks == 1


def test_free_form_task_runs_on_the_worker_thread_and_session_is_saved(env):
    e, ui = engine(env)
    e.start()
    rid = e.submit_task("o/r", "Make slugify handle empty titles", "slugify('') should return ''")
    assert e.wait_idle(60)
    e.shutdown()
    row = e.worker.s.runs.get(rid)
    assert row["state"] == "AWAITING_APPROVAL" and row["kind"] == "task" and row["issue_key"].startswith("task:")
    assert any(ev.kind == "tool_call" for ev in ui.events)
    last = e.episodic.last_sessions(1)[0]
    assert "AWAITING_APPROVAL(publish): 1" in last["summary"] and last["stats"]["counts"] == {"AWAITING_APPROVAL(publish)": 1}


def test_continue_fix_approval_queues_a_continuation(env):
    e, ui = engine(env)
    it = item(env); rid = claim(env, it)
    s = e.worker.s
    for st in (RunState.EXPLORING, RunState.IMPLEMENTING, RunState.TESTING, RunState.AWAITING_APPROVAL):
        s.runs.set_state(rid, st)
    from tests.test_publish import payload
    hid = s.handoffs.create(rid, "o/r", "#7", "continue_fix", {**payload(), "prior_failures": 2})
    res = e.handle_decision(hid, "approve", "liji")
    job = e.jobs.get_nowait()
    assert res.status == "continued" and job.seed.prior_failures == 2 and job.run_id == res.continuation.run_id


def test_lost_remote_sandbox_is_retried_once_locally(env):
    e, ui = engine(env)
    it = item(env); rid = claim(env, it)
    calls = []

    def fake_run(item_, run_id, seed=None, kind=None):
        calls.append(kind)
        e.worker.s.runs.set_state(run_id, RunState.FAILED, "sandbox_lost")
        return Outcome(run_id, "FAILED", "sandbox_lost", reclaim_local=True)
    e.worker.run = fake_run
    e.run_job(Job(it, rid))
    retry = e.jobs.get_nowait()
    assert retry.sandbox_kind == "local" and "retrying on the local sandbox" in ui.notices[-1]
    e.run_job(retry)
    assert e.jobs.empty() and calls == [None, "local"]


def test_view_lists_runs_pending_and_skips(env):
    e, ui = engine(env)
    it = item(env); rid = claim(env, it)
    e.last_poll = {"o/r#3": "blocked by the injection check"}
    v = e.view()
    assert v["runs"][0]["id"] == rid and v["pending"] == [] and v["current"] is None
    assert v["skipped"] == {"o/r#3": "blocked by the injection check"} and v["repos"] == ["o/r"]


def test_view_pending_carries_gate_flags_not_payloads(env):
    e, ui = engine(env)
    it = item(env); rid = claim(env, it)
    s = e.worker.s
    s.runs.set_state(rid, RunState.AWAITING_APPROVAL)
    s.handoffs.create(rid, "o/r", "#7", "publish", {"flags": ["more_than_5_files"], "files": [f"f{i}.py" for i in range(7)], "saved": ["big"]})
    p = e.view()["pending"][0]
    assert p["flags"] == ["more_than_5_files"] and p["file_count"] == 7 and "payload_json" not in p


def test_startup_session_note_is_one_short_line_without_empty_sessions():
    from codepilot.engine import session_note
    note = session_note([{"summary": "no runs"}, {"summary": "FAILED(setup: box command failed (1): cd …\nObtaining file:///x\n  noise): 1"},
                         {"summary": "PR_OPENED: 2"}])
    assert "\n" not in note and "no runs" not in note and "PR_OPENED: 2" in note and len(note) <= 200


def test_headless_once_does_not_start_a_second_poller(tmp_path, monkeypatch):
    import codepilot.cli as cli
    started = {}
    class FakeEngine:
        def startup(self): pass
        def start(self, poll_loop=True): started["poll_loop"] = poll_loop
        def poll_once(self): started["polled"] = True
        def wait_idle(self, t): return True
        def shutdown(self): pass
        def view(self): return {"runs": []}
    monkeypatch.setattr("codepilot.bootstrap.build_engine", lambda *a, **k: FakeEngine())
    cfg = tmp_path / "codepilot.yml"; cfg.write_text(f"repos:\n  - owner: o\n    name: r\nhome: {tmp_path / 'h'}\n")
    assert cli.main(["--config", str(cfg), "once"]) == 0
    assert started == {"poll_loop": False, "polled": True}            # exactly one poll, on the main thread


def test_run_log_returns_the_last_lines_of_a_finished_run(env):
    e, ui = engine(env)
    run_dir = Path(env["settings"].home) / "runs" / "abc123"
    run_dir.mkdir(parents=True)
    with (run_dir / "events.jsonl").open("w") as f:
        for i in range(250):
            f.write(json.dumps({"run_id": "abc123", "kind": "message", "agent": "coder", "text": f"line {i}", "data": {}}) + "\n")
        f.write("not json\n")                                          # a torn last line must not break it
    log = e.run_log("abc123", limit=200)
    assert len(log) == 200 and log[-1]["text"] == "line 249" and log[0]["text"] == "line 50"
    assert e.run_log("nope") == []


def test_view_says_when_it_last_polled_and_the_map_budget(env):
    e, ui = engine(env)
    assert e.view()["polled_at"] is None                         # not polled yet: the TUI keeps its plain title
    e.poller.poll = lambda: type("R", (), {"skipped": {}, "claimed": []})()
    e.watcher.check = lambda: None
    e.poll_once()
    v = e.view()
    assert v["polled_at"] is not None and v["poll_interval_s"] == e.settings.poll_interval_s
    assert v["map_budget"] == e.settings.repo_map_budget_tokens
