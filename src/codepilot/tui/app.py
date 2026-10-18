# src/codepilot/tui/app.py
"""The brief's four-panel TUI: GitHub Issues · Active Task · Agent Logs · Human Approval."""
from __future__ import annotations

import getpass
from collections import deque
from typing import Callable

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Label, ListItem, ListView, RichLog, Static

from codepilot.engine import UiCallbacks
from codepilot.tui.screens import ConfirmScreen, InspectScreen, LogScreen, NewTaskScreen, ReasonScreen

STATE_ICON = {"DONE": "✓", "FAILED": "✗", "SKIPPED": "–", "CLOSED": "■", "AWAITING_APPROVAL": "⚠", "PR_OPENED": "⇪"}
ENTRY_LABEL = {"publish": "Open PR to main (PR-to-main / git push gate)", "publish_failed": "Publishing failed — retry the PR",
               "continue_fix": "Retry after 2 failed test runs", "continue_with_decision": "Unexpected command",
               "continue_complex": "Complex issue — plan"}
KIND_STYLE = {"tool_call": "cyan", "tool_result": "dim", "message": "white", "state": "bold yellow",
              "interrupt": "bold magenta", "notice": "yellow", "error": "bold red"}


def _short(repo: str) -> str:
    return repo.split("/", 1)[-1]


