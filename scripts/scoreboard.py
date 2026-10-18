# scripts/scoreboard.py
"""Markdown summary of CodePilot+ runs for the README (from ~/.codepilot/codepilot.db).
Usage: uv run python scripts/scoreboard.py [--db PATH]"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path



def cell(text: str, limit: int = 80) -> str:
    """One Markdown table cell: one line, no unescaped pipes, shortened."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"

def scoreboard(conn: sqlite3.Connection) -> str:
    conn.row_factory = sqlite3.Row
    runs = conn.execute("select * from runs order by created_at").fetchall()
    by_state: dict[str, int] = {}
    for r in runs:
        k = r["state"] + (f" ({cell(r['reason'])})" if r["reason"] and r["state"] in ("FAILED", "SKIPPED", "CLOSED") else "")
        by_state[k] = by_state.get(k, 0) + 1
    ev = lambda action: conn.execute("select count(*) from events where action=?", (action,)).fetchone()[0]  # noqa: E731
    lines = ["| Measure | Value |", "|---|---|", f"| Runs | {len(runs)} |",
             f"| PRs opened | {sum(1 for r in runs if r['pr_url'])} |",
             f"| PRs merged | {sum(1 for r in runs if r['state'] == 'DONE')} |",
             f"| Commands refused by the guard | {ev('command_refused')} |",
             f"| Unexpected commands sent to a person | {ev('command_needs_decision')} |",
             f"| Issues blocked by the injection check | {ev('issue_blocked')} |",
             f"| Approvals / rejections | {ev('handoff_approve')} / {ev('handoff_reject')} |",
             f"| Tokens in / out | {sum(r['tokens_in'] or 0 for r in runs):,} / {sum(r['tokens_out'] or 0 for r in runs):,} |",
             "", "| Outcome | Runs |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(by_state.items())]
    lines += ["", "| Run | Issue | Type | Complexity | Outcome | PR |", "|---|---|---|---|---|---|"]
    lines += [f"| `{r['id']}` | {r['repo']} {r['issue_key']} | {r['task_type'] or '-'} | {r['complexity'] or '-'} | "
              f"{r['state']}{' (' + cell(r['reason']) + ')' if r['reason'] else ''} | {r['pr_url'] or '-'} |" for r in runs]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / ".codepilot" / "codepilot.db"))
    a = ap.parse_args(argv)
    print(scoreboard(sqlite3.connect(a.db)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
