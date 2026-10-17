# tests/test_tui.py
import pytest
from textual.widgets import DataTable, Input, Label, ListView, RichLog, Static

from codepilot.events import RunEvent
from codepilot.tui.app import CodePilotApp
from codepilot.tui.screens import InspectScreen, LogScreen, NewTaskScreen, ReasonScreen


class FakeEngine:
    def __init__(self, ui):
        self.ui, self.calls = ui, []
        self.state = {"runs": [{"id": "r1", "repo": "o/r", "issue_key": "#7", "title": "slugify crashes", "state": "IMPLEMENTING",
                                "reason": None, "attempts": 1, "skill": "bug_fix_skill"}],
                      "pending": [{"id": "h1", "repo": "o/r", "issue_key": "#5", "entry_point": "publish",
                                   "flags": ["more_than_5_files"], "file_count": 9}],
                      "current": {"id": "r1", "issue_key": "#7", "title": "slugify crashes", "state": "IMPLEMENTING",
                                  "attempts": 1, "skill": "bug_fix_skill"},
                      "skipped": {"o/r#3": "blocked by the injection check"}, "repos": ["o/r"]}
    def startup(self): self.calls.append("startup")
    def start(self): self.calls.append("start")
    def shutdown(self): self.calls.append("shutdown")
    def view(self): return self.state
    def decide(self, hid, d, by, reason=""): self.calls.append(("decide", hid, d, reason))
    def inspect(self, hid): return "PR: [CodePilot] Handle empty title\n+ return (t or '')"
    def skip(self, repo, key, by): self.calls.append(("skip", repo, key)); return "skipped"
    def submit_task(self, repo, title, body): self.calls.append(("task", repo, title, body)); return "r9"


def app():
    holder = {}
    a = CodePilotApp(lambda ui: holder.setdefault("e", FakeEngine(ui)), user="liji")
    return a, holder["e"]


@pytest.mark.anyio
async def test_four_panels_show_issues_task_logs_and_approvals():
    a, eng = app()
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert eng.calls[:2] == ["startup", "start"]
        table = a.query_one("#issues", DataTable)
        assert table.row_count == 2 and "skipped: blocked" in str(table.get_row_at(1)[2])
        eng.ui.on_event(RunEvent("r1", "tool_call", "coder", "write_todos(...)",
                                 {"name": "write_todos", "args": {"todos": [{"content": "Reproduce", "status": "completed"},
                                                                            {"content": "Fix", "status": "pending"}]}}))
        eng.ui.on_event(RunEvent("r1", "tool_result", "coder", "refused by CodePilot+ guard: git push", {"status": "error"}))
        await pilot.pause(0.3)
        task = str(a.query_one("#task", Static).render())
        assert "Status: IMPLEMENTING" in task and "Agent: coder (retry 1/3)" in task and "[✓] Reproduce" in task and "[ ] Fix" in task
        assert any("refused by CodePilot+ guard" in plain for plain, _ in a.lines)
        items = a.query_one("#approvals", ListView).children
        assert len(items) == 1
        text = str(items[0].query_one(Label).render())
        assert "PR-to-main / git push gate" in text and "commit touches 9 files (>5-file gate)" in text


@pytest.mark.anyio
async def test_approve_reject_inspect_keys():
    a, eng = app()
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        assert ("decide", "h1", "approve", "") in eng.calls
        await pilot.press("v")
        assert isinstance(a.screen, InspectScreen)
        await pilot.press("escape")
        await pilot.press("r")
        assert isinstance(a.screen, ReasonScreen)
        await pilot.press(*"wrong file", "enter")
        assert ("decide", "h1", "reject", "wrong file") in eng.calls


@pytest.mark.anyio
async def test_new_task_skip_logs_and_quit():
    a, eng = app()
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        assert isinstance(a.screen, NewTaskScreen)
        a.screen.query_one("#title", Input).value = "Add --version"
        a.screen.query_one("#start").press()
        await pilot.pause()
        assert ("task", "o/r", "Add --version", "") in eng.calls
        await pilot.press("s")
        assert ("skip", "o/r", "#7") in eng.calls
        a.on_notice("hello")
        await pilot.press("l")
        assert isinstance(a.screen, LogScreen)
        a.screen.query_one("#filter", Input).value = "zzz-no-match"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.press("q")
        assert "shutdown" in eng.calls
