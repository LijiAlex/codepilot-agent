# src/codepilot/tui/screens.py
"""Pop-up screens: inspect (read-only), new free-form task, reject reason, full-screen logs."""
from __future__ import annotations

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, RichLog, Select, Static, TextArea


class InspectScreen(ModalScreen[None]):
    """Read-only detail of a waiting item. Decides nothing."""
    BINDINGS = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="inspect"):
            yield Label("Inspect — read only (Esc to close)")
            with VerticalScroll():
                yield Static(self.text, id="inspect-body", markup=False)
        yield Footer()


class NewTaskScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, repos: list[str]):
        super().__init__()
        self.repos = repos

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task"):
            yield Static("New task (not tied to a GitHub issue) — Esc to cancel", markup=False)
            yield Label("Repository")
            yield Select([(r, r) for r in self.repos], value=self.repos[0], id="repo", allow_blank=False)
            yield Label("Title")
            yield Input(placeholder="e.g. Add a --version flag", id="title")
            yield Label("What should change (details, expected behaviour)")
            yield TextArea(id="body")
            yield Button("Start", id="start", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        title = self.query_one("#title", Input).value.strip()
        if not title:
            self.notify("A title is required", severity="warning")
            return
        self.dismiss({"repo": self.query_one("#repo", Select).value, "title": title,
                      "body": self.query_one("#body", TextArea).text})

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Spell out exactly what is about to be approved; nothing happens until y (or Enter)."""
    BINDINGS = [Binding("y,enter", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self.text, id="confirm-text", markup=False)
            yield Static("[y] confirm    [n] cancel", id="confirm-keys", markup=False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ReasonScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, item: str = ""):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="reason"):
            yield Label(Text(f"Reject {self.item} — reason (optional, becomes a lesson). Enter to confirm, Esc to cancel."))
            yield Input(id="reason-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class LogScreen(Screen[None]):
    """[l]: every log line of this session, filterable by agent or text."""
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, lines: list[tuple[str, str]]):
        super().__init__()
        self.lines = lines

    def compose(self) -> ComposeResult:
        yield Input(placeholder="filter: agent name or text", id="filter")
        yield RichLog(id="full-log", wrap=True, markup=True, min_width=20)
        yield Footer()

    def on_mount(self) -> None:
        self._show("")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._show(event.value.strip().lower())

    def _show(self, f: str) -> None:
        log = self.query_one("#full-log", RichLog)
        log.clear()
        for plain, styled in self.lines:
            if not f or f in plain.lower():
                log.write(styled)
