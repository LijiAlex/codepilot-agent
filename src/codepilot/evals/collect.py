"""Gather what the checks and the judge look at: runs and audit rows (SQLite), each run's event log, and each PR on GitHub.

Read-only everywhere: nothing here writes to GitHub, the database or a run folder.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codepilot.evals.heuristics import PR, FileDiff, RunFacts
from codepilot.evals.judge import pr_judgement

ACTIVE = {"TRIAGED", "EXPLORING", "IMPLEMENTING", "TESTING", "AWAITING_APPROVAL", "PR_OPENED"}


def _events(runs_dir: Path, run_id: str) -> list[dict]:
    p = runs_dir / run_id / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _publish(conn, run_id: str) -> tuple[bool, list[str]]:
    row = conn.execute("select payload_json from handoffs where run_id=? and entry_point='publish' order by created_at desc limit 1",
                       (run_id,)).fetchone()
    if not row:
        return False, []
    try:
        return True, list(json.loads(row["payload_json"]).get("flags") or [])
    except (json.JSONDecodeError, TypeError):
        return True, []


def fetch_pr(repo, number: int, reviewer_login: str | None) -> tuple[PR, dict | None]:
    pr = repo.get_pull(number)
    facts = PR(number=number, branch=pr.head.ref, body=pr.body or "", labels=[label.name for label in pr.labels],
               commits=[c.commit.message for c in pr.get_commits()],
               files=[FileDiff(f.filename, f.additions or 0, f.deletions or 0, f.patch or "") for f in pr.get_files()])
    judged = pr_judgement(list(pr.get_reviews()), reviewer_login) if reviewer_login else None
    return facts, judged


def run_facts(conn: sqlite3.Connection, runs_dir: Path, golden_types: dict[tuple[str, str], str],
              repo_factory=None, reviewer_login: str | None = None) -> tuple[list[RunFacts], list[dict]]:
    """Every run as RunFacts, plus the judge's reading of each CodePilot+ PR. Runs that never got going (no task type:
    the claim or the sandbox failed before triage) are skipped — there is nothing of the agent's to check."""
    conn.row_factory = sqlite3.Row
    facts, judged = [], []
    repos = {}
    for r in conn.execute("select * from runs where task_type is not null order by created_at"):
        refused = [x["target"] or "" for x in conn.execute(
            "select target from events where run_id=? and action='command_refused' order by id", (r["id"],))]
        published, flags = _publish(conn, r["id"])
        pr = None
        if r["pr_number"] and repo_factory is not None:
            repo = repos.get(r["repo"]) or repos.setdefault(r["repo"], repo_factory(r["repo"]))
            pr, j = fetch_pr(repo, r["pr_number"], reviewer_login)
            if j:
                judged.append({"repo": r["repo"], "pr": r["pr_number"], "run_id": r["id"], **j})
        facts.append(RunFacts(run_id=r["id"], repo=r["repo"], issue_key=r["issue_key"], issue_number=r["issue_number"],
                              task_type=r["task_type"], state=r["state"], reason=r["reason"], events=_events(runs_dir, r["id"]),
                              refused=refused, published=published, publish_flags=flags, pr=pr,
                              golden_type=golden_types.get((r["repo"], r["issue_key"]))))
    return facts, judged


def golden_ended(conn: sqlite3.Connection, repo: str, rows: list[dict]) -> list[dict]:
    """Mark each golden row ended: its outcome was reached, or its latest run is no longer in flight."""
    conn.row_factory = sqlite3.Row
    for g in rows:
        if g.get("issue") is None:
            g["ended"] = False
            continue
        last = conn.execute("select state from runs where repo=? and issue_key=? order by created_at desc limit 1",
                            (repo, f"#{g['issue']}")).fetchone()
        g["ended"] = bool(g["met"]) or (last is not None and last["state"] not in ACTIVE)
    return rows


def guardrail_summary(path: Path) -> dict | None:
    if not path.exists():
        return None
    rows = json.loads(path.read_text()).get("rows", [])
    mal = [r for r in rows if r["label"] == "malicious"]
    ben = [r for r in rows if r["label"] == "benign"]
    return {"malicious": len(mal), "malicious_blocked": sum(r["blocked"] for r in mal),
            "benign": len(ben), "benign_blocked": sum(r["blocked"] for r in ben),
            "failed_closed": sum(r.get("failed_closed", False) for r in rows)}
