# src/codepilot/events.py
"""Run events for the TUI log panel, the audit trail and replay.

The graph is streamed with stream_mode=["updates", "messages"], subgraphs=True (verified):
- "messages" metadata carries lc_agent_name for every message → we learn namespace → agent;
- "updates" carries complete model outputs (tool calls with full arguments, even when the model streams
  partial chunks) and tool results, and `__interrupt__` at namespace ().
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal

Kind = Literal["state", "tool_call", "tool_result", "message", "token", "interrupt", "notice", "error"]


@dataclass
class RunEvent:
    run_id: str
    kind: Kind
    agent: str
    text: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


_EXIT = re.compile(r"\[Command (?:succeeded|failed) with exit code (-?\d+)\]")


def _short(value, n: int = 300) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


class EventTranslator:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.agent_by_ns: dict[tuple, str] = {(): "orchestrator"}
        self.interrupts: list[dict] = []
        self.final_text: dict[str, str] = {}          # last plain answer per agent (explorer's file list, etc.)
        self.tokens_in = 0
        self.tokens_out = 0
        self.todos: list = []                             # latest write_todos list (handoff payloads, E10)

    def translate(self, ns: tuple, mode: str, data) -> list[RunEvent]:
        out: list[RunEvent] = []
        if mode == "messages":
            msg, meta = data
            agent = meta.get("lc_agent_name") or self.agent_by_ns.get(ns, "agent")
            self.agent_by_ns.setdefault(ns, agent)
            text = msg.text if type(msg).__name__ == "AIMessageChunk" else ""      # Claude streams list content: use .text
            if text:
                out.append(RunEvent(self.run_id, "token", agent, text))
            return out
        if not isinstance(data, dict):
            return out
        if "__interrupt__" in data:
            for itr in data["__interrupt__"]:
                self.interrupts.append(itr.value)
                out.append(RunEvent(self.run_id, "interrupt", self.agent_by_ns.get(ns, "agent"),
                                    f"needs a decision: {itr.value.get('gate')}", dict(itr.value)))
            return out
        agent = self.agent_by_ns.get(ns, "agent")
        for node, update in data.items():
            if not isinstance(update, dict):
                continue
            for m in update.get("messages", []) or []:
                usage = getattr(m, "usage_metadata", None) if node == "model" else None
                if usage:
                    self.tokens_in += usage.get("input_tokens", 0)
                    self.tokens_out += usage.get("output_tokens", 0)
                if node == "model" and getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        if tc["name"] == "write_todos":
                            self.todos = tc["args"].get("todos", [])
                        out.append(RunEvent(self.run_id, "tool_call", agent, f"{tc['name']}({_short(tc['args'], 160)})",
                                            {"name": tc["name"], "args": tc["args"], "id": tc.get("id")}))
                elif node == "model" and m.text:
                    self.final_text[agent] = m.text
                    out.append(RunEvent(self.run_id, "message", agent, _short(m.text, 2000)))
                elif node == "tools" and type(m).__name__ == "ToolMessage":
                    status = getattr(m, "status", "success")
                    data = {"name": m.name, "status": status, "tool_call_id": getattr(m, "tool_call_id", None)}
                    if m.name == "execute":                       # exit code is at the end; read it before shortening
                        found = _EXIT.search(str(m.content))
                        if found:
                            data["exit_code"] = int(found.group(1))
                    out.append(RunEvent(self.run_id, "tool_result", agent, _short(str(m.content)), data))
        return out


class EventRecorder:
    """Append-only JSONL per run (~/.codepilot/runs/<id>/events.jsonl) for replay and the dashboard."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, e: RunEvent) -> None:
        if e.kind == "token":
            return                                        # tokens are for the live view only
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(e), default=str) + "\n")


def replay(path: Path, speed: float = 0.0) -> Iterator[RunEvent]:
    """Yield recorded events; speed > 0 sleeps the recorded gaps divided by speed (demo --replay)."""
    prev = None
    for line in Path(path).read_text().splitlines():
        e = RunEvent(**json.loads(line))
        if speed and prev is not None:
            time.sleep(max(0.0, min(5.0, (e.ts - prev) / speed)))
        prev = e.ts
        yield e


def subagent_paths(text: str) -> list[str]:
    """Repository paths an agent listed (e.g. the explorer's answer), without the /sandbox/ prefix."""
    import re
    return list(dict.fromkeys(m.removeprefix("/sandbox/") for m in re.findall(r"/sandbox/[\w./\-]+\w", text)))
