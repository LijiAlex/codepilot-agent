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


def build_report(model, trace: dict, skill_text: str, config: dict | None = None) -> GapReport:
    outcome = {k: trace[k] for k in ("outcome", "reason", "first_try_test_pass", "changed_files")}
    user = (f"<repo_skill>\n{skill_text or '(none yet)'}\n</repo_skill>\n\n<outcome>{json.dumps(outcome)}</outcome>\n\n"
            f"<trace>{json.dumps({k: v for k, v in trace.items() if k not in outcome})}</trace>")
    report = structured(model, GapReport).invoke([{"role": "system", "content": GAP_PROMPT}, {"role": "user", "content": user}],
                                                  config=config)
    report.proposals = [p for p in report.proposals if not _FORBIDDEN.search(p.text)]     # code-side filter, not only the prompt
    return report


_FILE = re.compile(r"[\w./-]+\.(?:py|toml|ya?ml|json|cfg|ini|md|txt|lock)\b")
_RUN = re.compile(r"(?m)^\[([^\]\s]+)\]")                 # evidence lines start with "[run_id] "
VERY_SIMILAR, SIMILAR = 0.80, 0.60         # calibrated on real proposals (2026-10-19): duplicates 0.64-0.85, unrelated 0.66


def anchors(text: str) -> set[str]:
    """Concrete things a proposal is about: file names and the commands the sandbox refuses."""
    low = text.lower()
    found = {f.rsplit("/", 1)[-1] for f in _FILE.findall(low)}
    found |= {c for c in ("python -c", "pip") if re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", low)}
    return found


def wilson_lower_bound(seen: int, runs: int, z: float = 1.28) -> float:
    """Lower bound of the Wilson score interval (80%): high only when a change keeps showing up across enough runs."""
    if runs <= 0 or seen <= 0:
        return 0.0
    p = min(seen, runs) / runs
    centre = p + z * z / (2 * runs)
    spread = z * ((p * (1 - p) + z * z / (4 * runs)) / runs) ** 0.5
    return max(0.0, (centre - spread) / (1 + z * z / runs))


def _cosine(a, b) -> float:
    na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _key(p: Proposal) -> str:
    words = " ".join(sorted(set(re.findall(r"[a-z0-9_./-]{3,}", p.text.lower())))[:200])
    return f"{p.op}|{p.section}|{p.step_id or ''}|{words}"


class ImprovementStore:
    def __init__(self, conn: sqlite3.Connection, skills: RepoSkillStore, embed=None):
        """embed: texts -> vectors (the local MiniLM model). Without it only exact rewordings with shared files merge."""
        self.conn, self.skills, self.embed = conn, skills, embed
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
                                    (repo, key)).fetchone() or self._similar(repo, p)
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

    def _same(self, a_text: str, b_text: str, cos: float | None) -> bool:
        shared = anchors(a_text) & anchors(b_text)
        if cos is None:                                   # no embedder: only near-identical wording about the same file
            wa, wb = set(re.findall(r"[a-z]{4,}", a_text.lower())), set(re.findall(r"[a-z]{4,}", b_text.lower()))
            return bool(shared) and len(wa & wb) / max(1, len(wa | wb)) >= 0.5
        return cos >= VERY_SIMILAR or (cos >= SIMILAR and bool(shared))

    def _similar(self, repo: str, p: Proposal):
        rows = list(self.conn.execute("select id, text, evidence from repo_skill_proposals where repo=? and op=? and section=? "
                                      "and coalesce(step_id,'')=? and status='pending'", (repo, p.op, p.section, p.step_id or "")))
        if not rows:
            return None
        vecs = self.embed([p.text] + [r["text"] for r in rows]) if self.embed else None
        for i, r in enumerate(rows):
            if self._same(p.text, r["text"], _cosine(vecs[0], vecs[i + 1]) if vecs else None):
                return r
        return None

    def consolidate(self, repo: str) -> int:
        """Merge pending proposals that are rewordings of each other (older ones keep the text, gain the evidence)."""
        rows = list(self.conn.execute("select id, op, section, coalesce(step_id,'') sid, text, evidence from repo_skill_proposals "
                                      "where repo=? and status='pending' order by rowid", (repo,)))
        vecs = self.embed([r["text"] for r in rows]) if self.embed and len(rows) > 1 else None
        merged, gone = 0, set()
        for i, keep in enumerate(rows):
            if keep["id"] in gone:
                continue
            evidence = keep["evidence"]
            for j in range(i + 1, len(rows)):
                other = rows[j]
                if other["id"] in gone or (other["op"], other["section"], other["sid"]) != (keep["op"], keep["section"], keep["sid"]):
                    continue
                if self._same(keep["text"], other["text"], _cosine(vecs[i], vecs[j]) if vecs else None):
                    evidence = (evidence + "\n" + other["evidence"])[-4000:]
                    self.conn.execute("update repo_skill_proposals set status='merged' where id=? and status='pending'", (other["id"],))
                    gone.add(other["id"]); merged += 1
            if evidence != keep["evidence"]:
                self.conn.execute("update repo_skill_proposals set evidence=? where id=?", (evidence, keep["id"]))
        return merged

    def _support(self, repo: str, evidence: str, first_run: str | None) -> tuple[int, int]:
        """(distinct runs that proposed it, runs analysed on this repo since it first appeared)."""
        seen = set(_RUN.findall(evidence or ""))
        row = self.conn.execute("select rowid from runs where id=?", (first_run,)).fetchone() if first_run else None
        runs = self.conn.execute("select count(*) from runs where repo=? and task_type is not null and rowid>=?",
                                 (repo, row[0] if row else 0)).fetchone()[0]
        return len(seen) or 1, max(runs, len(seen) or 1)

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
        """Per repo-skill version. Version 0 = runs that ended (e.g. in sandbox setup) before any repo skill was loaded;
        versions no run has used yet are listed with 0 runs, so every version on the Skills page appears here."""
        out = []
        for repo in repos:
            rows = {}
            for r in self.conn.execute("select version, count(*) n, avg(failed_commands) fc, avg(tool_calls) tc, avg(first_try_pass) ftp "
                                       "from trace_analyses where repo=? group by version", (repo,)):
                rows[r["version"]] = {"repo": repo, "version": r["version"], "runs": r["n"], "failed_commands": round(r["fc"], 2),
                                      "tool_calls": round(r["tc"], 1), "first_try_pass": None if r["ftp"] is None else round(r["ftp"], 2)}
            for v in self.skills.history(repo):
                rows.setdefault(v["version"], {"repo": repo, "version": v["version"], "runs": 0, "failed_commands": None,
                                               "tool_calls": None, "first_try_pass": None})
            out.extend(rows[v] for v in sorted(rows))
        return out

    def view(self, repos: list[str]) -> dict:
        for repo in {r["repo"] for r in self.conn.execute("select distinct repo from repo_skill_proposals where status='pending'")}:
            self.consolidate(repo)
        props = []
        for r in self.conn.execute("select id, repo, op, section, text, step_id, evidence, hits, first_run from repo_skill_proposals "
                                   "where status='pending'"):
            seen, runs = self._support(r["repo"], r["evidence"], r["first_run"])
            props.append(dict(r) | {"seen": seen, "runs": runs, "confidence": round(wilson_lower_bound(seen, runs), 2)})
        props.sort(key=lambda p: (-p["confidence"], -p["seen"]))
        for p in props:
            p["change"] = f"{p['op']} {p['section']}" + (f" {p['step_id']}" if p["step_id"] else "") + f": {p['text']}"
        reports = [dict(r) for r in self.conn.execute("select run_id, repo, report_json, created_at from skill_gap_reports order by created_at desc limit 20")]
        return {"proposals": props, "reports": reports, "trend": self.trend(repos)}


