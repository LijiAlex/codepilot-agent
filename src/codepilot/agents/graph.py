# src/codepilot/agents/graph.py
"""Build the per-run agent graph: Orchestrator → Repo Explorer, compiled Coder (→ Test Agent, test-doctor), PR Agent.
Each agent gets an explicit tool list (FilesystemMiddleware(tools=…) replaces the default by name, verified),
an allowlist that refuses anything else, call limits, and retries."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepagents import CompiledSubAgent, FilesystemMiddleware, FilesystemPermission, create_deep_agent
from langchain.agents.middleware import (ModelCallLimitMiddleware, ModelRetryMiddleware, TodoListMiddleware,
                                         ToolCallLimitMiddleware, wrap_tool_call)
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from codepilot.agents import prompts
from codepilot.agents.tools import (AttemptState, make_fetch_changelog_tool, make_keyword_search_tool, make_lint_on_edit,
                                    make_parse_stack_trace_tool, make_parse_test_results_tool, make_read_repo_map_tool,
                                    make_reinstall_tool, safe_tool,
                                    make_run_tests_tool, make_semantic_search_tool, truncate_head_tail)
from codepilot.changes import capture_changes, unified_diff
from codepilot.memory.working import WorkingMemory
from codepilot.safety.allowlist import make_secret_path_guard, make_tool_allowlist
from codepilot.safety.shell_guard import make_shell_guard
from codepilot.sandbox.base import SANDBOX_ROUTE, SECRET_GLOBS, sandbox_permissions
from codepilot.skills import Skill, render_skill_prompt

TEST_PATHS = [f"{SANDBOX_ROUTE}tests/**", f"{SANDBOX_ROUTE}**/test_*.py", f"{SANDBOX_ROUTE}**/*_test.py", f"{SANDBOX_ROUTE}**/conftest.py"]
TEST_CONFIG_PATHS = TEST_PATHS + [f"{SANDBOX_ROUTE}{p}" for p in ("pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml")]

EXPECTED_TOOLS: dict[str, set[str]] = {
    "orchestrator": {"write_todos", "task", "read_claimed_issue", "ls", "read_file", "show_diff"},
    "repo-explorer": {"ls", "read_file", "glob", "grep", "keyword_search", "semantic_search", "read_repo_map"},
    "coder": {"write_todos", "task", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "show_diff",
              "parse_stack_trace", "fetch_changelog", "reinstall_dependencies"},
    "test-agent": {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "run_tests", "parse_test_results"},
    "test-doctor": {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "run_tests"},
    "pr-agent": {"ls", "read_file", "write_file"},
}
FS_TOOLS = {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
EXPECTED_SUBAGENTS = {"orchestrator": {"repo-explorer", "coder", "pr-agent"}, "coder": {"test-agent", "test-doctor"}}


def expected_for(skill: Skill | None) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Tool sets for this run. Classification decides the subagents (brief): a skill that spawns no test agent
    (documentation) gives the Coder run_tests directly and no test-agent / test-doctor."""
    tools = {k: set(v) for k, v in EXPECTED_TOOLS.items()}
    subs = {k: set(v) for k, v in EXPECTED_SUBAGENTS.items()}
    if skill is not None and "test_agent" not in skill.spawns:
        tools.pop("test-agent"); tools.pop("test-doctor")
        tools["coder"] = (tools["coder"] - {"task"}) | {"run_tests"}
        subs.pop("coder")
    return tools, subs


@dataclass
class RunContext:
    run_id: str
    repo: str                                   # owner/name
    task_type: str
    complexity: int
    skill: Skill
    backend: Any                                # SandboxHandle.backend
    shared_dir: Path
    mirror: Path                                # host mirror (old file text for diffs)
    base_sha: str
    test_command: str
    audit: Any
    attempts: AttemptState
    map_text: str
    issue_tool: StructuredTool | None = None
    searcher: Any = None                        # FileSearcher; None → semantic_search answers "not available"
    repo_skill: str = ""
    lessons: list[str] = field(default_factory=list)
    plan_text: str = ""
    approved_commands: frozenset[str] = frozenset()
    max_model_calls: int = 150
    max_tool_calls: int = 400
    reinstall: Any = None                       # () -> (exit_code, output); code reinstalls after a manifest edit
    protected_paths: list[str] = field(default_factory=list)   # repo's protected paths → write-deny for every agent
    top_k: int = 10


