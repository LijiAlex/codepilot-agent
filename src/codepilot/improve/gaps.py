# src/codepilot/improve/gaps.py
"""Skill gap reports and the approval queue for repo-skill changes (Plus).
A report is built from the trace AND the outcome (never the trace alone); proposals gain confidence when the same change
is proposed again by later runs; nothing changes the repo skill until a person approves it on the dashboard
(defence against memory poisoning, OWASP ASI06). The brief's skills and memory tiers are never edited by this loop."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from codepilot.improve.repo_skill import RepoSkillStore
from codepilot.improve.trace import analyse_run, save
from codepilot.agents.models import structured

SCHEMA = """
create table if not exists skill_gap_reports(run_id text primary key, repo text not null, report_json text not null,
  created_at text default (datetime('now')));
create table if not exists repo_skill_proposals(id text primary key, repo text not null, key text not null, op text not null,
  section text not null, text text not null, step_id text, evidence text not null, hits integer not null default 1,
  status text not null default 'pending', first_run text, last_run text, created_at text default (datetime('now')));
create table if not exists trace_analyses(run_id text primary key, repo text not null, version integer not null,
  failed_commands integer not null, tool_calls integer not null, first_try_pass integer, minutes real, tokens integer);
"""

GAP_PROMPT = """You review how an AI coding agent worked in one repository, to improve that repository's skill: the short
list of steps a teammate would give a newcomer (setup, where things live, how to run tests, never touch, gotchas).
You get the current repo skill, a trace analysis of one run, and its outcome. Say which steps were followed, missing
(should exist), wrong (misled the agent) or unused. Propose at most 3 changes, each a delta: add a step, correct a step
(give its id), or retire a step (give its id). Each proposal needs concrete evidence from the trace (a command and its exit
code, a file read order, a test result). Propose nothing if the evidence is weak. Never propose anything about secrets,
credentials, network access or disabling checks."""


class Proposal(BaseModel):
    op: Literal["add", "correct", "retire"]
    section: Literal["setup", "locations", "tests", "never_touch", "gotchas"]
    text: str = Field(max_length=300)
    step_id: str | None = None
    evidence: str = Field(max_length=500)


class GapReport(BaseModel):
    """Skill gap report for one run."""
    followed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    wrong: list[str] = Field(default_factory=list)
    unused: list[str] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list, max_length=3)


_FORBIDDEN = re.compile(r"(?i)secret|credential|token|password|\.env|disable|skip (the )?(check|guard|test)|network|curl|wget|pip install")


def build_report(model, trace: dict, skill_text: str) -> GapReport:
    outcome = {k: trace[k] for k in ("outcome", "reason", "first_try_test_pass", "changed_files")}
    user = (f"<repo_skill>\n{skill_text or '(none yet)'}\n</repo_skill>\n\n<outcome>{json.dumps(outcome)}</outcome>\n\n"
            f"<trace>{json.dumps({k: v for k, v in trace.items() if k not in outcome})}</trace>")
    report = structured(model, GapReport).invoke([{"role": "system", "content": GAP_PROMPT}, {"role": "user", "content": user}])
    report.proposals = [p for p in report.proposals if not _FORBIDDEN.search(p.text)]     # code-side filter, not only the prompt
    return report


def _key(p: Proposal) -> str:
    words = " ".join(sorted(set(re.findall(r"[a-z0-9_./-]{3,}", p.text.lower())))[:200])
    return f"{p.op}|{p.section}|{p.step_id or ''}|{words}"


class ImprovementStore:
    def __init__(self, conn: sqlite3.Connection, skills: RepoSkillStore):
        self.conn, self.skills = conn, skills
        conn.executescript(SCHEMA)

    def record_trace(self, t: dict) -> None:
        self.conn.execute("insert or replace into trace_analyses values(?,?,?,?,?,?,?,?)",
                          (t["run_id"], t["repo"], t["repo_skill_version"], len(t["failed_commands"]), sum(t["tool_calls"].values()),
                           None if t["first_try_test_pass"] is None else int(t["first_try_test_pass"]), t["minutes"], t["tokens"]))

    def record_report(self, repo: str, run_id: str, report: GapReport) -> list[str]:
        self.conn.execute("insert or replace into skill_gap_reports(run_id, repo, report_json) values(?,?,?)",
                          (run_id, repo, report.model_dump_json()))
        ids = []
        for p in report.proposals:
            key = _key(p)
            row = self.conn.execute("select id, evidence from repo_skill_proposals where repo=? and key=? and status='pending'",
                                    (repo, key)).fetchone()
            if row:
                self.conn.execute("update repo_skill_proposals set hits=hits+1, last_run=?, evidence=? where id=?",
                                  (run_id, (row["evidence"] + f"\n[{run_id}] {p.evidence}")[-2000:], row["id"]))
                ids.append(row["id"])
            else:
                pid = uuid.uuid4().hex[:10]
                self.conn.execute("insert into repo_skill_proposals(id, repo, key, op, section, text, step_id, evidence, first_run, last_run) "
                                  "values(?,?,?,?,?,?,?,?,?,?)", (pid, repo, key, p.op, p.section, p.text, p.step_id,
                                                                   f"[{run_id}] {p.evidence}", run_id, run_id))
                ids.append(pid)
        return ids

    def decide(self, proposal_id: str, decision: str, by: str) -> dict:
        row = self.conn.execute("select * from repo_skill_proposals where id=? and status='pending'", (proposal_id,)).fetchone()
        status = "approved" if decision == "approve" else "rejected"
        claimed = self.conn.execute("update repo_skill_proposals set status=? where id=? and status='pending'", (status, proposal_id))
        if row is None or claimed.rowcount != 1:                 # compare-and-set: a second click finds it decided
            return {"status": "stale"}
        if decision != "approve":
            return {"status": "rejected"}
        try:
            v = self.skills.apply(row["repo"], row["op"], row["section"], row["text"], row["step_id"], row["last_run"])
        except Exception:
            self.conn.execute("update repo_skill_proposals set status='pending' where id=?", (proposal_id,))
            raise
        return {"status": "approved", "version": v}

    def skill_history(self, repo: str) -> list[dict]:
        return self.skills.history(repo)

    def trend(self, repos: list[str]) -> list[dict]:
        out = []
        for repo in repos:
            for r in self.conn.execute("select version, count(*) n, avg(failed_commands) fc, avg(tool_calls) tc, avg(first_try_pass) ftp "
                                       "from trace_analyses where repo=? group by version order by version", (repo,)):
                out.append({"repo": repo, "version": r["version"], "runs": r["n"], "failed_commands": round(r["fc"], 2),
                            "tool_calls": round(r["tc"], 1), "first_try_pass": None if r["ftp"] is None else round(r["ftp"], 2)})
        return out

    def view(self, repos: list[str]) -> dict:
        props = [dict(r) | {"confidence": round(min(1.0, r["hits"] / 3), 2)} for r in self.conn.execute(
            "select id, repo, op, section, text, step_id, evidence, hits from repo_skill_proposals where status='pending' order by hits desc")]
        for p in props:
            p["change"] = f"{p['op']} {p['section']}" + (f" {p['step_id']}" if p["step_id"] else "") + f": {p['text']}"
        reports = [dict(r) for r in self.conn.execute("select run_id, repo, report_json, created_at from skill_gap_reports order by created_at desc limit 20")]
        return {"proposals": props, "reports": reports, "trend": self.trend(repos)}


class Improver:
    """Runs on the worker thread after every run (it is the single writer for repo-skill data)."""

    def __init__(self, conn, runs_dir: Path, store: ImprovementStore, model_factory, audit):
        self.conn, self.runs_dir, self.store, self.model_factory, self.audit = conn, Path(runs_dir), store, model_factory, audit

    def after_run(self, run_id: str) -> dict | None:
        trace = analyse_run(self.conn, self.runs_dir, run_id)
        if not trace["task_type"]:
            return None                                      # never got past triage: nothing to learn about the repo
        save(self.runs_dir, trace)
        self.store.record_trace(trace)
        _, skill_text = self.store.skills.render(trace["repo"])
        report = build_report(self.model_factory(), trace, skill_text)
        (self.runs_dir / run_id / "skill_gap_report.json").write_text(report.model_dump_json(indent=1))
        ids = self.store.record_report(trace["repo"], run_id, report)
        self.audit.log("improver", "skill_gap_report", run_id=run_id, detail={"proposals": ids})
        return {"trace": trace, "report": report.model_dump(), "proposals": ids}
