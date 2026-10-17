# tests/test_events.py
import json

from langgraph.checkpoint.memory import InMemorySaver

from codepilot.agents.graph import build_graph
from codepilot.events import EventRecorder, EventTranslator, RunEvent, replay, subagent_paths
from tests.test_graph import HAPPY, _issue_tool, ctx, models  # noqa: F401  (ctx is a fixture)


def test_translator_labels_agents_and_captures_tool_calls(ctx):
    ctx.issue_tool = _issue_tool()
    g = build_graph(ctx, models(HAPPY), InMemorySaver())
    tr, events = EventTranslator("r1"), []
    for ns, mode, data in g.stream({"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "r1"}},
                                   stream_mode=["updates", "messages"], subgraphs=True):
        events += tr.translate(ns, mode, data)
    calls = [(e.agent, e.data["name"]) for e in events if e.kind == "tool_call"]
    assert ("orchestrator", "read_claimed_issue") in calls and ("repo-explorer", "keyword_search") in calls
    assert ("coder", "edit_file") in calls and ("test-agent", "run_tests") in calls and ("pr-agent", "write_file") in calls
    task = [e for e in events if e.kind == "tool_call" and e.data["name"] == "task"]
    assert {t.data["args"]["subagent_type"] for t in task} == {"repo-explorer", "coder", "pr-agent", "test-agent", "test-doctor"}
    refused = [e for e in events if e.kind == "tool_result" and e.agent == "coder" and "refused" in e.text]
    assert refused
    assert subagent_paths(tr.final_text["repo-explorer"]) == ["pkg/slugs.py"]


def test_recorder_skips_tokens_and_replays(tmp_path):
    rec = EventRecorder(tmp_path / "events.jsonl")
    rec.write(RunEvent("r1", "token", "coder", "par"))
    rec.write(RunEvent("r1", "tool_call", "coder", "edit_file(...)", {"name": "edit_file"}))
    rec.write(RunEvent("r1", "state", "worker", "TESTING"))
    got = list(replay(tmp_path / "events.jsonl"))
    assert [e.kind for e in got] == ["tool_call", "state"] and got[0].data == {"name": "edit_file"}


def test_subagent_paths_dedupes_and_strips_prefix():
    assert subagent_paths("1. /sandbox/pkg/a.py - x\n2. /sandbox/tests/test_a.py.\n3. /sandbox/pkg/a.py") == ["pkg/a.py", "tests/test_a.py"]


def test_execute_exit_code_and_call_ids_are_kept():
    from langchain_core.messages import AIMessage, ToolMessage
    tr = EventTranslator("r1")
    tr.agent_by_ns[("x",)] = "coder"
    call = AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "make"}, "id": "c1", "type": "tool_call"}])
    res = ToolMessage(content="x" * 2000 + "\n[Command failed with exit code 127]", name="execute", tool_call_id="c1")
    ev = tr.translate(("x",), "updates", {"model": {"messages": [call]}}) + tr.translate(("x",), "updates", {"tools": {"messages": [res]}})
    assert ev[0].data["id"] == "c1" and ev[1].data["exit_code"] == 127 and ev[1].data["tool_call_id"] == "c1"


def test_list_content_from_real_claude_is_read():
    from langchain_core.messages import AIMessage, AIMessageChunk
    tr = EventTranslator("r1")
    chunk = AIMessageChunk(content=[{"type": "text", "text": "Looking at slugs", "index": 0}])
    ev = tr.translate((), "messages", (chunk, {"lc_agent_name": "repo-explorer"}))
    assert ev[0].kind == "token" and ev[0].text == "Looking at slugs"
    final = AIMessage(content=[{"type": "text", "text": "/sandbox/pkg/slugs.py", "index": 0}])
    tr.agent_by_ns[("x",)] = "repo-explorer"
    tr.translate(("x",), "updates", {"model": {"messages": [final]}})
    assert subagent_paths(tr.final_text["repo-explorer"]) == ["pkg/slugs.py"]