def _with_allow(perms: list[FilesystemPermission], paths: list[str]) -> list[FilesystemPermission]:
    """Secret rules stay first (first match wins); then allow writes to `paths`; deny every other write."""
    return [*perms, FilesystemPermission(operations=["write"], paths=paths, mode="allow"),
            FilesystemPermission(operations=["write"], paths=[f"{SANDBOX_ROUTE}**", "/shared/**"], mode="deny")]


def _guards(ctx: RunContext, agent: str, *, shell: bool = False) -> list:
    mw: list = [make_tool_allowlist(expected_for(ctx.skill)[0].get(agent, EXPECTED_TOOLS[agent]), agent, ctx.audit, ctx.run_id),
                make_secret_path_guard(agent, ctx.audit, ctx.run_id)]
    if shell:
        mw.append(make_shell_guard(ctx.audit, ctx.run_id, agent, ctx.approved_commands))
    return mw


def _limits(ctx: RunContext, agent: str) -> list:
    return [ModelCallLimitMiddleware(run_limit=ctx.max_model_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=ctx.max_tool_calls, exit_behavior="error"),
            ModelRetryMiddleware(max_retries=3, on_failure="error")]


def _fs(ctx: RunContext, agent: str, perms: list[FilesystemPermission]) -> FilesystemMiddleware:
    allowed = expected_for(ctx.skill)[0].get(agent, EXPECTED_TOOLS[agent])     # specs built but not mounted use the base set
    return FilesystemMiddleware(backend=ctx.backend, tools=sorted(allowed & FS_TOOLS), _permissions=perms)


def make_show_diff_tool(ctx: RunContext) -> StructuredTool:
    def show_diff() -> str:
        """Show the current change against the starting commit (also saved to /shared/proposed_diff.txt)."""
        diff = unified_diff(ctx.mirror, ctx.base_sha, capture_changes(ctx.backend, ctx.base_sha)) or "(no changes yet)"
        (ctx.shared_dir / "proposed_diff.txt").write_text(diff)
        return truncate_head_tail(diff, 12000)
    return safe_tool(show_diff, "show_diff")


def make_once_guard(subagent: str):
    """test-doctor fixes the test setup once per run (spec §4.2 step 7); a second request is refused."""
    calls = {"n": 0}

    @wrap_tool_call(name=f"once_{subagent}")
    def once(request, handler):
        call = request.tool_call
        if call["name"] == "task" and call["args"].get("subagent_type") == subagent:
            calls["n"] += 1
            if calls["n"] > 1:
                return ToolMessage(content=f"{subagent} already ran once in this run; fix the code or report the setup problem.",
                                   name="task", tool_call_id=call["id"], status="error")
        return handler(request)
    return once


def make_loop_detector(limit: int = 6):
    """More than `limit` edits to one file → tell the agent to step back (spec §4.2 loop detection)."""
    counts: Counter[str] = Counter()

    @wrap_tool_call(name="loop_detector")
    def loop_detector(request, handler):
        result = handler(request)
        call = request.tool_call
        if call["name"] in ("edit_file", "write_file") and isinstance(result, ToolMessage):
            path = str(call["args"].get("file_path", ""))
            counts[path] += 1
            if counts[path] == limit + 1:
                return ToolMessage(content=f"{result.content}\n\nYou have edited {path} {counts[path]} times. Stop and "
                                           f"reconsider the approach: re-read the task and the failing output first.",
                                   name=result.name, tool_call_id=result.tool_call_id, status=result.status)
        return result
    return loop_detector


def _unavailable_semantic() -> StructuredTool:
    def semantic_search(query: str) -> str:
        """Search files by meaning (not available for this repository yet; use keyword_search)."""
        return "semantic search is not available for this repository yet; use keyword_search"
    return StructuredTool.from_function(semantic_search, name="semantic_search")


