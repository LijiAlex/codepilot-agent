# src/codepilot/improve/trace.py
"""Trace analysis of one run from CodePilot+'s own records — the audit log and the run's event file
(LangSmith inputs are redacted, so it is not the primary source)."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def _events(runs_dir: Path, run_id: str) -> list[dict]:
    p = Path(runs_dir) / run_id / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def analyse_run(conn, runs_dir: Path, run_id: str) -> dict:
    run = conn.execute("select * from runs where id=?", (run_id,)).fetchone()
    events = _events(runs_dir, run_id)
    calls = {e["data"].get("id"): e for e in events if e["kind"] == "tool_call" and e["data"].get("id")}
    tool_calls = Counter(e["agent"] for e in events if e["kind"] == "tool_call")
    failed_tools = Counter(e["agent"] for e in events if e["kind"] == "tool_result" and e["data"].get("status") == "error")
    commands, failed_commands = [], []
    first_verify = None
    for e in events:
        if e["kind"] != "tool_result":
            continue
        call = calls.get(e["data"].get("tool_call_id"))
        if call and e["data"].get("name") == "execute" and "exit_code" in e["data"]:
            cmd = str(call["data"]["args"].get("command", ""))[:200]
            commands.append({"agent": call["agent"], "command": cmd, "exit_code": e["data"]["exit_code"]})
            if e["data"]["exit_code"] != 0:
                failed_commands.append({"agent": call["agent"], "command": cmd, "exit_code": e["data"]["exit_code"]})
        if call and e["data"].get("name") == "run_tests" and call["data"]["args"].get("purpose") == "verify" and first_verify is None:
            try:
                first_verify = json.loads(e["text"].split("\n", 1)[0]).get("outcome")
            except (ValueError, AttributeError):
                first_verify = None
    refused = [r[0] for r in conn.execute("select target from events where run_id=? and action='command_refused'", (run_id,))]
    h = conn.execute("select payload_json from handoffs where run_id=? and entry_point in ('publish','publish_failed','continue_fix','continue_with_decision') "
                     "order by created_at desc limit 1", (run_id,)).fetchone()
    changed = [s["path"] for s in json.loads(h["payload_json"]).get("saved", [])] if h else []
    reads = [str(e["data"]["args"].get("file_path", "")).removeprefix("/sandbox/") for e in events
             if e["kind"] == "tool_call" and e["data"].get("name") == "read_file" and e["agent"] in ("repo-explorer", "coder")]
    first_hit = next((i for i, p in enumerate(reads) if p in changed), None)
    ctx = next((e["data"] for e in events if e["kind"] == "notice" and e.get("data", {}).get("kind") == "context"), {})
    minutes = round((events[-1]["ts"] - events[0]["ts"]) / 60, 1) if len(events) > 1 else 0.0
    return {"run_id": run_id, "repo": run["repo"], "task_type": run["task_type"], "outcome": run["state"], "reason": run["reason"],
            "repo_skill_version": ctx.get("repo_skill_version", 0), "tool_calls": dict(tool_calls), "failed_tool_calls": dict(failed_tools),
            "commands": commands[-30:], "failed_commands": failed_commands[-15:], "refused_commands": refused,
            "files_read": reads[:40], "changed_files": changed,
            "files_read_before_right_one": first_hit if first_hit is not None else len(reads),
            "first_try_test_pass": first_verify == "passed" if first_verify else None,
            "minutes": minutes, "tokens": (run["tokens_in"] or 0) + (run["tokens_out"] or 0)}


def save(runs_dir: Path, analysis: dict) -> Path:
    p = Path(runs_dir) / analysis["run_id"] / "trace_analysis.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(analysis, indent=1))
    return p
