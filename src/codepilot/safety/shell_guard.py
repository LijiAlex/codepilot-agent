# src/codepilot/safety/shell_guard.py
from __future__ import annotations

import re

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from codepilot.audit import AuditLog
from codepilot.safety.commands import classify_command, rule_for, unknown_parts

_PYTEST = re.compile(r"\bpytest\b")
SHELL_TESTS_NOTE = ("\n[CodePilot+] Tests run through the shell do not count as verification. "
                    "Call run_tests(purpose=\"verify\") so the result is recorded.")


def make_shell_guard(audit: AuditLog, run_id: str, agent: str, approved_commands: frozenset[str] | set[str] = frozenset()):
    approved = frozenset().union(*(unknown_parts(a) or {a} for a in approved_commands))   # parts, not exact strings

    @wrap_tool_call(name=f"shell_guard_{agent}")
    def shell_guard(request, handler):
        call = request.tool_call
        if call["name"] != "execute":
            return handler(request)
        cmd = str(call["args"].get("command", ""))
        c = classify_command(cmd, approved)
        if c.kind == "blocked":
            audit.log(f"agent:{agent}", "command_refused", run_id=run_id, target=cmd, result="refused",
                      detail={"reason": c.reason, "rule": rule_for(c.reason)})
            return ToolMessage(content=f"refused by CodePilot+ guard: {c.reason}." + (f" {c.hint}" if c.hint else ""),
                               name="execute", tool_call_id=call["id"], status="error")
        if c.kind == "unknown":
            audit.log(f"agent:{agent}", "command_needs_decision", run_id=run_id, target=cmd, result="paused",
                      detail={"reason": c.reason, "rule": rule_for(c.reason)})
            interrupt({"gate": "unexpected_command", "command": cmd, "agent": agent, "reason": c.reason})
        audit.log(f"agent:{agent}", "command_run", run_id=run_id, target=cmd)
        result = handler(request)
        if _PYTEST.search(cmd) and isinstance(result, ToolMessage):    # only run_tests results count (worker verification)
            result.content = f"{result.content}{SHELL_TESTS_NOTE}"
        return result

    return shell_guard
