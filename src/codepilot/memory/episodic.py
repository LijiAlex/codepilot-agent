# src/codepilot/memory/episodic.py
"""Episodic memory in the LangGraph SqliteStore: one record per finished run, and
session summaries (written as the session goes, not only on quit — spec E11).
SqliteStore orders by updated_at with 1-second resolution (verified), so every
value carries a nanosecond `ts` and we sort ourselves."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from langgraph.store.sqlite import SqliteStore

NOT_A_FAILURE = {"crashed", "sandbox_lost"}     # E11: a crash or a lost box says nothing about the issue
INFRA_PREFIXES = ("setup:", "provider:")       # the sandbox could not be prepared, or the model provider refused the account: not the issue's fault


class EpisodicMemory:
    def __init__(self, path: Path):
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.execute("pragma busy_timeout=5000")
        self.store = SqliteStore(conn)
        self.store.setup()
        self._lock = threading.Lock()

    def record_run(self, repo: str, run_id: str, issue_key: str, outcome: str, reason: str | None, summary: str,
                   task_type: str | None = None, files: list[str] | None = None, duration_s: float | None = None) -> None:
        """Brief: issue id, task type, files modified, outcome, duration. A later record for the same run (e.g. the
        watcher's DONE after the worker's AWAITING_APPROVAL) keeps earlier fields it does not supply."""
        with self._lock:
            ns = ("episodes", repo.replace("/", "__"))
            old = self.store.get(ns, run_id)
            prev = old.value if old else {}
            task_type = task_type or prev.get("task_type")
            files = files or prev.get("files")
            duration_s = duration_s if duration_s is not None else prev.get("duration_s")
            self.store.put(ns, run_id,
                           {"ts": time.time_ns(), "issue_key": issue_key, "outcome": outcome, "reason": reason, "summary": summary,
                            "task_type": task_type, "files": files or [], "duration_s": duration_s})

    def episodes(self, repo: str, limit: int = 200) -> list[dict]:
        items = self.store.search(("episodes", repo.replace("/", "__")), limit=limit)
        return sorted((dict(i.value, run_id=i.key) for i in items), key=lambda v: v["ts"], reverse=True)

    def save_session(self, session_id: str, summary: str, stats: dict) -> None:
        with self._lock:
            old = self.store.get(("sessions",), session_id)
            started = old.value["started"] if old else time.time_ns()
            self.store.put(("sessions",), session_id, {"ts": time.time_ns(), "started": started, "summary": summary, "stats": stats})

    def session_episodes(self, repos: list[str], since_ns: int) -> list[dict]:
        """This session's runs, oldest first: the structured part of the session summary."""
        rows = [dict(e, repo=r) for r in repos for e in self.episodes(r) if e["ts"] >= since_ns]
        return sorted(rows, key=lambda e: e["ts"])

    def last_sessions(self, n: int = 3) -> list[dict]:
        items = self.store.search(("sessions",), limit=1000)
        return sorted((dict(i.value, session_id=i.key) for i in items), key=lambda v: v["started"], reverse=True)[:n]

    def recently_failed(self, repo: str, n_sessions: int = 3) -> set[str]:
        """Issues that failed in the last n sessions (brief: avoid retrying recently failed issues)."""
        # sessions that ran nothing (e.g. a TUI opened and closed) do not count: restarting must not make a retry happen sooner
        sessions = [x for x in self.last_sessions(1000) if (x.get("summary") or "no runs") != "no runs"][:n_sessions]
        if not sessions:
            return set()
        since = min(s["started"] for s in sessions)
        return {e["issue_key"] for e in self.episodes(repo)
                if e["ts"] >= since and e["outcome"] == "FAILED" and e["reason"] not in NOT_A_FAILURE
                and not str(e["reason"] or "").startswith(INFRA_PREFIXES)}
