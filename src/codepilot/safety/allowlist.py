# src/codepilot/safety/allowlist.py
from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage


def make_tool_allowlist(allowed: set[str], agent: str, audit, run_id: str):
    @wrap_tool_call(name=f"tool_allowlist_{agent}")
    def tool_allowlist(request, handler):
        name = request.tool_call["name"]
        if name not in allowed:
            audit.log(f"agent:{agent}", "tool_refused", run_id=run_id, target=name, result="refused")
            return ToolMessage(content=f"{name} is not available to the {agent}.", name=name,
                               tool_call_id=request.tool_call["id"], status="error")
        return handler(request)

    return tool_allowlist


def _secret_segment(path: str) -> bool:
    from codepilot.safety.commands import SENSITIVE
    parts = [p for p in str(path).split("/") if p]
    return any(SENSITIVE.search("/".join(parts[i:])) or SENSITIVE.search(parts[i]) for i in range(len(parts)))


def make_secret_path_guard(agent: str, audit, run_id: str):
    """deepagents' permission globs do not match dot-folders (no DOTGLOB, verified), so `.aws/credentials` or
    `x/.secrets/id.key` would pass them. This guard refuses file-tool paths with a secret-like segment anywhere, and drops
    such paths from ls/glob/grep results."""
    @wrap_tool_call(name=f"secret_path_guard_{agent}")
    def secret_path_guard(request, handler):
        call = request.tool_call
        path = str(call["args"].get("file_path") or call["args"].get("path") or "")
        if call["name"] in ("read_file", "write_file", "edit_file", "ls", "glob", "grep") and path and _secret_segment(path):
            audit.log(f"agent:{agent}", "secret_path_refused", run_id=run_id, target=path, result="refused")
            return ToolMessage(content=f"refused: {path} looks like a secret file", name=call["name"],
                               tool_call_id=call["id"], status="error")
        result = handler(request)
        if call["name"] in ("ls", "glob", "grep") and isinstance(result, ToolMessage) and isinstance(result.content, str):
            kept, hiding = [], False
            for line in result.content.splitlines():
                if line[:1].isspace():                   # grep content mode: "  N: text" lines belong to the last header
                    if not hiding:
                        kept.append(line)
                    continue
                hiding = _secret_segment(line.split(":", 1)[0].strip().strip("'\","))
                if not hiding:
                    kept.append(line)
            result = ToolMessage(content="\n".join(kept), name=result.name, tool_call_id=result.tool_call_id, status=result.status)
        return result
    return secret_path_guard
