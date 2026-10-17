# tests/test_worker.py
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from codepilot.agents.models import register_profiles
from codepilot.audit import AuditLog
from codepilot.config import RepoConfig, Settings
from codepilot.db import connect
from codepilot.gh.reader import IssueSnapshot
from codepilot.handoffs import HandoffStore
from codepilot.memory.episodic import EpisodicMemory
from codepilot.repomap.mirror import HostMirror
from codepilot.runs import RunStore
from codepilot.sandbox.base import SandboxHandle
from codepilot.worker import Seed, Services, WorkItem, Worker
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from tests.fakes import FakeClaude, tool_call

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"
TRIAGE = {"task_type": "bug_fix", "complexity": 3, "complexity_reasons": ["small"], "summary": "slugify handles empty titles",
          "identifiers": ["slugify"]}
DRAFT = {"branch_slug": "empty-title", "commit_summary": "handle empty title", "commit_bullets": ["guard"],
         "pr_title": "Handle empty title", "pr_summary": "s", "pr_approach": "a", "risks": "r"}
EMPTY_TEST = "from pkg.slugs import slugify\n\ndef test_empty():\n    assert slugify('') == ''\n"


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class TmpProvider:
    """Clone-per-run provider without OS sandboxing (unit tests); the real ones are tested in Task 8."""
    def __init__(self, home: Path):
        self.home, self.created, self.deleted, self.swept = home, [], [], 0
    def create(self, run_id, owner, name, base_sha, install_command):
        run = self.home / "runs" / run_id; (run / "shared").mkdir(parents=True, exist_ok=True)
        git(self.home, "clone", "-q", str(self.home / "repos" / owner / name / "mirror"), str(run / "sandbox"))
        git(run / "sandbox", "checkout", "-q", "--detach", base_sha)
        b = CompositeBackend(default=LocalShellBackend(root_dir=run, virtual_mode=True, env={"PATH": os.environ["PATH"]}),
                             routes={"/sandbox/": FilesystemBackend(root_dir=run / "sandbox", virtual_mode=True),
                                     "/shared/": FilesystemBackend(root_dir=run / "shared", virtual_mode=True)},
                             artifacts_root="/shared/")
        self.created.append(run_id)
        return SandboxHandle(run_id=run_id, kind="tmp", backend=b, shared_dir=run / "shared", sandbox_id=str(run))
    def delete(self, h):
        shutil.rmtree(self.home / "runs" / h.run_id / "sandbox", ignore_errors=True); self.deleted.append(h.run_id)
    def sweep(self, keep):
        self.swept += 1; return 0


@pytest.fixture
def env(tmp_path):
    origin = tmp_path / "origin"; (origin / "pkg").mkdir(parents=True); (origin / "tests").mkdir()
    (origin / "pkg" / "__init__.py").write_text("")
    (origin / "pkg" / "slugs.py").write_text("def slugify(t):\n    return t.lower()\n")
    (origin / "tests" / "test_slugs.py").write_text("from pkg.slugs import slugify\n\ndef test_basic():\n    assert slugify('A') == 'a'\n")
    git(origin, "init", "-q", "-b", "main"); git(origin, "add", "."); git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i")
    home = tmp_path / "home"
    git(tmp_path, "clone", "-q", str(origin), str(home / "repos" / "o" / "r" / "mirror"))
    conn = connect(home / "codepilot.db")
    settings = Settings(repos=[RepoConfig(owner="o", name="r", test_command=PYTEST)], home=home, sandbox="tmp")
    return {"home": home, "conn": conn, "settings": settings, "repo": settings.repos[0], "provider": TmpProvider(home)}


def services(env, scripts, triage_msgs=None, **overrides):
    def make_models():
        return {k: FakeClaude(tag=k, responses=list(v)) for k, v in scripts.items()}
    tri = triage_msgs or [tool_call("TriageResult", TRIAGE, "t")]
    s = Services(settings=overrides.pop("settings", env["settings"]), runs=RunStore(env["conn"]), handoffs=HandoffStore(env["conn"]),
                 audit=AuditLog(env["conn"]), mirror=HostMirror(env["home"]), providers={"tmp": env["provider"]},
                 make_models=make_models, triage_model=lambda: FakeClaude(responses=list(tri)),
                 episodic=EpisodicMemory(env["home"] / "episodic.db"), **overrides)
    register_profiles(FakeClaude(responses=[]))
    return s