class Improver:
    """Runs on the worker thread after every run (it is the single writer for repo-skill data)."""

    def __init__(self, conn, runs_dir: Path, store: ImprovementStore, model_factory, audit, tracer=None):
        self.conn, self.runs_dir, self.store, self.model_factory, self.audit = conn, Path(runs_dir), store, model_factory, audit
        self.tracer = tracer                                  # LangSmith: self-improvement shows as its own cost line

    def after_run(self, run_id: str) -> dict | None:
        trace = analyse_run(self.conn, self.runs_dir, run_id)
        if not trace["task_type"]:
            return None                                      # never got past triage: nothing to learn about the repo
        save(self.runs_dir, trace)
        self.store.record_trace(trace)
        _, skill_text = self.store.skills.render(trace["repo"])
        report = build_report(self.model_factory(), trace, skill_text,
                              config={"callbacks": [self.tracer] if self.tracer else [], "run_name": "skill-gap-report",
                                      "metadata": {"kind": "skill-gap-report", "for_run": run_id, "repo": trace["repo"]}})
        (self.runs_dir / run_id / "skill_gap_report.json").write_text(report.model_dump_json(indent=1))
        ids = self.store.record_report(trace["repo"], run_id, report)
        self.audit.log("improver", "skill_gap_report", run_id=run_id, detail={"proposals": ids})
        return {"trace": trace, "report": report.model_dump(), "proposals": ids}


def lazy_embedder():
    """The local MiniLM embedder, loaded on first use (keeps startup fast and offline until a proposal needs it)."""
    holder = {}

    def embed(texts):
        if "f" not in holder:
            from codepilot.memory.semantic import default_embedder
            holder["f"] = default_embedder()
        return holder["f"](texts)
    return embed
