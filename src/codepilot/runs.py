# src/codepilot/runs.py
from __future__ import annotations

import sqlite3
import uuid

from codepilot.states import IN_FLIGHT, RunState, can_transition


class RunStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def claim(self, repo: str, kind: str, issue_number: int | None, title: str, task_id: str | None = None) -> str | None:
        run_id = uuid.uuid4().hex[:12]
        key = f"#{issue_number}" if issue_number is not None else f"task:{task_id or run_id}"
        try:
            self.conn.execute(
                "insert into runs(id, repo, kind, issue_number, task_id, issue_key, title, state) values(?,?,?,?,?,?,?,?)",
                (run_id, repo, kind, issue_number, task_id, key, title, RunState.TRIAGED),
            )
        except sqlite3.IntegrityError:
            return None
        return run_id

    def get(self, run_id: str) -> sqlite3.Row:
        return self.conn.execute("select * from runs where id=?", (run_id,)).fetchone()

    def set_state(self, run_id: str, state: RunState, reason: str | None = None) -> None:
        current = RunState(self.get(run_id)["state"])
        if not can_transition(current, state):
            raise ValueError(f"illegal transition {current} -> {state}")
        ended = state in {RunState.DONE, RunState.FAILED, RunState.SKIPPED, RunState.CLOSED}
        cur = self.conn.execute(                         # compare-and-set: another thread may have moved it meanwhile
            "update runs set state=?, reason=?, ended_at=case when ? then datetime('now') else ended_at end where id=? and state=?",
            (state, reason, ended, run_id, current),
        )
        if cur.rowcount != 1:
            raise ValueError(f"{run_id} changed state concurrently (was {current})")

    def update(self, run_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"update runs set {cols} where id=?", (*fields.values(), run_id))

    def recover_crashed(self) -> list[str]:
        rows = self.conn.execute(
            f"select id from runs where state in ({','.join('?' * len(IN_FLIGHT))})", tuple(IN_FLIGHT)
        ).fetchall()
        ids = [r["id"] for r in rows]
        for rid in ids:
            self.set_state(rid, RunState.FAILED, "crashed")
        return ids

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("select * from runs order by created_at desc limit ?", (limit,)).fetchall()
