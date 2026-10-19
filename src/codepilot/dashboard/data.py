# src/codepilot/dashboard/data.py
"""Read models for the dashboard. Every value that came from GitHub or an agent is returned as plain data;
the page renders it with textContent only."""
from __future__ import annotations

import json
import re
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


def langsmith_costs(client: Any, project: str, limit: int = 100) -> dict[str, float]:   # 100 = LangSmith query maximum
    """Cost per CodePilot+ run from LangSmith (it prices every model call, incl. prompt-cache discounts).
    Root traces carry metadata.run_id (worker config); a continuation adds to the same run."""
    out: dict[str, float] = {}
    for r in client.list_runs(project_name=project, is_root=True, limit=limit):
        rid = ((getattr(r, "extra", None) or {}).get("metadata") or {}).get("run_id")
        if rid and getattr(r, "total_cost", None) is not None:
            out[rid] = round(out.get(rid, 0.0) + float(r.total_cost), 6)
    return out


def langsmith_usage(client: Any, project: str, reviewer_project: str | None = None, limit: int = 100) -> dict:
    """Tokens, cost and trace counts from LangSmith: per CodePilot+ run (metadata.run_id), the injection screen, and the
    reviewer (also per PR, from the review trace's metadata repo + pr)."""
    def add(bucket: dict, r) -> None:
        bucket["count"] = bucket.get("count", 0) + 1
        bucket["cost"] = round(bucket.get("cost", 0.0) + float(getattr(r, "total_cost", None) or 0), 6)
        bucket["tokens_in"] = bucket.get("tokens_in", 0) + int(getattr(r, "prompt_tokens", None) or 0)
        bucket["tokens_out"] = bucket.get("tokens_out", 0) + int(getattr(r, "completion_tokens", None) or 0)
    out: dict = {"runs": {}, "screen": {}, "improvement": {}, "reviewer": {}, "reviewer_by_pr": {}}
    for r in client.list_runs(project_name=project, is_root=True, limit=limit):
        meta = (getattr(r, "extra", None) or {}).get("metadata") or {}
        if meta.get("run_id"):
            add(out["runs"].setdefault(meta["run_id"], {}), r)
        elif meta.get("kind") == "injection-screen":
            add(out["screen"], r)
        elif meta.get("kind") == "skill-gap-report":                 # self-improvement: trace analysis → skill gap report
            add(out["improvement"], r)
    if reviewer_project:
        for r in client.list_runs(project_name=reviewer_project, is_root=True, limit=limit):
            add(out["reviewer"], r)
            meta = (getattr(r, "extra", None) or {}).get("metadata") or {}
            if meta.get("repo") and meta.get("pr") is not None:
                add(out["reviewer_by_pr"].setdefault(f"{meta['repo']}#{meta['pr']}", {}), r)
    return out


INFRA_REASONS = ("setup:", "crashed", "sandbox_lost", "error:")


def _outcome(state: str, reason: str | None) -> str:
    r = reason or ""
    if state in ("DONE", "PR_OPENED") or (state == "AWAITING_APPROVAL" and r in ("publish", "publish_failed")):
        return "solved"                                  # a verified change exists (waiting only for the person's approval)
    if state == "FAILED" and r.startswith("no change made"):
        return "declined"                                # the agents explained why they changed nothing
    if state == "FAILED" and r.startswith(INFRA_REASONS):
        return "infrastructure"                          # sandbox / crash / internal error: not the agents' answer
    if state in ("FAILED", "CLOSED", "SKIPPED"):
        return "failed"
    return "open"                                        # still running or waiting at another gate


def issue_outcomes(conn: sqlite3.Connection) -> dict:
    """Success per issue (its latest run), not per run: retries and infrastructure failures do not hide the answer."""
    runs: dict[tuple, list] = {}
    for r in conn.execute("select repo, issue_key, state, reason from runs order by rowid"):
        runs.setdefault((r["repo"], r["issue_key"]), []).append(r)
    counts = {"issues": len(runs), "solved": 0, "declined": 0, "failed": 0, "infrastructure": 0, "open": 0, "first_attempt": 0}
    for attempts in runs.values():
        out = _outcome(attempts[-1]["state"], attempts[-1]["reason"])
        counts[out] += 1
        if out == "solved" and len(attempts) == 1:
            counts["first_attempt"] += 1
    judged = counts["issues"] - counts["infrastructure"]
    counts["rate"] = round(counts["solved"] / judged, 2) if judged else None
    return counts


