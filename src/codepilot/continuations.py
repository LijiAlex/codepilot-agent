# src/codepilot/continuations.py
"""Decisions on handoffs (TUI approve / reject / inspect / skip) and the continuation runs they start.
A continuation never resumes the old run: the waiting run is closed and a new run is claimed with a Seed."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from codepilot.agents.triage import ComplexPlan, plan_as_todos
from codepilot.gh.reader import IssueSnapshot
from codepilot.publish import PublishOutcome, PublishServices, publish, recheck_issue
from codepilot.states import ACTIVE, IN_FLIGHT, RunState
from codepilot.worker import Seed, WorkItem


@dataclass
class ContinuationServices:
    publish: PublishServices
    repo_config: Callable[[str], Any]           # "owner/name" → RepoConfig
    lessons: Any = None
    cancel_run: Callable[[str], None] = lambda run_id: None


@dataclass
class Continuation:
    item: WorkItem
    run_id: str
    seed: Seed


@dataclass
class DecisionResult:
    status: str                                 # published | closed | continued | skipped | stale | failed | waiting
    message: str
    continuation: Continuation | None = None
    publish: PublishOutcome | None = None


def _item(cs: ContinuationServices, repo: str, payload: dict) -> WorkItem:
    cfg = cs.repo_config(repo)
    if payload.get("issue"):
        return WorkItem(repo=cfg, issue=IssueSnapshot(**payload["issue"]))
    return WorkItem(repo=cfg, task=payload["task"])


def _restart(cs: ContinuationServices, h, payload: dict, seed: Seed) -> Continuation | None:
    runs = cs.publish.runs
    runs.set_state(h["run_id"], RunState.CLOSED, "continued")
    item = _item(cs, h["repo"], payload)
    new = runs.claim(h["repo"], "issue" if item.issue else "task", item.issue.number if item.issue else None,
                     item.title, None if item.issue else item.task["id"])
    if new is None:                                       # the poller claimed it in between: that run carries on the work
        cs.publish.audit.log("human", "continuation_superseded", run_id=h["run_id"], target=h["issue_key"])
        return None
    cs.publish.audit.log("human", "continuation_started", run_id=new, target=h["issue_key"], detail={"from": h["run_id"]})
    return Continuation(item, new, seed)


def decide(cs: ContinuationServices, handoff_id: str, decision: str, by: str, reason: str = "") -> DecisionResult:
    ps = cs.publish
    h = ps.handoffs.get(handoff_id)
    if h is None or h["status"] != "pending" or not ps.handoffs.decide(handoff_id, decision, by, reason):
        return DecisionResult("stale", "this item was already decided or expired")
    try:
        return _apply(cs, h, handoff_id, decision, by, reason)
    except Exception as e:                               # noqa: BLE001 — network/token errors must not strand the run
        run = ps.runs.get(h["run_id"])
        if run is not None and run["state"] == "AWAITING_APPROVAL":
            ps.handoffs.mark(handoff_id, "pending")      # put it back in the TUI; the person can try again
        ps.audit.log("worker", "decision_error", run_id=h["run_id"], result="error", detail={"type": type(e).__name__})
        return DecisionResult("error", f"{type(e).__name__}: {str(e)[:200]} — the item is back in the approval list")


def _apply(cs: ContinuationServices, h, handoff_id: str, decision: str, by: str, reason: str) -> DecisionResult:
    ps = cs.publish
    ps.audit.log(f"human:{by}", f"handoff_{decision}", run_id=h["run_id"], target=h["entry_point"], detail={"reason": reason})
    payload, entry = ps.handoffs.payload(handoff_id), h["entry_point"]

    if entry in ("publish", "publish_failed"):
        if decision == "approve":
            out = publish(ps, handoff_id)
            status = {"PR_OPENED": "published", "CLOSED": "stale", "FAILED": "failed", "AWAITING_APPROVAL": "waiting"}[out.state]
            return DecisionResult(status, out.pr_url or out.reason or "", publish=out)
        ps.runs.set_state(h["run_id"], RunState.CLOSED, "rejected")
        if cs.lessons is not None and reason:
            cs.lessons.add(h["repo"], payload["triage"]["task_type"], f"A person rejected a change: {reason}", "rejection", h["run_id"])
        return DecisionResult("closed", "rejected")

    stale = recheck_issue(ps.repo_factory(h["repo"]), payload)          # every continuation re-checks first (spec §5)
    if stale:
        ps.handoffs.mark(handoff_id, "expired")
        ps.runs.set_state(h["run_id"], RunState.CLOSED, f"stale: {stale}")
        return DecisionResult("stale", stale)

    common = dict(base_sha=payload.get("base_sha"), triage=payload.get("triage"), saved=payload.get("saved", []))
    if entry == "continue_fix":
        if decision == "reject":                         # spec §10: reject → FAILED, lesson
            ps.runs.set_state(h["run_id"], RunState.FAILED, "rejected")
            if cs.lessons is not None:
                cs.lessons.add(h["repo"], payload["triage"]["task_type"],
                               f"A retry was declined after two failed test runs{': ' + reason if reason else ''}", "failure", h["run_id"])
            return DecisionResult("failed", "rejected")
        report = payload.get("test_report") or {}
        notes = (f"Attempt 3 of 3. The previous attempt failed its tests twice. Failing tests: {report.get('failing_ids', [])}.\n"
                 f"Output:\n{(report.get('output') or '')[-3000:]}")
        c = _restart(cs, h, payload, Seed(**common, prior_failures=payload.get("prior_failures", 2), notes=notes))
        return DecisionResult("continued" if c else "superseded", "third attempt started" if c else "another run already took the issue", c)

    if entry == "continue_with_decision":
        cmd = payload["gate"]["command"]
        if decision == "approve":
            seed = Seed(**common, prior_failures=payload.get("prior_failures", 0), approved_command=cmd)
        else:
            seed = Seed(**common, prior_failures=payload.get("prior_failures", 0),
                        notes=f"A person refused the command `{cmd}`. Continue the task without it.")
        c = _restart(cs, h, payload, seed)
        return DecisionResult("continued" if c else "superseded",
                              ("continuing " + ("with" if decision == "approve" else "without") + f" `{cmd}`") if c else "another run already took the issue", c)

    if entry == "continue_complex":
        if decision == "reject":
            ps.runs.set_state(h["run_id"], RunState.SKIPPED, "complex")
            return DecisionResult("skipped", "complex issue skipped")
        plan = ComplexPlan(**payload["plan"])
        seed = Seed(base_sha=None, triage=payload["triage"], plan_text=plan_as_todos(plan), plan_approved=True,
                    time_cap_minutes=max(10, int(plan.est_minutes * 1.5)))
        c = _restart(cs, h, payload, seed)
        return DecisionResult("continued" if c else "superseded", "approved plan started" if c else "another run already took the issue", c)

    raise ValueError(f"unknown entry point {entry}")


def describe(handoffs, handoff_id: str) -> str:
    """Text for the TUI inspect screen."""
    h, p = handoffs.get(handoff_id), handoffs.payload(handoff_id)
    head = f"{h['repo']} {h['issue_key']} — {h['entry_point']}\n"
    if h["entry_point"] in ("publish", "publish_failed"):
        flags = ", ".join(p.get("flags", [])) or "none"
        rep = p.get("test_report") or {}
        title = (p.get("issue") or {}).get("title") or (p.get("task") or {}).get("title", "")
        return (head + f"PR: [CodePilot] {title}\nFiles: {', '.join(p.get('files', []))}\nFlags: {flags}\n"
                f"Tests: {rep.get('passed', 0)} passed, {rep.get('failed', 0)} failed\n"
                + (f"Last error: {p['error']}\n" if p.get("error") else "") + "\n" + p.get("diff", ""))
    if h["entry_point"] == "continue_fix":
        rep = p.get("test_report") or {}
        return head + f"Failing tests: {rep.get('failing_ids', [])}\n\n{rep.get('output', '')}\n\nChange so far:\n{p.get('diff', '')}"
    if h["entry_point"] == "continue_with_decision":
        g = p["gate"]
        todos = "\n".join(f"  [{'x' if t.get('status') == 'completed' else ' '}] {t.get('content', '')}" for t in p.get("todos", []))
        return head + (f"Command: {g['command']}\nAsked by: {g['agent']}\nWhy it stopped: {g['reason']}\n"
                       f"Plan so far:\n{todos or '  (none)'}\n\nChange so far:\n{p.get('diff', '')}")
    if h["entry_point"] == "continue_complex":
        plan = p["plan"]
        return head + json.dumps(plan, indent=2)
    return head + json.dumps(p, indent=2)[:4000]


def skip(cs: ContinuationServices, repo: str, issue_key: str, by: str) -> str:
    """[s] (E12): stop an in-flight run, or expire a pending handoff and skip the issue."""
    ps = cs.publish
    row = ps.runs.conn.execute(
        f"select * from runs where repo=? and issue_key=? and state in ({','.join('?' * len(ACTIVE))})",
        (repo, issue_key, *ACTIVE)).fetchone()
    if row is None:
        return "nothing active for this issue"
    if RunState(row["state"]) in IN_FLIGHT:
        cs.cancel_run(row["id"])
        ps.audit.log(f"human:{by}", "skip_requested", run_id=row["id"], target=issue_key)
        return "stopping the run"
    if RunState(row["state"]) == RunState.AWAITING_APPROVAL:
        ps.handoffs.expire_for_issue(repo, issue_key, f"skipped by {by}")
        ps.runs.set_state(row["id"], RunState.SKIPPED, "by_user")
        ps.audit.log(f"human:{by}", "skipped", run_id=row["id"], target=issue_key)
        return "skipped"
    return "a PR is already open; close it on GitHub"