def build_graph(ctx: RunContext, models: dict[str, Any], checkpointer=None):
    """models: one chat model per agent name (they may be the same object in production)."""
    base_perms = sandbox_permissions(ctx.protected_paths)
    lessons = "\n".join(f"- {l}" for l in ctx.lessons[:3]) or "(none yet)"
    tl = [l for l in ctx.lessons if "test" in l.lower()][:3]                         # Plus: testing lessons for the Test Agent
    testing_lessons = ("Testing lessons from earlier work in this repository:\n" + "\n".join(f"- {l}" for l in tl)) if tl else ""
    task_json = WorkingMemory.VIRTUAL_PATH
    full_note = " — this task type always runs the whole suite" if ctx.skill.full_test_suite else ""
    show_diff = make_show_diff_tool(ctx)
    protected_tests = [p for p in ctx.protected_paths if "test" in p and p.endswith(".py")]      # e.g. tests/test_contract_*.py

    test_agent = {
        "name": "test-agent", "description": "Writes and runs tests for a change; reports parsed results.",
        "system_prompt": prompts.TEST_AGENT.format(untrusted=prompts.UNTRUSTED, testing=render_skill_prompt(ctx.skill, "testing"),
                                                   full_suite_note=full_note, lessons=testing_lessons, task_json=task_json),
        "model": models.get("test-agent", models["coder"]),
        "tools": [make_run_tests_tool(ctx.backend, ctx.test_command, ctx.task_type, ctx.attempts, always_verify=protected_tests),
                  make_parse_test_results_tool()],
        "permissions": _with_allow(base_perms, TEST_PATHS),
        "middleware": [_fs(ctx, "test-agent", _with_allow(base_perms, TEST_PATHS)), *_guards(ctx, "test-agent", shell=True),
                       make_lint_on_edit(ctx.backend), *_limits(ctx, "test-agent")],
    }
    test_doctor = {
        "name": "test-doctor", "description": "Fixes broken test configuration once when tests cannot be collected. Never installs packages.",
        "system_prompt": prompts.TEST_DOCTOR, "model": models.get("test-doctor", models["coder"]),
        "tools": [make_run_tests_tool(ctx.backend, ctx.test_command, ctx.task_type, ctx.attempts)],
        "permissions": _with_allow(base_perms, TEST_CONFIG_PATHS),
        "middleware": [_fs(ctx, "test-doctor", _with_allow(base_perms, TEST_CONFIG_PATHS)), *_guards(ctx, "test-doctor", shell=True),
                       *_limits(ctx, "test-doctor")],
    }
    with_tests = "test_agent" in ctx.skill.spawns
    coder_perms = [*base_perms, FilesystemPermission(operations=["write"], paths=["/shared/**"], mode="deny")]
    coder_tools = [show_diff, make_parse_stack_trace_tool(), make_fetch_changelog_tool(),
                   make_reinstall_tool(ctx.backend, ctx.reinstall, ctx.audit, ctx.run_id)]
    if not with_tests:
        coder_tools.append(make_run_tests_tool(ctx.backend, ctx.test_command, ctx.task_type, ctx.attempts, always_verify=protected_tests))
    coder = create_deep_agent(
        model=models["coder"],
        system_prompt=prompts.CODER.format(untrusted=prompts.UNTRUSTED, skill=render_skill_prompt(ctx.skill),
                                           repo_skill=ctx.repo_skill, lessons=lessons, task_json=task_json,
                                           testing_step=prompts.TESTING_WITH_AGENT if with_tests else prompts.TESTING_SELF),
        tools=coder_tools,
        subagents=[test_agent, test_doctor] if with_tests else [], backend=ctx.backend, permissions=coder_perms,
        middleware=[TodoListMiddleware(), _fs(ctx, "coder", coder_perms), *_guards(ctx, "coder", shell=True),
                    make_lint_on_edit(ctx.backend), make_loop_detector(), make_once_guard("test-doctor"), *_limits(ctx, "coder")],
        name="coder",
    )
    explorer_read_only = [*base_perms, FilesystemPermission(operations=["write"], paths=[f"{SANDBOX_ROUTE}**", "/shared/**"], mode="deny")]
    explorer = {
        "name": "repo-explorer", "description": "Finds the files relevant to a task (top 10) using the repo map and search.",
        "system_prompt": prompts.EXPLORER.format(untrusted=prompts.UNTRUSTED, top_k=ctx.top_k, task_json=task_json,
                                                 skill=render_skill_prompt(ctx.skill, "explore"),
                                                 repo_skill=ctx.repo_skill, lessons=f"Lessons:\n{lessons}" if ctx.lessons else ""),
        "model": models["repo-explorer"],
        "tools": [make_read_repo_map_tool(ctx.map_text), make_keyword_search_tool(ctx.backend, ctx.top_k),
                  make_semantic_search_tool(ctx.searcher, ctx.top_k) if ctx.searcher is not None else _unavailable_semantic()],
        "permissions": explorer_read_only,
        "middleware": [_fs(ctx, "repo-explorer", explorer_read_only), *_guards(ctx, "repo-explorer"), *_limits(ctx, "repo-explorer")],
    }
    pr_perms = _with_allow(base_perms, ["/shared/pr_draft.json"])
    pr_agent = {
        "name": "pr-agent", "description": "Drafts branch name, commit message and PR text into /shared/pr_draft.json.",
        "system_prompt": prompts.PR_AGENT.format(untrusted=prompts.UNTRUSTED, task_json=task_json),
        "model": models["pr-agent"], "tools": [], "permissions": pr_perms,
        "middleware": [_fs(ctx, "pr-agent", pr_perms), *_guards(ctx, "pr-agent"), *_limits(ctx, "pr-agent")],
    }
    orch_perms = [*base_perms, FilesystemPermission(operations=["write"], paths=[f"{SANDBOX_ROUTE}**", "/shared/**"], mode="deny")]
    orch_tools = [show_diff] + ([ctx.issue_tool] if ctx.issue_tool is not None else [_free_form_issue_tool(ctx)])
    return create_deep_agent(
        model=models["orchestrator"],
        system_prompt=prompts.ORCHESTRATOR.format(repo=ctx.repo, untrusted=prompts.UNTRUSTED, task_type=ctx.task_type,
                                                  complexity=ctx.complexity, skill=render_skill_prompt(ctx.skill),
                                                  repo_skill=ctx.repo_skill, plan=ctx.plan_text, task_json=task_json),
        tools=orch_tools,
        subagents=[explorer, CompiledSubAgent(name="coder", description="Changes the code and gets it tested (runs the test agent).",
                                              runnable=coder), pr_agent],
        backend=ctx.backend, permissions=orch_perms,
        middleware=[TodoListMiddleware(), _fs(ctx, "orchestrator", orch_perms), *_guards(ctx, "orchestrator"), *_limits(ctx, "orchestrator")],
        checkpointer=checkpointer, name="orchestrator",
    )