def overview(conn: sqlite3.Connection, cost_tracked: bool = False, trace_costs: dict | None = None, trace_usage: dict | None = None) -> dict:
    by_state = {r["state"]: r["n"] for r in conn.execute("select state, count(*) n from runs group by state")}
    count = lambda a: conn.execute("select count(*) from events where action=?", (a,)).fetchone()[0]  # noqa: E731
    tok = conn.execute("select coalesce(sum(tokens_in),0) ti, coalesce(sum(tokens_out),0) tout, sum(cost_usd) c from runs").fetchone()
    try:
        proposals = conn.execute("select count(*) from repo_skill_proposals where status='pending'").fetchone()[0]
    except sqlite3.OperationalError:                   # improvement loop tables not created yet
        proposals = 0
    if trace_usage:
        trace_costs = {rid: u.get("cost", 0.0) for rid, u in trace_usage["runs"].items()}
        extra = sum(trace_usage.get(k, {}).get("cost", 0.0) for k in ("screen", "improvement", "reviewer"))
    else:
        extra = 0.0
    return {"runs": sum(by_state.values()), "by_state": by_state, "usage": trace_usage, "issues": issue_outcomes(conn),
            "prs_opened": conn.execute("select count(*) from runs where pr_url is not null").fetchone()[0],
            "pending_decisions": conn.execute("select count(*) from handoffs where status='pending'").fetchone()[0],
            "commands_refused": count("command_refused"), "issues_blocked": count("issue_blocked"),
            "proposals_waiting": proposals,
            "tokens_in": tok["ti"], "tokens_out": tok["tout"],
            "cost_usd": (round(sum((trace_costs or {}).values()) + extra, 2) if (trace_costs or trace_usage) else round(tok["c"] or 0, 2) if cost_tracked else None),
            "cost_source": "LangSmith" if (trace_costs or trace_usage) else ("codepilot.yml prices" if cost_tracked else None)}


def overview_skills(conn: sqlite3.Connection, repos: list[str]) -> dict:
    """Runs and outcomes per task skill, and each repo's current repo-skill version with its waiting / approved changes."""
    by_task: dict[str, dict] = {}
    for r in conn.execute("select task_type, state, count(*) n from runs where task_type is not null group by 1, 2 order by 1"):
        t = by_task.setdefault(r["task_type"], {"task_type": r["task_type"], "runs": 0, "outcomes": {}})
        t["runs"] += r["n"]
        t["outcomes"][r["state"]] = r["n"]
    out_repos = []
    for repo in repos:
        try:
            v = conn.execute("select max(version) from repo_skill_versions where repo=?", (repo,)).fetchone()[0] or 0
            n = dict(conn.execute("select status, count(*) from repo_skill_proposals where repo=? group by status", (repo,)).fetchall())
        except sqlite3.OperationalError:               # improvement loop tables not created yet
            v, n = 0, {}
        out_repos.append({"repo": repo, "version": v, "waiting": n.get("pending", 0), "approved": n.get("approved", 0)})
    return {"by_task": list(by_task.values()), "repos": out_repos}


def _stage_marks(conn, runs_dir: Path, run: sqlite3.Row) -> dict[str, str]:
    """done / failed / '' per stage, from state events, handoffs and the final state."""
    events = _events(runs_dir, run["id"])
    seen = {e["text"] for e in events if e["kind"] == "state"}
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
    if reason == "untested" and any(_tests_passed(e) for e in events):
        marks["Tested"] = "voided"                     # tests passed, but a change after them was not re-tested
    if run["pr_url"]:
        marks["PR opened"] = "done"
    if run["state"] == "DONE":
        marks["Merged"] = "done"
    return marks


def _tests_passed(e: dict) -> bool:
    return e["kind"] == "tool_result" and (e.get("data") or {}).get("name") == "run_tests" \
        and str(e.get("text", "")).startswith('{"outcome": "passed"')


def matrix(conn, runs_dir: Path, trace_costs: dict | None = None) -> dict:
    rows = []
    for r in conn.execute("select * from runs order by created_at desc limit 200"):
        rows.append({"run_id": r["id"], "repo": r["repo"], "issue": r["issue_key"], "title": r["title"], "state": r["state"],
                     "started": r["created_at"], "ended": r["ended_at"],
                     "reason": r["reason"], "type": r["task_type"], "pr_url": r["pr_url"], "marks": _stage_marks(conn, runs_dir, r),
                     "cost_usd": (trace_costs or {}).get(r["id"])})
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


RAIL_NAMES = {"regex check input": "NeMo regex rail", "self check input": "NeMo self-check rail"}