class CodePilotApp(App):
    TITLE = "CodePilot+"
    CSS = """
    Screen { layout: grid; grid-size: 2 2; grid-columns: 1fr 1fr; grid-rows: 1fr 1fr; }
    .panel { border: round $panel-lighten-2; padding: 0 1; }
    .panel > Label { text-style: bold; color: $text-muted; }
    .panel:focus-within { border: heavy $warning; }              /* the panel the arrow keys move in */
    .panel:focus-within > Label { color: $warning; }
    #approvals > ListItem { padding: 0 1; }
    #approvals > ListItem.-highlight { background: $warning 35%; text-style: bold; }
    #approvals:focus > ListItem.-highlight { background: $warning; color: $background; text-style: bold; }
    #approval-hint { color: $warning; text-style: bold; height: auto; }
    #inspect, #new-task, #reason { width: 90%; height: 90%; border: thick $accent; background: $surface; padding: 1; }
    ConfirmScreen { align: center middle; }
    #confirm { width: 90; max-width: 95%; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    #confirm-keys { margin-top: 1; color: $text-muted; }
    """
    BINDINGS = [Binding("i", "new_task", "New task"), Binding("s", "skip", "Skip issue"), Binding("q", "quit", "Quit"),
                Binding("l", "logs", "Logs"), Binding("a", "approve", "Approve"), Binding("r", "reject", "Reject"),
                Binding("v", "inspect", "Inspect")]

    def __init__(self, engine_factory: Callable[[UiCallbacks], object], user: str | None = None):
        super().__init__()
        self.user = user or getpass.getuser()
        self.engine = engine_factory(UiCallbacks(on_event=self._from_thread(self.on_run_event),
                                                 on_notice=self._from_thread(self.on_notice),
                                                 on_change=self._from_thread(self.refresh_view)))
        self.buffer: deque = deque()
        self.lines: list[tuple[str, str]] = []
        self.last_agent: dict[str, str] = {}
        self.todos: dict[str, list] = {}
        self.pending: list[dict] = []
        self.issue_rows: list[dict] = []

    def _from_thread(self, fn):
        def call(*args):
            try:
                self.call_from_thread(fn, *args)
            except RuntimeError:                       # same thread (tests) or app not running yet
                fn(*args)
        return call

    def compose(self) -> ComposeResult:
        with Vertical(classes="panel", id="issues-panel"):
            yield Label("GitHub Issues")
            yield DataTable(id="issues", cursor_type="row", zebra_stripes=True)
        with Vertical(classes="panel", id="task-panel"):
            yield Label("Active Task")
            yield Static("idle — waiting for issues", id="task", markup=False)
        with Vertical(classes="panel", id="logs-panel"):
            yield Label("Agent Logs")
            yield RichLog(id="logs", wrap=True, markup=True, max_lines=2000)
        with Vertical(classes="panel", id="approval-panel"):
            yield Label("Human Approval")
            yield ListView(id="approvals")
            yield Static("nothing waiting", id="approval-hint", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#issues", DataTable).add_columns("Issue", "Status", "Title")
        self.set_interval(0.1, self.flush_logs)             # ~10 Hz: token floods never freeze the UI
        self.boot()

    @work(thread=True, exit_on_error=False)
    def boot(self) -> None:
        try:
            self.engine.startup()
            self.engine.start()
        except Exception as e:                              # noqa: BLE001 — shown, not swallowed
            self.call_from_thread(self.on_notice, f"startup failed: {e}")
            return
        self.call_from_thread(self.refresh_view)

    # ---- incoming ----------------------------------------------------------
    def on_run_event(self, e) -> None:
        self.buffer.append(e)

    def on_notice(self, text: str) -> None:
        self._log("notice", "CodePilot+", text)
        self.notify(text, timeout=6)

    def flush_logs(self) -> None:
        tokens: dict[str, str] = {}
        while self.buffer:
            e = self.buffer.popleft()
            self.last_agent[e.run_id] = e.agent
            if e.kind == "token":
                tokens[e.agent] = tokens.get(e.agent, "") + e.text
                continue
            if e.kind == "tool_call" and e.data.get("name") == "write_todos":
                self.todos[e.run_id] = e.data.get("args", {}).get("todos", [])
            kind = "error" if e.kind == "tool_result" and e.data.get("status") == "error" else e.kind
            self._log(kind, e.agent, e.text)
            if e.kind in ("state", "interrupt"):
                self.refresh_view()
        for agent, text in tokens.items():                  # thoughts arrive as tokens; show them compactly
            self._log("message", agent, text.strip()[:400])
        if tokens or self.lines:
            self._render_task()

    def _log(self, kind: str, agent: str, text: str) -> None:
        plain = f"[{agent}] {text}"
        styled = f"[{KIND_STYLE.get(kind, 'white')}]\\[{escape(agent)}][/] {escape(text)}"
        self.lines.append((plain, styled))
        self.query_one("#logs", RichLog).write(styled)

    # ---- views -------------------------------------------------------------
    def refresh_view(self) -> None:
        v = self.engine.view()
        table = self.query_one("#issues", DataTable)
        table.clear()
        self.issue_rows = []
        seen: set[tuple[str, str]] = set()
        for r in v["runs"]:                                  # newest first: one row per issue, its latest run
            key = (r["repo"], r["issue_key"])
            if key in seen:
                continue
            seen.add(key)
            icon = STATE_ICON.get(r["state"], "●")
            state = "PR opened" if r["state"] == "PR_OPENED" else r["state"].lower().replace("_", " ")
            reason = r.get("reason") or ""
            status = f"{icon} {state}" + (f" ({reason.splitlines()[0][:22]})" if reason else "")
            table.add_row(Text(_short(r["repo"]) + " " + r["issue_key"]), Text(status), Text(r["title"][:60]))
            self.issue_rows.append(r)
        for key, why in v["skipped"].items():                # "owner/name#N" → why the last poll skipped it
            repo, num = key.split("#", 1)
            if (repo, f"#{num}") in seen or why == "already being processed":
                continue                                      # already listed with its run's state
            table.add_row(Text(f"{_short(repo)} #{num}"), Text(f"– skipped: {why}"), "")
            self.issue_rows.append({"repo": repo, "issue_key": f"#{num}"})
        approvals = self.query_one("#approvals", ListView)
        keep = self._selected_id()
        had_pending = bool(self.pending)
        approvals.clear()
        self.pending = v["pending"]
        for h in self.pending:
            label = f"⚠ {h['repo']} {h['issue_key']}: {ENTRY_LABEL.get(h['entry_point'], h['entry_point'])}"
            if "more_than_5_files" in h.get("flags", []):          # brief gate: commit touching more than 5 files (E18)
                label += f" — commit touches {h.get('file_count', '?')} files (>5-file gate)"
            approvals.append(ListItem(Label(Text(label))))
        ids = [h["id"] for h in self.pending]
        idx = ids.index(keep) if keep in ids else (0 if ids else None)

        def restore() -> None:                                  # after the new rows are mounted
            approvals.index = idx
            if self.pending and not had_pending and self.screen is self.screen_stack[0]:
                approvals.focus()                               # items arrived: focus where a / r / v act
            self._update_hint()
        self.call_after_refresh(restore)
        self._current = v["current"]
        self._render_task()

    def _render_task(self) -> None:
        cur = getattr(self, "_current", None)
        box = self.query_one("#task", Static)
        if not cur:
            box.update("idle — waiting for issues")
            return
        todos = self.todos.get(cur["id"], [])
        todo_lines = "\n".join(f"  [{'✓' if t.get('status') == 'completed' else ' '}] {t.get('content', '')}" for t in todos)
        box.update(Text(f"{cur['issue_key']}: {cur['title']}\nStatus: {cur['state']}\n"
                        f"Agent: {self.last_agent.get(cur['id'], '-')} (retry {cur.get('attempts') or 0}/3)\n"
                        f"Skill: {cur.get('skill') or '-'}\nTodo:\n{todo_lines or '  (not planned yet)'}"))

    # ---- actions -----------------------------------------------------------
    def _describe(self, h: dict) -> str:
        return f"{h['repo']} {h['issue_key']} — {ENTRY_LABEL.get(h['entry_point'], h['entry_point'])}"

    def _selected_id(self) -> str | None:
        h = self._selected_handoff(quiet=True)
        return h["id"] if h else None

    def _selected_handoff(self, quiet: bool = False) -> dict | None:
        lv = self.query_one("#approvals", ListView)
        if not self.pending:
            if not quiet:
                self.notify("Nothing is waiting for approval")
            return None
        if lv.index is None or not 0 <= lv.index < len(self.pending):
            if not quiet:
                self.notify("Select an item in Human Approval first (Tab to the panel, then ↑/↓ or click)")
            return None
        return self.pending[lv.index]

    def _update_hint(self) -> None:
        h = self._selected_handoff(quiet=True)
        text = (f"Selected: {self._describe(h)}\n[a] approve  [r] reject  [v] inspect" if h else
                ("Select an item (↑/↓ or click)" if self.pending else "nothing waiting"))
        self.query_one("#approval-hint", Static).update(Text(text))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "approvals":
            self._update_hint()

    def action_approve(self) -> None:
        h = self._selected_handoff()
        if not h:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.engine.decide(h["id"], "approve", self.user)
                self.notify(f"approved {h['repo']} {h['issue_key']}")
        flags = f"\nFlags: {', '.join(h['flags'])}" if h.get("flags") else ""
        self.push_screen(ConfirmScreen(f"Approve this item?\n\n{self._describe(h)}{flags}\n\n"
                                       "Approving lets CodePilot+ act on GitHub for this item."), done)

    def action_reject(self) -> None:
        h = self._selected_handoff()
        if h:
            def done(reason: str | None) -> None:
                if reason is not None:
                    self.engine.decide(h["id"], "reject", self.user, reason)
                    self.notify(f"rejected {h['repo']} {h['issue_key']}")
            self.push_screen(ReasonScreen(self._describe(h)), done)

    def action_inspect(self) -> None:
        h = self._selected_handoff()
        if h:
            self.push_screen(InspectScreen(self.engine.inspect(h["id"])))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_inspect()

    def action_new_task(self) -> None:
        def done(task: dict | None) -> None:
            if task:
                rid = self.engine.submit_task(task["repo"], task["title"], task["body"])
                self.notify("task queued" if rid else "a task with this id is already active")
        self.push_screen(NewTaskScreen(self.engine.view()["repos"]), done)

    def action_skip(self) -> None:
        table = self.query_one("#issues", DataTable)
        if not self.issue_rows:
            return
        row = self.issue_rows[table.cursor_row]
        self.notify(self.engine.skip(row["repo"], row["issue_key"], self.user))

    def action_logs(self) -> None:
        self.push_screen(LogScreen(list(self.lines)))

    async def action_quit(self) -> None:
        self.engine.shutdown()
        self.exit()
