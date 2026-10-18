# src/codepilot/dashboard/data.py
"""Read models for the dashboard. Every value that came from GitHub or an agent is returned as plain data;
the page renders it with textContent only."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Any

STAGES = ["Triaged", "Explored", "Implemented", "Tested", "File check", "Approved", "PR opened", "Merged"]


def _events(runs_dir: Path, run_id: str, limit: int | None = None) -> list[dict]:
    p = Path(runs_dir) / run_id / "events.jsonl"
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    return [json.loads(x) for x in (lines[-limit:] if limit else lines)]


def overview(conn: sqlite3.Connection, cost_tracked: bool = False) -> dict:
    by_state = {r["state"]: r["n"] for r in conn.execute("select state, count(*) n from runs group by state")}
    count = lambda a: conn.execute("select count(*) from events where action=?", (a,)).fetchone()[0]  # noqa: E731
    tok = conn.execute("select coalesce(sum(tokens_in),0) ti, coalesce(sum(tokens_out),0) tout, sum(cost_usd) c from runs").fetchone()
    try:
        proposals = conn.execute("select count(*) from repo_skill_proposals where status='pending'").fetchone()[0]
    except sqlite3.OperationalError:                   # improvement loop tables not created yet
        proposals = 0
    return {"runs": sum(by_state.values()), "by_state": by_state,
            "prs_opened": conn.execute("select count(*) from runs where pr_url is not null").fetchone()[0],
            "pending_decisions": conn.execute("select count(*) from handoffs where status='pending'").fetchone()[0],
            "commands_refused": count("command_refused"), "issues_blocked": count("issue_blocked"),
            "proposals_waiting": proposals,
            "tokens_in": tok["ti"], "tokens_out": tok["tout"],
            "cost_usd": round(tok["c"] or 0, 2) if cost_tracked else None}      # None = no prices in codepilot.yml


def _stage_marks(conn, runs_dir: Path, run: sqlite3.Row) -> dict[str, str]:
    """done / failed / '' per stage, from state events, handoffs and the final state."""
    seen = {e["text"] for e in _events(runs_dir, run["id"]) if e["kind"] == "state"}
    marks = {s: "" for s in STAGES}
    marks["Triaged"] = "done" if run["task_type"] else ""
    if "EXPLORING" in seen: marks["Explored"] = "done"
    if "IMPLEMENTING" in seen: marks["Implemented"] = "done"
    if "TESTING" in seen: marks["Tested"] = "done"
    pub = conn.execute("select status from handoffs where run_id=? and entry_point in ('publish','publish_failed') "
                       "order by created_at desc limit 1", (run["id"],)).fetchone()
    reason = run["reason"] or ""
    if pub:
        marks["File check"] = "done"
        marks["Approved"] = {"approved": "done", "rejected": "failed"}.get(pub["status"], "")
    elif reason.startswith("file_check"):
        marks["File check"] = "failed"
    if reason in ("tests", "untested"):
        marks["Tested"] = "failed"
    if run["pr_url"]:
        marks["PR opened"] = "done"
    if run["state"] == "DONE":
        marks["Merged"] = "done"
    return marks


def matrix(conn, runs_dir: Path) -> dict:
    rows = []
    for r in conn.execute("select * from runs order by created_at desc limit 200"):
        rows.append({"run_id": r["id"], "repo": r["repo"], "issue": r["issue_key"], "title": r["title"], "state": r["state"],
                     "reason": r["reason"], "type": r["task_type"], "pr_url": r["pr_url"], "marks": _stage_marks(conn, runs_dir, r)})
    return {"stages": STAGES, "rows": rows}


def run_detail(conn, runs_dir: Path, run_id: str) -> dict | None:
    r = conn.execute("select * from runs where id=?", (run_id,)).fetchone()
    if r is None:
        return None
    hs = [dict(h) | {"payload": json.loads(h["payload_json"])} for h in
          conn.execute("select * from handoffs where run_id=? order by created_at", (run_id,))]
    for h in hs:
        h.pop("payload_json", None)
        h["payload"].pop("saved", None)                   # file contents stay out of the page; the diff is enough
    events = _events(runs_dir, run_id)
    ctx = next((e["data"] for e in events if e["kind"] == "notice" and e.get("data", {}).get("kind") == "context"), None)
    trace = Path(runs_dir) / run_id / "trace_analysis.json"
    gap = Path(runs_dir) / run_id / "skill_gap_report.json"
    return {"run": dict(r), "handoffs": hs, "events": events[-400:], "context": ctx,
            "trace_analysis": json.loads(trace.read_text()) if trace.exists() else None,
            "skill_gap_report": json.loads(gap.read_text()) if gap.exists() else None,
            "audit": [dict(e) for e in conn.execute("select * from events where run_id=? order by id", (run_id,))],
            "errors": [_error_text(e["detail_json"]) for e in conn.execute(
                "select detail_json from events where run_id=? and action='run_error' order by id", (run_id,))]}


def _error_text(detail_json: str | None) -> str:
    try:
        d = json.loads(detail_json or "{}")
    except ValueError:
        return str(detail_json)
    return d.get("output_tail") or f"{d.get('type', 'error')}: {d.get('msg', '')}".strip()


def safety(conn, results_dir: Path) -> dict:
    def rows(action):
        return [dict(e) for e in conn.execute("select ts, run_id, actor, target, result, detail_json from events "
                                              "where action=? order by id desc limit 100", (action,))]
    g = Path(results_dir) / "guardrail.json"
    return {"commands_refused": rows("command_refused"), "commands_needing_decision": rows("command_needs_decision"),
            "tools_refused": rows("tool_refused"), "issues_blocked": rows("issue_blocked"),
            "guardrail_measurement": _measurement(g)}


def _measurement(g: Path) -> dict | None:
    if not g.exists():
        return None
    m = json.loads(g.read_text())
    m["missed"] = [r["id"] for r in m.get("rows", []) if r.get("label") == "malicious" and not r.get("blocked")]
    m["false_alarm_ids"] = [r["id"] for r in m.get("rows", []) if r.get("label") == "benign" and r.get("blocked")]
    m["measured_at"] = datetime.fromtimestamp(g.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return m


def context(conn, runs_dir: Path, budget: int = 4000) -> dict:
    out = []
    for r in conn.execute("select * from runs where task_type is not null order by created_at desc limit 100"):
        ctx = next((e["data"] for e in _events(runs_dir, r["id"]) if e["kind"] == "notice"
                    and e.get("data", {}).get("kind") == "context"), None)
        out.append({"run_id": r["id"], "issue": r["issue_key"], "repo": r["repo"], "tokens_in": r["tokens_in"],
                    "tokens_out": r["tokens_out"], "context": ctx})
    return {"runs": out, "budget": budget}


def memory(lessons: Any, episodic: Any, repos: list[str]) -> dict:
    return {"lessons": {repo: lessons.all(repo) for repo in repos} if lessons is not None else {},
            "sessions": episodic.last_sessions(20) if episodic is not None else [],
            "episodes": {repo: episodic.episodes(repo, 50) for repo in repos} if episodic is not None else {}}


def skills(skill_map: dict) -> dict:
    return {"brief_skills": [s.model_dump() for s in skill_map.values()]}


def audit(conn, limit: int = 500) -> dict:
    return {"events": [dict(e) for e in conn.execute("select * from events order by id desc limit ?", (limit,))]}


def pull_requests(repo, reviewer_login: str | None) -> list[dict]:
    out = []
    for pr in repo.get_pulls(state="open"):
        labels = [label.name for label in pr.labels]
        review = None
        if reviewer_login:
            mine = [r for r in pr.get_reviews() if r.user and r.user.login == reviewer_login]
            if mine:
                last = mine[-1]
                review = {"body": (last.body or "")[:4000], "commit_id": last.commit_id, "current": last.commit_id == pr.head.sha}
        out.append({"number": pr.number, "title": pr.title, "author": pr.user.login if pr.user else "", "url": pr.html_url,
                    "head_sha": pr.head.sha, "labels": labels, "codepilot": "codepilot-generated" in labels,
                    "merge_ready": "merge-ready" in labels, "mergeable": pr.mergeable, "review": review})
    return out


class MergeRefused(Exception):
    pass


def merge_pr(repo, number: int, expected_head: str, reviewer_login: str | None) -> dict:
    """Merge only what the page showed: same head SHA, merge-ready label, a current independent review, mergeable."""
    pr = repo.get_pull(number)
    if pr.head.sha != expected_head:
        raise MergeRefused("the PR changed since the page loaded; reload and look again")
    if "merge-ready" not in {label.name for label in pr.labels}:
        raise MergeRefused("not labelled merge-ready by the independent reviewer")
    if not reviewer_login:
        raise MergeRefused("no independent reviewer is configured (reviewer_login): merge on GitHub instead")
    mine = [r for r in pr.get_reviews() if r.user and r.user.login == reviewer_login]
    if not mine or mine[-1].commit_id != pr.head.sha:
        raise MergeRefused("the independent review is not for the current commit")
    if "**Merge-ready:** yes" not in (mine[-1].body or ""):      # the label could be stale or added by hand
        raise MergeRefused("the latest independent review does not say merge-ready")
    if pr.mergeable is False:
        raise MergeRefused("GitHub reports conflicts")
    status = pr.merge(sha=expected_head, merge_method="squash")
    return {"merged": bool(status.merged), "sha": status.sha, "message": status.message}


def close_pr(repo, number: int, expected_head: str) -> dict:
    pr = repo.get_pull(number)
    if pr.head.sha != expected_head:
        raise MergeRefused("the PR changed since the page loaded; reload and look again")
    pr.edit(state="closed")
    return {"closed": True}