def item(env, n=7):
    return WorkItem(repo=env["repo"], issue=IssueSnapshot(n, "slugify('') crashes", "Empty title raises.", "alice"))


def base_scripts(**kw):
    s = {
        "orchestrator": [tool_call("task", {"description": "find", "subagent_type": "repo-explorer"}, "o1"),
                         tool_call("task", {"description": "fix", "subagent_type": "coder"}, "o2"),
                         tool_call("task", {"description": "draft", "subagent_type": "pr-agent"}, "o3"),
                         AIMessage(content="Fixed and tested.")],
        "repo-explorer": [AIMessage(content="/sandbox/pkg/slugs.py\n/sandbox/tests/test_slugs.py")],
        "coder": [tool_call("edit_file", {"file_path": "/sandbox/pkg/slugs.py", "old_string": "return t.lower()",
                                          "new_string": "return (t or '').lower()"}, "c1"),
                  tool_call("task", {"description": "test", "subagent_type": "test-agent"}, "c2"), AIMessage(content="done")],
        "test-agent": [tool_call("write_file", {"file_path": "/sandbox/tests/test_empty.py", "content": EMPTY_TEST}, "t1"),
                       tool_call("run_tests", {"purpose": "verify"}, "t2"), AIMessage(content="2 passed")],
        "test-doctor": [AIMessage(content="ok")],
        "pr-agent": [tool_call("write_file", {"file_path": "/shared/pr_draft.json", "content": json.dumps(DRAFT)}, "p1"),
                     AIMessage(content="draft written")],
    }
    s.update(kw)
    return s


def claim(env, it):
    return RunStore(env["conn"]).claim(it.repo.full_name, "issue", it.issue.number, it.title)


def test_happy_path_ends_in_publish_handoff_and_deletes_sandbox(env):
    events = []
    s = services(env, base_scripts(), on_event=events.append)
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid)
    assert out.state == "AWAITING_APPROVAL" and out.reason == "publish", out
    p = s.handoffs.payload(out.handoff_id)
    assert {f["path"] for f in p["saved"]} == {"pkg/slugs.py", "tests/test_empty.py"}
    assert p["draft"]["branch_slug"] == "empty-title" and p["test_report"]["outcome"] == "passed"
    assert "+    return (t or '').lower()" in p["diff"] and p["issue"]["number"] == 7 and p["flags"] == []
    assert env["provider"].deleted == [rid]
    states = [e.text for e in events if e.kind == "state"]
    assert states[:3] == ["EXPLORING", "IMPLEMENTING", "TESTING"] and states[-1].startswith("AWAITING_APPROVAL")
    assert (env["home"] / "runs" / rid / "events.jsonl").exists()
    ctxev = [e for e in events if e.kind == "notice" and e.data.get("kind") == "context"]
    assert ctxev and ctxev[0].data["map_tokens"] > 0 and ctxev[0].data["skill"] == "bug_fix_skill"
    assert s.runs.get(rid)["task_type"] == "bug_fix"
    # working memory was kept current during the run, then left for the waiting handoff (cleared only at DONE/FAILED)
    import json as _json
    wm = _json.loads((env["home"] / "runs" / rid / "shared" / "task.json").read_text())
    assert wm["relevant_files"] == ["pkg/slugs.py", "tests/test_slugs.py"] and wm["test_results"]["outcome"] == "passed"
    assert wm["retry_count"] == 0
    ep = s.episodic.episodes("o/r")[0]          # waiting-for-approval runs are episodes too
    assert ep["outcome"] == "AWAITING_APPROVAL" and ep["task_type"] == "bug_fix" and set(ep["files"]) == {"pkg/slugs.py", "tests/test_empty.py"}