def _guard(action: str, row: dict) -> str:
    """Which guardrail produced this row (and which rule inside it)."""
    from codepilot.safety.commands import rule_for
    try:
        d = json.loads(row.get("detail_json") or "{}")
    except ValueError:
        d = {}
    if action in ("command_refused", "command_needs_decision"):
        if d.get("rule"):
            return "Shell guard · " + d["rule"]
        from codepilot.safety.commands import classify_command
        now = classify_command(row.get("target") or "")            # rows saved before rules had names: ask today's guard
        return "Shell guard · " + rule_for(now.reason if now.kind != "allowed" else d.get("reason", ""))
    if action == "tool_refused":
        return "Tool allow-list"
    if action == "issue_blocked":
        if row.get("result") == "failed_closed":
            return "Injection screen · unavailable (held back)"
        rails = [RAIL_NAMES.get(r, r) for r in d.get("reasons", []) if isinstance(r, str)]
        return "Injection screen" + (" · " + ", ".join(rails) if rails else "")
    return ""


def safety(conn, results_dir: Path) -> dict:
    def rows(action):
        out = [dict(e) for e in conn.execute("select ts, run_id, actor, target, result, detail_json from events "
                                             "where action=? order by id desc limit 100", (action,))]
        for r in out:
            r["guard"] = _guard(action, r)
        return out
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


class ReadOnlyLessons:
    """Lessons for the standalone dashboard, read straight from ChromaDB's SQLite file in read-only mode.
    Opening a second Chroma client on the folder the TUI uses is unsafe (one process per Chroma folder), but a
    read-only SQLite connection is fine: SQLite's own locking keeps readers and the TUI's writes consistent.
    Chroma's tables are internal, so any schema surprise shows as "no lessons" rather than an error."""

    def __init__(self, chroma_dir: Path):
        self.db = Path(chroma_dir) / "chroma.sqlite3"

    def all(self, repo: str) -> list[dict]:
        if not self.db.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "select e.embedding_id, m.key, coalesce(m.string_value, m.int_value, m.float_value) "
                    "from embeddings e join segments s on s.id=e.segment_id join collections c on c.id=s.collection "
                    "join embedding_metadata m on m.id=e.id where c.name='lessons'").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        by_id: dict[str, dict] = {}
        for lid, key, val in rows:
            by_id.setdefault(lid, {"id": lid})["text" if key == "chroma:document" else key] = val
        return sorted((x for x in by_id.values() if x.get("repo") == repo), key=lambda x: x.get("ts") or 0)


def memory(lessons: Any, episodic: Any, repos: list[str]) -> dict:
    return {"lessons": {repo: lessons.all(repo) for repo in repos} if lessons is not None else {},
            "sessions": episodic.last_sessions(20) if episodic is not None else [],
            "episodes": {repo: episodic.episodes(repo, 50) for repo in repos} if episodic is not None else {}}


def skills(skill_map: dict) -> dict:
    return {"brief_skills": [s.model_dump() for s in skill_map.values()]}


def audit(conn, limit: int = 2000) -> dict:
    """linked_run: for rows logged without a run (PR merge / close from the dashboard), the run that opened that PR."""
    by_pr = {f"{r['repo']}#{r['pr_number']}": r["id"] for r in
             conn.execute("select id, repo, pr_number from runs where pr_number is not null order by created_at")}
    events = [dict(e) for e in conn.execute("select * from events order by id desc limit ?", (limit,))]
    for e in events:
        e["linked_run"] = e["run_id"] or by_pr.get(e.get("target") or "")
    return {"events": events}


def parse_review(body: str) -> dict:
    """The independent reviewer's comment, split into fields for the PR table (its format is fixed by review.py)."""
    b = body or ""
    num = lambda pat: int(m.group(1)) if (m := re.search(pat, b)) else None      # noqa: E731
    matches = re.search(r"\*\*Matches the issue:\*\*\s*(.+)", b)
    sec = re.search(r"\*\*Security concerns:\*\*\n((?:- .+\n?)+)", b)
    ready = re.search(r"\*\*Merge-ready:\*\*\s*(yes|no)(?:\s*—\s*(.+))?", b)
    cap = re.search(r"\*\*Review effort:\*\*\s*\d+/5 \(([^)\n]+)\)", b)
    rub = re.search(r"\*\*Rubric:\*\*\s*(.+)", b)
    rubric = {m.group(1).strip(): int(m.group(2)) for m in re.finditer(r"([a-z][a-z ]*?) (\d)/2", rub.group(1))} if rub else {}
    reasons = {}
    if rub:                                            # "- <criterion>: <reason>" lines right after the rubric line
        for line in b[rub.end():].lstrip("\n").splitlines():
            m = re.match(r"- ([a-z][a-z ]*): (.+)", line)
            if not m or m.group(1) not in rubric:
                break
            reasons[m.group(1)] = m.group(2).strip()
    return {"score": num(r"\*\*Score:\*\*\s*(\d+)/10"), "effort": num(r"\*\*Review effort:\*\*\s*(\d+)/5"),
            "cap": cap.group(1).strip() if cap else "", "rubric": rubric, "rubric_reasons": reasons,
            "matches": matches.group(1).strip() if matches else "",
            "security": [x[2:].strip() for x in sec.group(1).strip().splitlines()] if sec else [],
            "merge_ready": (ready.group(1) == "yes") if ready else None,
            "why_not": (ready.group(2) or "").strip() if ready else "",
            "tests_not_run": "Tests not run" in b}


