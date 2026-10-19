# src/codepilot/engine.py
"""Wires the pieces together and runs them on background threads, so the TUI (main thread) never blocks:
- worker thread: one run at a time from a queue (issues, free-form tasks, continuations);
- decision thread: approvals/rejections → publish or a continuation queued for the worker;
- poll thread: poller + PR watcher every poll_interval_s (and on demand)."""
from __future__ import annotations

import json
import queue
import time
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codepilot.continuations import ContinuationServices, decide, describe, skip
from codepilot.poller import Poller
from codepilot.watcher import PRWatcher
from codepilot.worker import Outcome, Seed, WorkItem, Worker


def session_note(sessions: list) -> str:
    """One short line for the startup notice: skip empty sessions, first line of each summary only."""
    parts = []
    for sess in sessions:
        text = " ".join((sess["summary"] or "").split())
        if text and text != "no runs":
            parts.append(text[:90])
    return ("last sessions: " + " | ".join(parts))[:200] if parts else ""


@dataclass
class Job:
    item: WorkItem
    run_id: str
    seed: Seed | None = None
    sandbox_kind: str | None = None


@dataclass
class UiCallbacks:
    on_event: Callable[[Any], None] = lambda e: None
    on_notice: Callable[[str], None] = lambda text: None
    on_change: Callable[[], None] = lambda: None


