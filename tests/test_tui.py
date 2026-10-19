# tests/test_tui.py
import pytest
from textual.widgets import DataTable, Input, Label, ListView, RichLog, Static

from codepilot.events import RunEvent
from codepilot.tui.app import CodePilotApp
from codepilot.tui.screens import ConfirmScreen, InspectScreen, LogScreen, NewTaskScreen, ReasonScreen


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
        assert table.row_count == 2 and "skipped: blocked" in str(table.get_row_at(1)[1])   # columns: Issue, Status, Title
        eng.ui.on_event(RunEvent("r1", "tool_call", "coder", "write_todos(...)",
                                 {"name": "write_todos", "args": {"todos": [{"content": "Reproduce", "status": "completed"},
                                                                            {"content": "Fix", "status": "pending"}]}}))
        eng.ui.on_event(RunEvent("r1", "tool_result", "coder", "refused by CodePilot+ guard: git push", {"status": "error"}))
        a.flush_logs(); await pilot.pause()            # flush now rather than wait for the 10 Hz timer (load-proof)
        task = str(a.query_one("#task", Static).render())
        assert "Status: IMPLEMENTING" in task and "Agent: coder (retry 1/3)" in task and "[✓] Reproduce" in task and "[ ] Fix" in task
        assert any("refused by CodePilot+ guard" in plain for plain, _ in a.lines)
        items = a.query_one("#approvals", ListView).children
        assert len(items) == 1
        text = str(items[0].query_one(Label).render())
        assert "open PR to main — 9 files changed (>5-file gate)" in text
        assert "PR-to-main / git push gate" in str(a.query_one("#approval-hint", Static).render())


@pytest.mark.anyio
async def test_approve_reject_inspect_keys():
    a, eng = app()
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a", "y")                    # approve asks first; y confirms
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


@pytest.mark.anyio
async def test_markup_in_untrusted_text_is_shown_literally():
    a, eng = app()
    evil = "fix [/] bug [link=http://x]x[/link]"
    eng.state["runs"][0]["title"] = evil
    eng.state["runs"][0]["reason"] = "[bold]r[/]"
    if eng.state.get("current"):
        eng.state["current"]["title"] = evil
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        table = a.query_one("#issues")
        assert evil[:40] in str(table.get_row_at(0)[2])



def two_pending(eng):
    eng.state["pending"] = [{"id": "h1", "repo": "o/plugin", "issue_key": "task:ab12", "entry_point": "publish", "flags": []},
                            {"id": "h2", "repo": "o/playground", "issue_key": "#1", "entry_point": "publish", "flags": []}]


@pytest.mark.anyio
async def test_approve_names_the_item_and_does_nothing_until_confirmed():
    a, eng = app(); two_pending(eng)
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        a.query_one("#approvals", ListView).index = 1
        await pilot.pause()
        await pilot.press("a")
        assert isinstance(a.screen, ConfirmScreen)
        text = str(a.screen.query_one("#confirm-text", Static).render())
        assert "o/playground" in text and "#1" in text and "Open PR to main" in text
        await pilot.press("n")
        assert not [c for c in eng.calls if c[0] == "decide"]          # cancelled: nothing decided
        await pilot.press("a", "y")
        assert ("decide", "h2", "approve", "") in eng.calls and ("decide", "h1", "approve", "") not in eng.calls


@pytest.mark.anyio
async def test_selected_item_is_spelled_out_and_approval_panel_takes_focus():
    a, eng = app(); two_pending(eng)
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        lv = a.query_one("#approvals", ListView)
        assert a.focused is lv and lv.index == 0                        # pending items: focus lands where a/r/v act
        assert "o/plugin task:ab12" in str(a.query_one("#approval-hint", Static).render())
        lv.index = 1; await pilot.pause()
        assert "o/playground #1" in str(a.query_one("#approval-hint", Static).render())


@pytest.mark.anyio
async def test_reject_screen_names_the_item():
    a, eng = app(); two_pending(eng)
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        a.query_one("#approvals", ListView).index = 1; await pilot.pause()
        await pilot.press("r")
        assert isinstance(a.screen, ReasonScreen) and "o/playground #1" in str(a.screen.query_one(Label).render())
        await pilot.press("escape")
        assert not [c for c in eng.calls if c[0] == "decide"]


@pytest.mark.anyio
async def test_issues_panel_shows_one_row_per_issue_with_status_first():
    a, eng = app()
    eng.state["runs"] = [
        {"id": "r3", "repo": "o/playground", "issue_key": "#1", "title": "Citation off by one", "state": "PR_OPENED", "reason": None},
        {"id": "r2", "repo": "o/playground", "issue_key": "#1", "title": "Citation off by one", "state": "FAILED", "reason": "setup: x"},
        {"id": "r1", "repo": "o/plugin", "issue_key": "task:ab12", "title": "Accents", "state": "FAILED", "reason": "tests"}]
    eng.state["skipped"] = {"o/playground#1": "already being processed", "o/playground#10": "blocked by the injection check"}
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        t = a.query_one("#issues", DataTable)
        rows = [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]
        assert [r[0] for r in rows] == ["playground #1", "plugin task:ab12", "playground #10"]
        assert "PR opened" in rows[0][1] and "failed" in rows[1][1] and "skipped" in rows[2][1]
        assert rows[0][2].startswith("Citation")