MIN_SCORE = 7


def attention(pr: dict) -> list[str]:
    """Why a person should look at this PR now (empty = nothing unusual)."""
    why = []
    if pr.get("from_fork"):
        why.append("from a fork")
    rev = pr.get("review")
    if not rev:
        why.append("not reviewed yet")
        return why
    f = rev.get("fields") or {}
    if not rev.get("current", True):
        why.append("review is for an older commit")
    if f.get("security"):
        why.append("security concerns")
    if f.get("score") is not None and f["score"] < MIN_SCORE:
        why.append(f"low score ({f['score']}/10)")
    if f.get("tests_not_run") and not pr.get("from_fork"):
        why.append("tests not run")
    return why


def pull_requests(repo, reviewer_login: str | None) -> list[dict]:
    out = []
    for pr in repo.get_pulls(state="open"):
        labels = [label.name for label in pr.labels]
        review = None
        if reviewer_login:
            mine = [r for r in pr.get_reviews() if r.user and r.user.login == reviewer_login]
            if mine:
                last = mine[-1]
                review = {"body": (last.body or "")[:4000], "commit_id": last.commit_id, "current": last.commit_id == pr.head.sha,
                          "fields": parse_review(last.body or "")}
        head_repo = getattr(getattr(pr.head, "repo", None), "full_name", None)
        row = {"number": pr.number, "title": pr.title, "author": pr.user.login if pr.user else "", "url": pr.html_url,
               "head_sha": pr.head.sha, "labels": labels, "codepilot": "codepilot-generated" in labels,
               "merge_ready": "merge-ready" in labels, "mergeable": pr.mergeable, "review": review,
               "from_fork": bool(head_repo) and head_repo != getattr(repo, "full_name", head_repo)}
        row["attention"] = attention(row)
        out.append(row)
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


def overview_summary(conn: sqlite3.Connection, repos: list[str], lessons: Any, improvements: Any, results_dir: Path) -> dict:
    """The Overview's Safety and Learning panels: counts only, each panel links to its tab for the detail."""
    from collections import Counter
    s = safety(conn, results_dir)
    rules = Counter(r["guard"].split(" · ", 1)[-1] for r in s["commands_refused"])
    m = s.get("guardrail_measurement") or {}
    safety_panel = {"refused": len(s["commands_refused"]), "top_rules": [[k, n] for k, n in rules.most_common(3)],
                    "issues_blocked": len(s["issues_blocked"]), "tools_refused": len(s["tools_refused"]),
                    "screen": {k: m.get(k) for k in ("detected", "malicious", "false_alarms", "benign")} if m else None}
    learning = {"lessons": 0, "waiting": 0, "approved": 0, "versions": {}, "failed_commands": {}}
    if lessons is not None:
        for repo in repos:
            try:
                learning["lessons"] += len(lessons.all(repo) or [])
            except Exception:                              # noqa: BLE001 — lessons unreadable: show 0, not an error
                pass
    if improvements is not None:
        learning["waiting"] = len(improvements.view(repos).get("proposals", []))
        by_repo: dict[str, list] = {}
        for t in improvements.trend(repos):
            if t.get("version") and t.get("runs"):
                by_repo.setdefault(t["repo"], []).append(t)
        for repo, rows in by_repo.items():
            if len(rows) >= 2:
                learning["failed_commands"][repo] = {"from": rows[0]["failed_commands"], "to": rows[-1]["failed_commands"],
                                                     "versions": [rows[0]["version"], rows[-1]["version"]]}
    try:
        learning["approved"] = conn.execute("select count(*) from repo_skill_proposals where status='approved'").fetchone()[0]
        for r in conn.execute("select repo, max(version) v from repo_skill_versions group by repo"):
            learning["versions"][r["repo"]] = r["v"]
    except sqlite3.OperationalError:
        pass
    return {"safety": safety_panel, "learning": learning}
