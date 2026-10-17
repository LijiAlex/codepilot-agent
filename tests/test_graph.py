# tests/test_graph.py
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from codepilot.agents.graph import EXPECTED_TOOLS, RunContext, build_graph, check_tool_sets, subagents_in_task_description
from codepilot.agents.models import register_profiles
from codepilot.agents.tools import AttemptState
from codepilot.audit import AuditLog
from codepilot.db import connect
from codepilot.skills import load_skills
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from tests.fakes import BOUND_TOOLS, FakeClaude, tool_call

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def ctx(tmp_path):
    mirror = tmp_path / "mirror"; (mirror / "pkg").mkdir(parents=True); (mirror / "tests").mkdir()
    (mirror / "pkg" / "slugs.py").write_text("def slugify(t):\n    return t.lower()\n")
    (mirror / "tests" / "test_slugs.py").write_text("from pkg.slugs import slugify\n\ndef test_basic():\n    assert slugify('A') == 'a'\n")
    git(mirror, "init", "-q", "-b", "main"); git(mirror, "add", "."); git(mirror, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i")
    sha = git(mirror, "rev-parse", "HEAD").strip()
    run = tmp_path / "run"; run.mkdir(); (run / "shared").mkdir()
    git(tmp_path, "clone", "-q", str(mirror), str(run / "sandbox"))
    backend = CompositeBackend(default=LocalShellBackend(root_dir=run, virtual_mode=True, env={"PATH": os.environ["PATH"]}),
                               routes={"/sandbox/": FilesystemBackend(root_dir=run / "sandbox", virtual_mode=True),
                                       "/shared/": FilesystemBackend(root_dir=run / "shared", virtual_mode=True)},
                               artifacts_root="/shared/")
    audit = AuditLog(connect(tmp_path / "cp.db"))
    return RunContext(run_id="r1", repo="o/r", task_type="bug_fix", complexity=3, skill=load_skills()["bug_fix"],
                      backend=backend, shared_dir=run / "shared", mirror=mirror, base_sha=sha, test_command=PYTEST,
                      audit=audit, attempts=AttemptState(), map_text="pkg/slugs.py: slugify", lessons=["slug bugs: test empty input"])


def models(scripts: dict[str, list]):
    return {name: FakeClaude(tag=name, responses=msgs) for name, msgs in scripts.items()}


DRAFT = {"branch_slug": "empty-title", "commit_summary": "handle empty title", "commit_bullets": ["guard empty input"],
         "pr_title": "Handle empty title", "pr_summary": "s", "pr_approach": "a", "risks": "r"}

HAPPY = {
    "orchestrator": [tool_call("read_claimed_issue", {}, "o0"),
                     tool_call("write_todos", {"todos": [{"content": "fix", "status": "in_progress"}]}, "o1"),
                     tool_call("task", {"description": "find files for slugify", "subagent_type": "repo-explorer"}, "o2"),
                     tool_call("task", {"description": "fix slugify for empty titles", "subagent_type": "coder"}, "o3"),
                     tool_call("show_diff", {}, "o4"),
                     tool_call("write_file", {"file_path": "/sandbox/pkg/x.py", "content": "x"}, "o5"),
                     tool_call("task", {"description": "draft the PR", "subagent_type": "pr-agent"}, "o6"),
                     AIMessage(content="Fixed slugify; tests pass.")],
    "repo-explorer": [tool_call("read_repo_map", {}, "e1"), tool_call("keyword_search", {"query": "slugify"}, "e2"),
                      tool_call("write_file", {"file_path": "/sandbox/pkg/evil.py", "content": "x"}, "e3"),
                      AIMessage(content="/sandbox/pkg/slugs.py")],
    "coder": [tool_call("edit_file", {"file_path": "/sandbox/pkg/slugs.py", "old_string": "return t.lower()",
                                      "new_string": "return (t or '').lower()"}, "c1"),
              tool_call("execute", {"command": "git push origin main"}, "c2"),
              tool_call("task", {"description": "test empty title", "subagent_type": "test-agent"}, "c3"),
              tool_call("task", {"description": "check config", "subagent_type": "test-doctor"}, "c4"),
              AIMessage(content="done; tests pass")],
    "test-agent": [tool_call("write_file", {"file_path": "/sandbox/tests/test_empty.py",
                                            "content": "from pkg.slugs import slugify\n\ndef test_empty():\n    assert slugify('') == ''\n"}, "t1"),
                   tool_call("write_file", {"file_path": "/sandbox/pkg/slugs.py", "content": "hacked"}, "t2"),
                   tool_call("run_tests", {"purpose": "verify"}, "t3"), AIMessage(content="2 passed")],
    "test-doctor": [AIMessage(content="config fine")],
    "pr-agent": [tool_call("write_file", {"file_path": "/shared/pr_draft.json", "content": json.dumps(DRAFT)}, "p1"),
                 AIMessage(content="draft written")],
}


def _issue_tool():
    from codepilot.gh.reader import IssueSnapshot, make_read_claimed_issue_tool
    return make_read_claimed_issue_tool(IssueSnapshot(7, "slugify('') crashes", "steps", "alice"))


def test_full_tree_runs_with_exact_tool_sets_and_permissions(ctx):
    ctx.issue_tool = _issue_tool()
    m = models(HAPPY)
    register_profiles(m["orchestrator"])
    BOUND_TOOLS.clear()
    graph = build_graph(ctx, m, InMemorySaver())
    out = graph.invoke({"messages": [{"role": "user", "content": "work on issue #7"}]}, {"configurable": {"thread_id": "r1"}})
    assert check_tool_sets(BOUND_TOOLS) == [], check_tool_sets(BOUND_TOOLS)
    assert "general-purpose" not in subagents_in_task_description(BOUND_TOOLS["orchestrator"]["task"])
    sandbox = ctx.shared_dir.parent / "sandbox"
    assert (sandbox / "pkg" / "slugs.py").read_text() == "def slugify(t):\n    return (t or '').lower()\n"   # test agent's overwrite denied
    assert (sandbox / "tests" / "test_empty.py").exists()
    assert not (sandbox / "pkg" / "evil.py").exists() and not (sandbox / "pkg" / "x.py").exists()
    assert json.loads((ctx.shared_dir / "pr_draft.json").read_text())["branch_slug"] == "empty-title"
    assert "(t or '')" in (ctx.shared_dir / "proposed_diff.txt").read_text()
    assert ctx.attempts.last_report.outcome == "passed" and ctx.attempts.last_report.passed == 2
    refused = ctx.audit.conn.execute("select target from events where action='command_refused'").fetchall()
    assert [r[0] for r in refused] == ["git push origin main"]
    assert out["messages"][-1].content == "Fixed slugify; tests pass."


def test_unexpected_command_in_coder_surfaces_as_interrupt(ctx):
    ctx.issue_tool = _issue_tool()
    scripts = {k: [AIMessage(content="ok")] for k in EXPECTED_TOOLS}
    scripts["orchestrator"] = [tool_call("task", {"description": "fix", "subagent_type": "coder"}, "o1"), AIMessage(content="x")]
    scripts["coder"] = [tool_call("execute", {"command": "docker build ."}, "c1"), AIMessage(content="x")]
    graph = build_graph(ctx, models(scripts), InMemorySaver())
    seen = []
    for ns, mode, data in graph.stream({"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "r1"}},
                                       stream_mode=["updates"], subgraphs=True):
        if isinstance(data, dict) and "__interrupt__" in data:
            seen += [i.value for i in data["__interrupt__"]]
    assert seen and seen[0]["gate"] == "unexpected_command" and seen[0]["agent"] == "coder"


def test_check_tool_sets_reports_drift():
    bound = {a: {t: "" for t in tools} for a, tools in EXPECTED_TOOLS.items()}
    bound["orchestrator"]["task"] = "Available agents:\n- repo-explorer: x\n- coder: y\n- pr-agent: z\n- general-purpose: g"
    bound["coder"]["task"] = "- test-agent: a\n- test-doctor: b"
    bound["coder"]["delete"] = ""
    problems = check_tool_sets(bound)
    assert any("coder: unexpected ['delete']" in p for p in problems) and any("general-purpose" in p for p in problems)


def test_startup_selfcheck_passes():
    from codepilot.agents.selfcheck import run_selfcheck
    assert run_selfcheck() == []


def test_test_doctor_runs_once_per_run(ctx):
    ctx.issue_tool = _issue_tool()
    scripts = {k: [AIMessage(content="ok")] for k in EXPECTED_TOOLS}
    scripts["orchestrator"] = [tool_call("task", {"description": "fix", "subagent_type": "coder"}, "o1"), AIMessage(content="x")]
    scripts["coder"] = [tool_call("task", {"description": "fix setup", "subagent_type": "test-doctor"}, "c1"),
                        tool_call("task", {"description": "again", "subagent_type": "test-doctor"}, "c2"), AIMessage(content="x")]
    scripts["test-doctor"] = [AIMessage(content="fixed pytest.ini"), AIMessage(content="should not run")]
    from tests.fakes import FakeClaude
    m = {k: FakeClaude(tag=k, responses=v) for k, v in scripts.items()}
    events = []
    g = build_graph(ctx, m, InMemorySaver())
    for ns, mode, data in g.stream({"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "r1"}},
                                   stream_mode=["updates"], subgraphs=True):
        if isinstance(data, dict) and "tools" in data:
            events += [msg.content for msg in data["tools"].get("messages", []) if getattr(msg, "name", "") == "task"]
    assert any("fixed pytest.ini" in str(e) for e in events) and any("already ran once" in str(e) for e in events)


def test_protected_paths_are_write_denied_during_the_run(ctx):
    ctx.issue_tool = _issue_tool()
    ctx.protected_paths = ["tests/test_contract_*.py"]
    contract = ctx.shared_dir.parent / "sandbox" / "tests" / "test_contract_slugs.py"
    contract.write_text("def test_contract():\n    assert True\n")
    scripts = {k: [AIMessage(content="ok")] for k in EXPECTED_TOOLS}
    scripts["orchestrator"] = [tool_call("task", {"description": "fix", "subagent_type": "coder"}, "o1"), AIMessage(content="x")]
    scripts["coder"] = [tool_call("write_file", {"file_path": "/sandbox/tests/test_contract_slugs.py", "content": "# weakened"}, "c1"),
                        tool_call("edit_file", {"file_path": "/sandbox/tests/test_contract_slugs.py", "old_string": "assert True",
                                                "new_string": "pass"}, "c2"), AIMessage(content="x")]
    build_graph(ctx, models(scripts), InMemorySaver()).invoke({"messages": [{"role": "user", "content": "go"}]},
                                                              {"configurable": {"thread_id": "r1"}})
    assert contract.read_text() == "def test_contract():\n    assert True\n"


def test_documentation_skill_spawns_no_test_agent_and_coder_verifies_itself(ctx):
    from codepilot.agents.graph import expected_for
    ctx.issue_tool = _issue_tool()
    ctx.skill = load_skills()["documentation"]; ctx.task_type = "documentation"
    scripts = {"orchestrator": [tool_call("task", {"description": "x", "subagent_type": s}, f"o{i}") for i, s in
                                enumerate(("repo-explorer", "coder", "pr-agent"))] + [AIMessage(content="ok")],
               "repo-explorer": [AIMessage(content="/sandbox/README.md")],
               "coder": [tool_call("run_tests", {"purpose": "verify"}, "c1"), AIMessage(content="ok")],
               "pr-agent": [AIMessage(content="ok")]}
    BOUND_TOOLS.clear()
    build_graph(ctx, models(scripts), InMemorySaver()).invoke({"messages": [{"role": "user", "content": "go"}]},
                                                              {"configurable": {"thread_id": "d"}})
    assert check_tool_sets(BOUND_TOOLS, ctx.skill) == [], check_tool_sets(BOUND_TOOLS, ctx.skill)
    assert "run_tests" in BOUND_TOOLS["coder"] and "task" not in BOUND_TOOLS["coder"] and ctx.attempts.last_report.passed == 1
    assert "test-agent" not in expected_for(ctx.skill)[0]


def test_dot_folder_secrets_are_refused_and_hidden_from_listings(ctx):
    ctx.issue_tool = _issue_tool()
    sb = ctx.shared_dir.parent / "sandbox"
    (sb / ".aws").mkdir(); (sb / ".aws" / "credentials").write_text("AKIA-SECRET")
    (sb / "x" / ".secrets").mkdir(parents=True); (sb / "x" / ".secrets" / "id.key").write_text("KEY-LINE-SECRET\n")
    scripts = {k: [AIMessage(content="ok")] for k in EXPECTED_TOOLS}
    scripts["orchestrator"] = [tool_call("task", {"description": "find", "subagent_type": "repo-explorer"}, "o1"), AIMessage(content="x")]
    scripts["repo-explorer"] = [tool_call("read_file", {"file_path": "/sandbox/.aws/credentials"}, "e1"),
                                tool_call("read_file", {"file_path": "/sandbox/x/.secrets/id.key"}, "e2"),
                                tool_call("glob", {"pattern": "**/*", "path": "/sandbox/"}, "e3"),
                                tool_call("grep", {"pattern": "KEY", "path": "/sandbox/", "output_mode": "content"}, "e4"),
                                AIMessage(content="x")]
    tool_out = []
    for ns, mode, data in build_graph(ctx, models(scripts), InMemorySaver()).stream(
            {"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "s"}}, stream_mode=["updates"], subgraphs=True):
        if isinstance(data, dict) and "tools" in data:
            tool_out += [str(m.content) for m in data["tools"].get("messages", [])]
    joined = "\n".join(tool_out)
    assert "AKIA-SECRET" not in joined and "KEY-LINE-SECRET" not in joined and joined.count("looks like a secret file") == 2
    assert "credentials" not in joined.split("looks like a secret file")[-1] and "pkg/slugs.py" in joined
