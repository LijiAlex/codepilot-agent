# src/codepilot/watcher.py
"""Watch open CodePilot+ PRs: merged → DONE + lesson; closed → CLOSED; not mergeable → one notice.
Also run at startup to reconcile merges made while CodePilot+ was off (E17)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codepilot.states import RunState


@dataclass
class WatchServices:
    runs: Any
    handoffs: Any
    audit: Any
    episodic: Any
    repo_factory: Callable[[str], Any]
    lessons: Any = None
    on_notice: Callable[[str], None] = lambda text: None
    runs_dir: Any = None                        # Path to ~/.codepilot/runs: working memory is cleared at DONE/CLOSED


@dataclass
class WatchResult:
    done: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


class PRWatcher:
    def __init__(self, ws: WatchServices):
        self.ws = ws
        self._noticed: set[str] = set()

    def _publish_payload(self, run_id: str) -> dict:
        row = self.ws.runs.conn.execute(
            "select payload_json from handoffs where run_id=? and entry_point in ('publish','publish_failed') "
            "order by created_at desc limit 1", (run_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else {}

    def _lesson(self, run, payload: dict) -> None:
        if self.ws.lessons is None or not payload:
            return
        d, files = payload.get("draft", {}), payload.get("files", [])
        text = (f"Issue '{run['title']}' ({run['task_type']}) was fixed by changing {', '.join(files[:6])}. "
                f"Approach: {d.get('pr_approach') or d.get('commit_summary', '')}"[:600])
        self.ws.lessons.add(run["repo"], run["task_type"], text, "merged", run["id"])

    def check(self) -> WatchResult:
        ws, res = self.ws, WatchResult()
        for run in ws.runs.conn.execute("select * from runs where state='PR_OPENED'").fetchall():
            pr = ws.repo_factory(run["repo"]).get_pull(run["pr_number"])
            if pr.merged or pr.state == "closed":
                if ws.runs_dir is not None:
                    (Path(ws.runs_dir) / run["id"] / "shared" / "task.json").unlink(missing_ok=True)
            payload = self._publish_payload(run["id"]) if (pr.merged or pr.state == "closed") else {}
            facts = {"task_type": run["task_type"], "files": payload.get("files", [])}
            if pr.merged:
                ws.runs.set_state(run["id"], RunState.DONE, "merged")
                self._lesson(run, payload)
                ws.episodic.record_run(run["repo"], run["id"], run["issue_key"], "DONE", "merged", run["title"], **facts)
                ws.audit.log("watcher", "pr_merged", run_id=run["id"], target=run["pr_url"])
                res.done.append(run["id"])
            elif pr.state == "closed":
                ws.runs.set_state(run["id"], RunState.CLOSED, "pr closed")
                ws.episodic.record_run(run["repo"], run["id"], run["issue_key"], "CLOSED", "pr closed", run["title"], **facts)
                ws.audit.log("watcher", "pr_closed", run_id=run["id"], target=run["pr_url"])
                res.closed.append(run["id"])
            elif pr.mergeable is False and run["id"] not in self._noticed:
                self._noticed.add(run["id"])
                msg = f"{run['repo']} PR #{run['pr_number']} has conflicts with the default branch"
                ws.on_notice(msg)
                res.notices.append(msg)
        return res