class Engine:
    def __init__(self, settings, worker: Worker, poller: Poller, watcher: PRWatcher, cs: ContinuationServices, episodic,
                 ui: UiCallbacks, selfcheck: Callable[[], list[str]] = lambda: [], improver=None):
        self.settings, self.worker, self.poller, self.watcher, self.cs = settings, worker, poller, watcher, cs
        self.improver = improver
        self.episodic, self.ui, self.selfcheck = episodic, ui, selfcheck
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_ns = time.time_ns()
        self.jobs: queue.Queue[Job | None] = queue.Queue()
        self.decisions: queue.Queue[tuple | None] = queue.Queue()
        self.stop_event, self.poll_now = threading.Event(), threading.Event()
        self.current_run: str | None = None
        self.last_poll: dict[str, str] = {}             # issue → why it was skipped in the last poll
        self.polled_at: float | None = None             # epoch seconds of the last finished poll (TUI title)
        self.stats: Counter[str] = Counter()
        self.threads: list[threading.Thread] = []
        worker.s.on_run_end = self._on_run_end
        worker.s.on_event = ui.on_event

    # ---- lifecycle ---------------------------------------------------------
    def startup(self) -> list[str]:
        problems = self.selfcheck()
        if problems:
            raise RuntimeError("agent tool sets differ from the design: " + "; ".join(problems))
        crashed = self.worker.recover()
        if crashed:
            self.ui.on_notice(f"{len(crashed)} run(s) were interrupted by the last shutdown and marked FAILED(crashed)")
        self.watcher.check()                                   # merges made while we were off (E17)
        last = self.episodic.last_sessions(3)
        if last:
            note = session_note(last)
            if note:
                self.ui.on_notice(note)
        return crashed

    def start(self, poll_loop: bool = True) -> None:
        """poll_loop=False for headless `once`, which polls exactly once on the calling thread."""
        loops = (self._worker_loop, self._decision_loop) + ((self._poll_loop,) if poll_loop else ())
        for target in loops:
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self.threads.append(t)

    def shutdown(self) -> None:
        self.stop_event.set()
        self.poll_now.set()
        self.jobs.put(None)
        self.decisions.put(None)
        for t in self.threads:                                 # let a publish in progress finish (startup reopens it if not)
            if t.name == "_decision_loop":
                t.join(timeout=30)
        self._save_session()

    # ---- actions from the TUI ----------------------------------------------
    def submit_task(self, repo_full_name: str, title: str, body: str) -> str | None:
        cfg = next(r for r in self.settings.repos if r.full_name == repo_full_name)
        task = {"id": uuid.uuid4().hex[:8], "title": title.strip()[:120], "body": body.strip()}
        rid = self.worker.s.runs.claim(cfg.full_name, "task", None, task["title"], task["id"])
        if rid:
            self.worker.s.audit.log("human", "task_submitted", run_id=rid, target=task["title"])
            self.jobs.put(Job(WorkItem(repo=cfg, task=task), rid))
            self.ui.on_change()
        return rid

    def decide(self, handoff_id: str, decision: str, by: str, reason: str = "") -> None:
        self.decisions.put((handoff_id, decision, by, reason))

    def inspect(self, handoff_id: str) -> str:
        return describe(self.cs.publish.handoffs, handoff_id)

    def skip(self, repo: str, issue_key: str, by: str) -> str:
        msg = skip(self.cs, repo, issue_key, by)
        self.ui.on_change()
        return msg

    # ---- loops ---------------------------------------------------------------
    def run_job(self, job: Job) -> Outcome:
        self.current_run = job.run_id
        self.ui.on_change()
        try:
            out = self.worker.run(job.item, job.run_id, job.seed, job.sandbox_kind)
        finally:
            self.current_run = None
        if self.improver is not None:                                   # Plus: trace analysis + skill gap report
            try:
                self.improver.after_run(out.run_id)
            except Exception as e:                                      # noqa: BLE001 — never blocks the next run
                self.ui.on_notice(f"skill gap report skipped: {type(e).__name__}")
        if out.reclaim_local and job.sandbox_kind != "local":       # E4: one automatic re-claim on the local backend
            it = job.item
            rid = self.worker.s.runs.claim(it.repo.full_name, "issue" if it.issue else "task",
                                           it.issue.number if it.issue else None, it.title, None if it.issue else it.task["id"])
            if rid:
                self.ui.on_notice(f"{it.repo.full_name} {it.key}: remote sandbox lost — retrying on the local sandbox")
                self.jobs.put(Job(it, rid, job.seed, "local"))
        self.ui.on_change()
        return out

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            job = self.jobs.get()
            try:
                if job is None:
                    return
                self.run_job(job)
            except Exception as e:                       # noqa: BLE001 — a crash in one job must not stop the queue
                self.ui.on_notice(f"run {job.run_id} crashed in the worker: {type(e).__name__}: {str(e)[:160]}")
            finally:
                self.jobs.task_done()

    def wait_idle(self, timeout: float) -> bool:
        """True once every queued job has finished (used by tests and `codepilot run --once`)."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.jobs.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return False

    def handle_decision(self, handoff_id: str, decision: str, by: str, reason: str = ""):
        res = decide(self.cs, handoff_id, decision, by, reason)
        if res.continuation is not None:
            c = res.continuation
            self.jobs.put(Job(c.item, c.run_id, c.seed))
        if res.publish is not None and res.publish.notes:
            for n in res.publish.notes:
                self.ui.on_notice(n)
        self.ui.on_notice(f"{decision}: {res.status} — {res.message}")
        self.ui.on_change()
        return res

    def _decision_loop(self) -> None:
        while not self.stop_event.is_set():
            d = self.decisions.get()
            if d is None:
                return
            try:
                self.handle_decision(*d)
            except Exception as e:                              # noqa: BLE001 — keep the loop alive, tell the person
                self.ui.on_notice(f"decision failed: {type(e).__name__}: {e}")

    def poll_once(self) -> None:
        res = self.poller.poll()
        self.last_poll = dict(res.skipped)
        self.polled_at = time.time()
        for item, rid in res.claimed:
            self.jobs.put(Job(item, rid))
        self.watcher.check()
        self.ui.on_change()

    def _poll_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:                              # noqa: BLE001 — GitHub down: try again next round
                self.ui.on_notice(f"poll failed: {type(e).__name__}")
            self.poll_now.wait(self.settings.poll_interval_s)
            self.poll_now.clear()

    # ---- read model for the TUI ----------------------------------------------
    def run_log(self, run_id: str, limit: int = 200) -> list[dict]:
        """The last lines of a run's recorded events (runs done elsewhere, e.g. by `codepilot once`)."""
        f = Path(self.settings.home) / "runs" / run_id / "events.jsonl"
        if not f.exists():
            return []
        out = []
        for line in f.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") != "token":
                out.append(e)
        return out[-limit:]

    def view(self) -> dict:
        runs = self.worker.s.runs
        cur = runs.get(self.current_run) if self.current_run else None
        pending = []
        for h in self.cs.publish.handoffs.pending():
            row = dict(h)
            payload = json.loads(row.pop("payload_json") or "{}")
            row["flags"] = payload.get("flags", [])
            row["file_count"] = len(payload.get("files", []))
            pending.append(row)
        return {"runs": [dict(r) for r in runs.recent(40)],
                "pending": pending,
                "current": dict(cur) if cur else None, "skipped": dict(self.last_poll),
                "repos": [r.full_name for r in self.settings.repos], "polled_at": self.polled_at,
                "poll_interval_s": self.settings.poll_interval_s, "map_budget": self.settings.repo_map_budget_tokens}

    # ---- session memory ----------------------------------------------------
    def _on_run_end(self, out: Outcome) -> None:
        reason = (out.reason or "").splitlines()[0][:40] if out.reason else ""
        self.stats[out.state if not reason else f"{out.state}({reason})"] += 1
        self._save_session()

    def _save_session(self) -> None:
        """Brief: a structured session summary — one row per run (issue, type, files, outcome, duration) plus counts."""
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(self.stats.items())) or "no runs"
        runs = self.episodic.session_episodes([r.full_name for r in self.settings.repos], self.session_start_ns)
        self.episodic.save_session(self.session_id, summary, {"counts": dict(self.stats), "runs": [
            {k: e.get(k) for k in ("issue_key", "task_type", "files", "outcome", "reason", "duration_s")} for e in runs]})
