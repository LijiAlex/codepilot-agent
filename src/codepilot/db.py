# src/codepilot/db.py
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
create table if not exists runs(
  id text primary key, repo text not null, kind text not null,
  issue_number integer, task_id text, issue_key text not null, title text not null,
  task_type text, skill text, complexity integer, state text not null, reason text,
  base_sha text, sandbox_id text, attempts integer default 0, branch text, pr_number integer, pr_url text,
  tokens_in integer default 0, tokens_out integer default 0, cost_usd real default 0,
  created_at text default (datetime('now')), ended_at text
);
create unique index if not exists runs_one_active
  on runs(repo, issue_key) where state in ('TRIAGED','EXPLORING','IMPLEMENTING','TESTING','AWAITING_APPROVAL','PR_OPENED');
create table if not exists handoffs(
  id text primary key, run_id text not null, repo text not null, issue_key text not null,
  entry_point text not null, payload_json text not null, status text not null default 'pending',
  created_at text default (datetime('now')), decided_by text, decided_at text, reason text
);
create unique index if not exists handoffs_one_pending on handoffs(repo, issue_key) where status = 'pending';
create table if not exists events(
  id integer primary key autoincrement, ts text default (datetime('now')), run_id text,
  actor text not null, action text not null, target text, result text not null, detail_json text
);
create trigger if not exists events_no_update before update on events
  begin select raise(abort, 'events is append-only'); end;
create trigger if not exists events_no_delete before delete on events
  begin select raise(abort, 'events is append-only'); end;
"""


class Result:
    """A fully read query result: no cursor outlives the lock, so another thread can never interleave with it."""
    __slots__ = ("_rows", "rowcount", "lastrowid", "_i")

    def __init__(self, rows: list, rowcount: int, lastrowid: int | None):
        self._rows, self.rowcount, self.lastrowid, self._i = rows, rowcount, lastrowid, 0

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list:
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class LockedConnection(sqlite3.Connection):
    """One connection shared by the TUI, worker, decision and poll threads. Python's sqlite3 does not support
    concurrent use of one connection (live: "InterfaceError: bad parameter or other API misuse"), so every statement
    runs under a lock and its rows are read before the lock is released."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def execute(self, sql, parameters=(), /):
        with self._lock:
            cur = super().execute(sql, parameters)
            rows = cur.fetchall() if cur.description else []
            return Result(rows, cur.rowcount, cur.lastrowid)

    def executescript(self, script, /):
        with self._lock:
            return super().executescript(script)


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, factory=LockedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn
