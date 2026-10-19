# src/codepilot/safety/shell_guard.py
from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from codepilot.audit import AuditLog
from codepilot.safety.commands import classify_command, rule_for, unknown_parts


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
        return handler(request)

    return shell_guard