def test_file_index_built_at_the_map_sha_and_given_to_the_explorer(env, tmp_path):
    from codepilot.memory.semantic import FileIndex
    from tests.test_memory import hash_embed
    idx = FileIndex(tmp_path / "chroma", "o/r", hash_embed)
    s = services(env, base_scripts(), file_index=lambda repo: idx)
    it = item(env); rid = claim(env, it)
    assert Worker(s).run(it, rid).reason == "publish"
    assert idx.sha == s.runs.get(rid)["base_sha"] and "pkg/slugs.py" in [p for p, _ in idx.search("slugify")]


def test_complex_issue_proposes_plan_without_a_sandbox(env):
    plan = {"score": 1, "reasons": ["many files"], "steps": ["a", "b"], "likely_files": ["pkg/slugs.py"], "risks": ["r"],
            "est_cost_usd": 3.0, "est_minutes": 30}
    tri = [tool_call("TriageResult", {**TRIAGE, "complexity": 9}, "t"), tool_call("ComplexPlan", plan, "p")]
    s = services(env, base_scripts(), triage_msgs=tri)
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid)
    assert (out.state, out.reason) == ("AWAITING_APPROVAL", "complex") and env["provider"].created == []
    p = s.handoffs.payload(out.handoff_id)
    assert p["plan"]["score"] == 9 and p["triage"]["complexity"] == 9


FAILING_FIX = [tool_call("edit_file", {"file_path": "/sandbox/pkg/slugs.py", "old_string": "return t.lower()",
                                       "new_string": "return t.upper()"}, "c1"),
               tool_call("task", {"description": "test", "subagent_type": "test-agent"}, "c2"), AIMessage(content="x")]


def test_two_failed_test_runs_hand_off_continue_fix_with_the_saved_change(env):
    ta = [tool_call("run_tests", {"purpose": "verify"}, "t1"), tool_call("run_tests", {"purpose": "verify"}, "t2"), AIMessage(content="x")]
    s = services(env, base_scripts(coder=FAILING_FIX, **{"test-agent": ta}))
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid)
    assert (out.state, out.reason) == ("AWAITING_APPROVAL", "continue_fix")
    p = s.handoffs.payload(out.handoff_id)
    assert p["prior_failures"] == 2 and p["saved"][0]["path"] == "pkg/slugs.py" and p["gate"]["gate"] == "continue_fix"
    assert env["provider"].deleted == [rid]


def test_third_attempt_fails_the_run(env):
    ta = [tool_call("run_tests", {"purpose": "verify"}, "t1"), AIMessage(content="x")]
    s = services(env, base_scripts(coder=FAILING_FIX, **{"test-agent": ta}))
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid, Seed(prior_failures=2, triage=TRIAGE))
    assert (out.state, out.reason) == ("FAILED", "tests")


def test_unexpected_command_hands_off_continue_with_decision(env):
    coder = [tool_call("execute", {"command": "docker build ."}, "c1"), AIMessage(content="x")]
    orch = [tool_call("write_todos", {"todos": [{"content": "build image", "status": "in_progress"}]}, "o0"),
            tool_call("task", {"description": "fix", "subagent_type": "coder"}, "o2"), AIMessage(content="x")]
    s = services(env, base_scripts(coder=coder, orchestrator=orch))
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid)
    assert (out.state, out.reason) == ("AWAITING_APPROVAL", "continue_with_decision")
    assert s.handoffs.payload(out.handoff_id)["gate"]["command"] == "docker build ."
    assert s.handoffs.payload(out.handoff_id)["todos"] == [{"content": "build image", "status": "in_progress"}]


def test_protected_file_cannot_be_written_and_never_reaches_the_change(env):
    coder = [tool_call("write_file", {"file_path": "/sandbox/uv.lock", "content": "x"}, "c1"),
             tool_call("task", {"description": "test", "subagent_type": "test-agent"}, "c2"), AIMessage(content="x")]
    events = []
    s = services(env, base_scripts(coder=coder), on_event=events.append)
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid)
    denied = [e for e in events if e.kind == "tool_result" and "permission denied for write on /sandbox/uv.lock" in e.text]
    assert denied
    if out.handoff_id:
        assert "uv.lock" not in [f["path"] for f in s.handoffs.payload(out.handoff_id)["saved"]]


