# src/codepilot/handoffs.py
from __future__ import annotations

import json
import sqlite3
import uuid

ENTRY_POINTS = {"publish", "continue_fix", "continue_with_decision", "continue_complex", "publish_failed"}


class HandoffStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, run_id: str, repo: str, issue_key: str, entry_point: str, payload: dict) -> str:
        if entry_point not in ENTRY_POINTS:
            raise ValueError(entry_point)
        hid = uuid.uuid4().hex[:12]
        self.conn.execute(
            "insert into handoffs(id, run_id, repo, issue_key, entry_point, payload_json) values(?,?,?,?,?,?)",
            (hid, run_id, repo, issue_key, entry_point, json.dumps(payload)),
        )
        return hid

    def get(self, hid: str) -> sqlite3.Row:
        return self.conn.execute("select * from handoffs where id=?", (hid,)).fetchone()

    def payload(self, hid: str) -> dict:
        return json.loads(self.get(hid)["payload_json"])

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute("select * from handoffs where status='pending' order by created_at").fetchall()

    def decide(self, hid: str, decision: str, by: str, reason: str = "") -> bool:
        """True only for the caller that moved it out of pending (a second decision or a racing expiry gets False)."""
        status = {"approve": "approved", "reject": "rejected"}[decision]
        cur = self.conn.execute(
            "update handoffs set status=?, decided_by=?, decided_at=datetime('now'), reason=? where id=? and status='pending'",
            (status, by, reason, hid),
        )
        return cur.rowcount == 1

    def mark(self, hid: str, status: str) -> None:
        self.conn.execute("update handoffs set status=? where id=?", (status, hid))

    def expire_for_issue(self, repo: str, issue_key: str, reason: str) -> int:
        cur = self.conn.execute(
            "update handoffs set status='expired', reason=? where repo=? and issue_key=? and status='pending'",
            (reason, repo, issue_key),
        )
        return cur.rowcount