@pytest.mark.anyio
async def test_full_screen_logs_stack_filter_above_the_log():
    a, eng = app()
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("l"); await pilot.pause()
        f, log = a.screen.query_one("#filter").region, a.screen.query_one("#full-log").region
        assert f.y < log.y and log.width > 100                      # one column, full width (no main-screen grid)


@pytest.mark.anyio
async def test_new_task_dialog_is_centred_and_labels_every_field():
    a, eng = app()
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("i"); await pilot.pause()
        box = a.screen.query_one("#new-task").region
        assert abs((box.x + box.width / 2) - 70) <= 3                # centred horizontally
        labels = " ".join(str(l.render()) for l in a.screen.query(Label))
        assert "Repository" in labels and "Title" in labels and "What should change" in labels


@pytest.mark.anyio
async def test_selecting_an_issue_row_shows_that_runs_past_log():
    a, eng = app()
    eng.run_log = lambda rid, limit=200: [{"agent": "coder", "kind": "message", "text": "edited slugs.py"},
                                          {"agent": "test-agent", "kind": "tool_result", "text": "2 passed"}]
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        t = a.query_one("#issues", DataTable); t.focus(); t.move_cursor(row=0); await pilot.pause()
        await pilot.press("enter"); await pilot.pause()
        text = "\n".join(str(line.text) for line in a.query_one("#logs", RichLog).lines)
        assert "past log" in text and "edited slugs.py" in text and "2 passed" in text


@pytest.mark.anyio
async def test_log_lines_wrap_to_a_narrow_panel_instead_of_scrolling_sideways():
    a, eng = app()
    async with a.run_test(size=(110, 35)) as pilot:                   # panel narrower than 78 columns
        await pilot.pause()
        a.on_notice("approve: published — https://github.com/LijiAlex/codepilot-playground/pull/15 and a long tail of words here")
        a.flush_logs(); await pilot.pause()            # flush now rather than wait for the 10 Hz timer (load-proof)
        log = a.query_one("#logs", RichLog)
        assert log.scroll_x == 0 and log.virtual_size.width <= log.scrollable_content_region.width


@pytest.mark.anyio
async def test_arrow_keys_switch_the_past_log_and_refreshes_keep_the_cursor():
    a, eng = app()
    eng.state["runs"] = [{"id": "r1", "repo": "o/r", "issue_key": "#1", "title": "one", "state": "PR_OPENED", "reason": None},
                         {"id": "r2", "repo": "o/r", "issue_key": "#2", "title": "two", "state": "FAILED", "reason": "tests"}]
    eng.state["skipped"] = {}
    eng.run_log = lambda rid, limit=200: [{"agent": "coder", "kind": "message", "text": f"log of {rid}"}]
    async with a.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        t = a.query_one("#issues", DataTable); t.focus(); await pilot.pause()
        await pilot.press("down"); await pilot.pause(0.3)
        text = lambda: "\n".join(str(line.text) for line in a.query_one("#logs", RichLog).lines)
        assert "log of r2" in text() and "log of r1" not in text()
        a.refresh_view(); await pilot.pause(0.3)                     # a poll or run event refreshes the screen
        assert t.cursor_row == 1 and "log of r2" in text()          # cursor and log stay on #2


@pytest.mark.anyio
async def test_demo_visibility_polling_context_guardrail_block_and_file_count():
    a, eng = app()
    eng.state |= {"polled_at": 1_800_000_000.0, "poll_interval_s": 300, "map_budget": 4000}
    eng.state["pending"].append({"id": "h2", "repo": "o/r", "issue_key": "#8", "entry_point": "publish", "flags": [], "file_count": 2})
    async with a.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        title = str(a.query_one("#issues-panel Label", Label).render())
        assert "polled" in title and "next" in title and "every 5 min" in title
        eng.ui.on_event(RunEvent("r1", "notice", "worker", "context", {"kind": "context", "map_tokens": 2530, "lessons": 1,
                                                                      "skill": "bug_fix_skill", "repo_skill_version": 2}))
        eng.ui.on_event(RunEvent("r1", "tool_call", "coder", "execute(...)",
                                 {"name": "execute", "args": {"command": "rm -rf build"}, "id": "c1"}))
        eng.ui.on_event(RunEvent("r1", "tool_result", "coder", "refused by CodePilot+ guard: deleting or running files by shell is not allowed; use the file tools.",
                                 {"name": "execute", "status": "error", "tool_call_id": "c1"}))
        a.flush_logs(); await pilot.pause()            # flush now rather than wait for the 10 Hz timer (load-proof)
        task = str(a.query_one("#task", Static).render())
        assert "Context: repo map 2,530 / 4,000 tokens · 1 lesson recalled · repo skill v2" in task
        blocked = [p for p, _ in a.lines if p.startswith("[guardrail]")]
        assert blocked and "rm -rf build" in blocked[0] and "delete-files" in blocked[0]
        labels = [str(i.query_one(Label).render()) for i in a.query_one("#approvals", ListView).children]
        assert "2 files changed" in labels[1] and ">5-file gate" not in labels[1]
