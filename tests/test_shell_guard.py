# tests/test_shell_guard.py
import os
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from codepilot.db import connect
from codepilot.audit import AuditLog
from codepilot.safety.shell_guard import make_shell_guard
from tests.fakes import FakeClaude, tool_call

def _agent(tmp_path, script):
    audit = AuditLog(connect(tmp_path / "cp.db"))
    guard = make_shell_guard(audit, run_id="r1", agent="coder")
    agent = create_deep_agent(model=FakeClaude(responses=script), middleware=[guard],
                              backend=LocalShellBackend(root_dir=str(tmp_path), env={"PATH": "/usr/bin:/bin"}),
                              checkpointer=InMemorySaver())
    return agent, audit

def test_blocked_command_refused_without_interrupt(tmp_path):
    agent, audit = _agent(tmp_path, [tool_call("execute", {"command": "git push origin main"}, "1"), AIMessage(content="ok")])
    out = agent.invoke({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "t"}})
    tool_msgs = [m for m in out["messages"] if m.type == "tool"]
    assert "refused" in tool_msgs[0].content and tool_msgs[0].name == "execute"
    assert audit.conn.execute("select count(*) from events where action='command_refused'").fetchone()[0] == 1

def test_unknown_command_interrupts(tmp_path):
    agent, _ = _agent(tmp_path, [tool_call("execute", {"command": "docker build ."}, "1"), AIMessage(content="ok")])
    seen = []
    for ns, mode, data in agent.stream({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "t"}},
                                       stream_mode=["updates"], subgraphs=True):
        if isinstance(data, dict) and "__interrupt__" in data:
            seen += [i.value for i in data["__interrupt__"]]
    assert seen and seen[0]["gate"] == "unexpected_command" and seen[0]["command"] == "docker build ."

def test_refusal_message_matches_the_reason(tmp_path):
    agent, _ = _agent(tmp_path, [tool_call("execute", {"command": "python -c \"print(1)\""}, "1"),
                                 tool_call("execute", {"command": "pip install x"}, "2"), AIMessage(content="ok")])
    out = agent.invoke({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "t"}})
    inline, pip = [m.content for m in out["messages"] if m.type == "tool"]
    assert inline.startswith("refused by CodePilot+ guard: inline python is not allowed") and "install" not in inline
    assert "do not try to push, download or install" in pip


def test_refusals_are_logged_with_the_rule_name(tmp_path):
    import json
    agent, audit = _agent(tmp_path, [tool_call("execute", {"command": "python -m pip show pydantic"}, "1"), AIMessage(content="ok")])
    agent.invoke({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "t"}})
    row = audit.conn.execute("select detail_json from events where action='command_refused'").fetchone()
    assert json.loads(row["detail_json"])["rule"] == "pip-install"


def test_a_variant_of_an_approved_command_does_not_ask_again(tmp_path):
    # live 2026-10-19, issue #9: `make lint` approved, the agents then ran it combined differently and were stopped again
    audit = AuditLog(connect(tmp_path / "cp.db"))
    guard = make_shell_guard(audit, run_id="r1", agent="test-agent", approved_commands=frozenset({"make lint"}))
    agent = create_deep_agent(model=FakeClaude(responses=[tool_call("execute", {"command": "cd . && make lint"}, "1"),
                                                          AIMessage(content="ok")]), middleware=[guard],
                              backend=LocalShellBackend(root_dir=str(tmp_path), env={"PATH": "/usr/bin:/bin"}),
                              checkpointer=InMemorySaver())
    seen = []
    for ns, mode, data in agent.stream({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "t"}},
                                       stream_mode=["updates"], subgraphs=True):
        if isinstance(data, dict) and "__interrupt__" in data:
            seen += [i.value for i in data["__interrupt__"]]
    assert seen == []
    assert audit.conn.execute("select count(*) from events where action='command_needs_decision'").fetchone()[0] == 0
