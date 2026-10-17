# src/codepilot/audit.py
"""Append-only record of every action by people, agents and the system."""
from __future__ import annotations

import json
import re
import sqlite3

_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"dtn_[A-Za-z0-9]{20,}"),                 # Daytona
    re.compile(r"lsv2_[A-Za-z0-9_]{20,}"),               # LangSmith
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.=]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S+"),
]


def redact(text: str) -> str:
    for p in _PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


class AuditLog:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def log(self, actor: str, action: str, *, run_id: str | None = None, target: str | None = None,
            result: str = "ok", detail: dict | None = None) -> None:
        detail_json = redact(json.dumps(detail or {}, default=str))[:8000]
        self.conn.execute(
            "insert into events(run_id, actor, action, target, result, detail_json) values(?,?,?,?,?,?)",
            (run_id, actor, action, redact(target or "") or None, result, detail_json),
        )

    def for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("select * from events where run_id=? order by id", (run_id,)).fetchall()
