# src/codepilot/agents/selfcheck.py
"""Startup check: build the real graph with recording models (no API calls), let every agent run once,
and compare the tools each one was actually given with EXPECTED_TOOLS. CodePilot+ refuses to start on drift
(e.g. a library upgrade re-enabling the general-purpose subagent)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from codepilot.agents.graph import EXPECTED_TOOLS, RunContext, build_graph, check_tool_sets
from codepilot.agents.models import register_profiles
from codepilot.agents.tools import AttemptState
from codepilot.skills import load_skills


_BOUND: dict[str, dict[str, str]] = {}          # module-level: pydantic would copy a dict field


class _Recorder(FakeMessagesListChatModel):
    tag: str = ""

    def _get_ls_params(self, stop: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "anthropic", "ls_model_name": "selfcheck", "ls_model_type": "chat"}

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_Recorder":
        _BOUND[self.tag] = {getattr(t, "name", None) or t.get("name"): getattr(t, "description", "") or t.get("description", "")
                               for t in tools}
        return self


def _call(name: str, args: dict, cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])


class _NullAudit:
    def log(self, *a, **k):
        pass


def run_selfcheck() -> list[str]:
    _BOUND.clear()
    scripts = {a: [AIMessage(content="ok")] for a in EXPECTED_TOOLS}
    scripts["orchestrator"] = [_call("task", {"description": "x", "subagent_type": s}, f"o{i}")
                               for i, s in enumerate(("repo-explorer", "coder", "pr-agent"))] + [AIMessage(content="ok")]
    scripts["coder"] = [_call("task", {"description": "x", "subagent_type": s}, f"c{i}")
                        for i, s in enumerate(("test-agent", "test-doctor"))] + [AIMessage(content="ok")]
    models = {a: _Recorder(tag=a, responses=msgs) for a, msgs in scripts.items()}
    register_profiles(models["orchestrator"])
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        for d in ("sandbox", "shared"):
            (run / d).mkdir()
        subprocess.run(["git", "init", "-q", str(run / "sandbox")], check=True)
        backend = CompositeBackend(default=LocalShellBackend(root_dir=run, virtual_mode=True, env={"PATH": "/usr/bin:/bin"}),
                                   routes={"/sandbox/": FilesystemBackend(root_dir=run / "sandbox", virtual_mode=True),
                                           "/shared/": FilesystemBackend(root_dir=run / "shared", virtual_mode=True)},
                                   artifacts_root="/shared/")
        ctx = RunContext(run_id="selfcheck", repo="selfcheck/selfcheck", task_type="bug_fix", complexity=1,
                         skill=load_skills()["bug_fix"], backend=backend, shared_dir=run / "shared", mirror=run / "sandbox",
                         base_sha="HEAD", test_command="true", audit=_NullAudit(), attempts=AttemptState(), map_text="")
        build_graph(ctx, models, InMemorySaver()).invoke({"messages": [{"role": "user", "content": "selfcheck"}]},
                                                         {"configurable": {"thread_id": "selfcheck"}})
    return check_tool_sets(_BOUND)