def _free_form_issue_tool(ctx: RunContext) -> StructuredTool:
    """Free-form [i] tasks have no issue: the read tool returns the task text from working memory."""
    def read_claimed_issue() -> str:
        """Read the task this run is working on."""
        issue = WorkingMemory(ctx.shared_dir).read().get("issue", {})
        return f"<untrusted_issue>\nTask: {issue.get('title', '')}\n\n{issue.get('body', '')}\n</untrusted_issue>"
    return StructuredTool.from_function(read_claimed_issue, name="read_claimed_issue")


_AGENT_LINE = re.compile(r"^- ([a-z][a-z0-9-]*):", re.M)


def subagents_in_task_description(description: str) -> set[str]:
    """The task tool lists agents as '- name: description' lines; 'general-purpose' also appears once in usage notes,
    so only line starts count (verified)."""
    return set(_AGENT_LINE.findall(description))


def check_tool_sets(bound: dict[str, dict[str, str]], skill: Skill | None = None) -> list[str]:
    """Compare what each agent's model was actually given with the expected sets for this skill. Empty = OK."""
    problems = []
    tools, subagents = expected_for(skill)
    for agent, expected in tools.items():
        got = set(bound.get(agent, {}))
        if got != expected:
            problems.append(f"{agent}: unexpected {sorted(got - expected)}, missing {sorted(expected - got)}")
    for agent, subs in subagents.items():
        got = subagents_in_task_description(bound.get(agent, {}).get("task", ""))
        if got != subs:
            problems.append(f"{agent}: task tool offers {sorted(got)}, expected {sorted(subs)}")
    return problems