def test_protected_file_written_by_shell_still_fails_file_check(env):
    from codepilot.safety.file_check import FileChange, check_changes
    r = check_changes([FileChange("uv.lock", "M", "100644", "a", "b")], set(), env["repo"].protected_paths, "bug_fix")
    assert not r.ok and "protected path changed: uv.lock" in r.problems


def test_change_without_passing_tests_is_not_offered(env):
    coder = [tool_call("edit_file", {"file_path": "/sandbox/pkg/slugs.py", "old_string": "return t.lower()",
                                     "new_string": "return (t or '').lower()"}, "c1"), AIMessage(content="x")]
    s = services(env, base_scripts(coder=coder))
    it = item(env); rid = claim(env, it)
    assert Worker(s).run(it, rid).reason == "untested"
    assert not (env["home"] / "runs" / rid / "shared" / "task.json").exists()          # cleared at FAILED
    ep = s.episodic.episodes("o/r")[0]
    assert ep["task_type"] == "bug_fix" and ep["files"] == ["pkg/slugs.py"] and ep["duration_s"] is not None


def test_step_limit(env):
    settings = env["settings"].model_copy(update={"max_model_calls_per_run": 2})
    s = services(env, base_scripts(), settings=settings)
    it = item(env); rid = claim(env, it)
    assert Worker(s).run(it, rid).reason == "step_limit"


def test_seed_reapplies_saved_change_and_runs_approved_command(env):
    s = services(env, base_scripts(coder=[tool_call("task", {"description": "test", "subagent_type": "test-agent"}, "c2"),
                                          AIMessage(content="done")]))
    it = item(env); rid = claim(env, it)
    saved = [{"path": "pkg/slugs.py", "status": "M", "mode": "100644",
              "content_b64": __import__("base64").b64encode(b"def slugify(t):\n    return (t or '').lower()\n").decode()}]
    out = Worker(s).run(it, rid, Seed(triage=TRIAGE, saved=saved, approved_command="echo approved-ok", prior_failures=0))
    assert out.reason == "publish", out
    ran = s.audit.conn.execute("select target, result from events where action='command_run_after_approval'").fetchone()
    assert ran["target"] == "echo approved-ok" and ran["result"] == "0"


def test_unassigned_issue_above_threshold_is_skipped_not_planned(env):
    s = services(env, base_scripts(), triage_msgs=[tool_call("TriageResult", {**TRIAGE, "complexity": 9}, "t")])
    it = item(env); it.below_threshold_only = True
    rid = claim(env, it)
    out = Worker(s).run(it, rid)
    assert (out.state, out.reason) == ("SKIPPED", "above_threshold") and env["provider"].created == []


def test_cancel_stops_an_in_flight_run(env):
    s = services(env, base_scripts())
    it = item(env); rid = claim(env, it)
    w = Worker(s)
    w.cancel(rid)
    out = w.run(it, rid)
    assert (out.state, out.reason) == ("SKIPPED", "by_user") and env["provider"].deleted == [rid]


def test_recover_marks_in_flight_runs_crashed_and_sweeps(env):
    s = services(env, base_scripts())
    rid = s.runs.claim("o/r", "issue", 9, "t")
    assert Worker(s).recover() == [rid] and s.runs.get(rid)["reason"] == "crashed" and env["provider"].swept == 1


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="local fallback uses macOS sandbox-exec")
def test_happy_path_on_the_real_local_sandbox(env):
    from codepilot.sandbox.local import LocalSandboxProvider
    settings = env["settings"].model_copy(update={"sandbox": "local"})
    s = services(env, base_scripts(), settings=settings)
    s.providers["local"] = LocalSandboxProvider(env["home"], env["home"] / "repos")
    it = item(env); rid = claim(env, it)
    out = Worker(s).run(it, rid)
    assert out.reason == "publish", out
    assert not (env["home"] / "runs" / rid / "sandbox").exists()


def test_recover_survives_a_provider_that_cannot_sweep(env):
    s = services(env, base_scripts())
    class NoKey:
        def sweep(self, keep): raise KeyError("DAYTONA_API_KEY")
    s.providers["daytona"] = NoKey()
    assert Worker(s).recover() == [] and env["provider"].swept == 1
