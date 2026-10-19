# src/codepilot/worker.py
"""One run, start to finish: triage → (plan proposal) → sandbox → agent graph → file check → handoff.
No flow waits on a person: a decision becomes a handoff, the run ends and its sandbox is deleted."""
from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langgraph.checkpoint.memory import InMemorySaver

from codepilot.agents.graph import RunContext, build_graph
from codepilot.agents.tools import AttemptState
from codepilot.agents.triage import TriageError, TriageResult, needs_plan, plan_as_todos, propose_plan, triage
from codepilot.audit import redact
from codepilot.changes import SavedFile, apply_changes, capture_changes, from_payload, to_file_changes, to_payload, unified_diff
from codepilot.config import RepoConfig, Settings
from codepilot.events import EventRecorder, EventTranslator, RunEvent, subagent_paths
from codepilot.gh.reader import IssueSnapshot, make_read_claimed_issue_tool
from codepilot.memory.working import WorkingMemory
from codepilot.observability import cost_usd
from codepilot.repomap.build import build_repo_map, scan
from codepilot.repomap.cache import RepoMapCache
from codepilot.safety.file_check import check_changes
from codepilot.sandbox.daytona import SandboxSetupError
from codepilot.skills import load_skills
from codepilot.states import RunState

PHASE_ORDER = [RunState.TRIAGED, RunState.EXPLORING, RunState.IMPLEMENTING, RunState.TESTING]
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){0,7}$")

_READ_ONLY = {"git", "ls", "cat", "head", "tail", "grep", "find", "wc", "echo", "pwd", "tree", "diff", "sort", "uniq", "cd", "true"}


def setup_reason(e: Exception) -> str:
    """First line (which command) + the last lines (why it failed): pip puts the cause at the end."""
    lines = [ln for ln in redact(str(e)).splitlines() if ln.strip()]
    if len(lines) <= 2:
        return ("setup: " + " | ".join(lines))[:300]
    tail = " | ".join(lines[-2:])
    return f"setup: {lines[0][:80]} … {tail[-200:]}"


_PY_FLAGS_WITH_VALUE = {"-W", "-X"}       # interpreter options that take the next word, e.g. -W error::DeprecationWarning


def _runs_tests(argv: list[str]) -> bool:
    prog = argv[0].rsplit("/", 1)[-1]
    if prog == "pytest":
        return True
    if prog not in ("python", "python3"):
        return False
    rest = argv[1:]
    while rest and rest[0].startswith("-") and rest[0] != "-m":     # skip interpreter flags before -m pytest
        rest = rest[2:] if rest[0] in _PY_FLAGS_WITH_VALUE else rest[1:]
    return rest[:2] == ["-m", "pytest"]


def _changes_files(data: dict) -> bool:
    """A tool call that may change the repository: file writes under /sandbox/, or a shell command that is not read-only."""
    name, args = data.get("name"), data.get("args") or {}
    if name in ("write_file", "edit_file"):
        return str(args.get("file_path", "")).startswith("/sandbox/")
    if name == "execute":
        try:
            segs = [shlex.split(seg) for seg in re.split(r"&&|\|\||;|\|", str(args.get("command", "")))]
        except ValueError:
            return True
        return any(argv and argv[0].rsplit("/", 1)[-1] not in _READ_ONLY and not _runs_tests(argv) for argv in segs)
    return False

class TimeLimitExceeded(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


@dataclass
class WorkItem:
    repo: RepoConfig
    issue: IssueSnapshot | None = None
    task: dict | None = None                    # free-form [i]: {"id", "title", "body"}
    below_threshold_only: bool = False          # picked up as unassigned (brief): skip if above the threshold

    @property
    def key(self) -> str:
        return f"#{self.issue.number}" if self.issue else f"task:{self.task['id']}"

    @property
    def title(self) -> str:
        return self.issue.title if self.issue else self.task["title"]

    def text(self) -> str:
        if self.issue:
            return make_read_claimed_issue_tool(self.issue).invoke({})
        return f"<untrusted_issue>\nTask: {self.task['title']}\n\n{self.task['body']}\n</untrusted_issue>"

    def payload(self) -> dict:
        if self.issue:
            return {"issue": asdict(self.issue), "labelled": not self.below_threshold_only}
        return {"task": dict(self.task)}


@dataclass
class Seed:
    """What a continuation carries into its new run."""
    base_sha: str | None = None
    triage: dict | None = None
    prior_failures: int = 0
    saved: list[dict] = field(default_factory=list)
    approved_command: str | None = None
    plan_text: str = ""
    plan_approved: bool = False
    notes: str = ""
    time_cap_minutes: int | None = None         # E9: an approved plan's estimate caps the run


@dataclass
class Outcome:
    run_id: str
    state: str
    reason: str | None = None
    handoff_id: str | None = None
    reclaim_local: bool = False


@dataclass
class Services:
    settings: Settings
    runs: Any                                   # RunStore
    handoffs: Any                               # HandoffStore
    audit: Any                                  # AuditLog
    mirror: Any                                 # HostMirror
    providers: dict[str, Any]                   # "daytona" / "local" → SandboxProvider
    make_models: Callable[[], dict[str, Any]]   # fresh per run
    triage_model: Callable[[], Any]
    episodic: Any
    lessons: Any = None                         # LessonStore
    file_index: Callable[[str], Any] | None = None
    on_event: Callable[[RunEvent], None] = lambda e: None
    on_run_end: Callable[[Outcome], None] = lambda o: None
    tracer: Any = None                          # LangChainTracer with redaction, or None
    repo_skill: Callable[[RepoConfig, list], tuple[int, str]] | None = None   # Plus: (version, text) for this repo


class Worker:
    def __init__(self, services: Services):
        self.s = services
        self.skills = load_skills()
        self._cancelled: set[str] = set()
        self._facts: dict[str, dict] = {}                # run id → task type, files, start time (for the episode)

    def cancel(self, run_id: str) -> None:
        """[s] on an in-flight run (E12): the stream loop stops at the next event."""
        self._cancelled.add(run_id)

    # ---- helpers -----------------------------------------------------------
    def _emit(self, rec: EventRecorder, e: RunEvent) -> None:
        rec.write(e)
        self.s.on_event(e)

    def _advance(self, run_id: str, target: RunState) -> RunState | None:
        current = RunState(self.s.runs.get(run_id)["state"])
        if current not in PHASE_ORDER or current == target:
            return None
        if PHASE_ORDER.index(target) > PHASE_ORDER.index(current):
            for st in PHASE_ORDER[PHASE_ORDER.index(current) + 1: PHASE_ORDER.index(target) + 1]:
                self.s.runs.set_state(run_id, st)
            return target
        try:
            self.s.runs.set_state(run_id, target)          # allowed step back (TESTING→IMPLEMENTING, IMPLEMENTING→EXPLORING)
            return target
        except ValueError:
            return None

    def _phase_for(self, e: RunEvent) -> RunState | None:
        if e.kind != "tool_call":
            return None
        name = e.data.get("name")
        if name == "task":
            return {"repo-explorer": RunState.EXPLORING, "coder": RunState.IMPLEMENTING}.get(e.data["args"].get("subagent_type"))
        if name == "run_tests":
            return RunState.TESTING
        if name in ("edit_file", "write_file") and e.agent == "coder":
            return RunState.IMPLEMENTING
        return None

    @staticmethod
    def _working_memory(wm: WorkingMemory, e: RunEvent, translator: EventTranslator, attempts: AttemptState) -> None:
        """Keep /shared/task.json current, so each subagent can read the task state by path (brief: working memory)."""
        if e.kind == "message" and e.agent == "repo-explorer":
            files = subagent_paths(e.text)
            if files:
                wm.update(relevant_files=files)
        elif e.kind == "tool_result" and e.data.get("name") == "show_diff":
            wm.update(diff_path="/shared/proposed_diff.txt")
        elif e.kind == "tool_call" and _changes_files(e.data):
            attempts.pending_changes[e.data.get("id")] = e.data.get("name")   # judged when its result shows it ran
        elif e.kind == "tool_result" and e.data.get("tool_call_id") in attempts.pending_changes:
            name = attempts.pending_changes.pop(e.data.get("tool_call_id"))
            refused = (e.text or "").startswith("refused by CodePilot+ guard")
            ran = not refused if name == "execute" else e.data.get("status") != "error"
            if ran:
                attempts.verified = None                           # a change after the last verify is untested
        elif e.kind == "tool_result" and e.data.get("name") == "run_tests" and attempts.last_report is not None:
            r = attempts.last_report
            wm.update(test_results={"outcome": r.outcome, "passed": r.passed, "failed": r.failed, "failing_ids": r.failing_ids},
                      retry_count=attempts.total_failures)

    def _entries(self, repo: RepoConfig, mirror: Path, sha: str):
        cache = RepoMapCache(self.s.settings.home / "repos" / repo.owner / repo.name / "maps")
        got = cache.get(sha)
        if got is not None:
            return got
        old = cache.latest_sha()
        if old:
            return cache.refresh(mirror, old, sha, self.s.mirror.changed_files(mirror, old, sha))
        entries = scan(mirror)
        cache.put(sha, entries)
        return entries

    def _searcher(self, repo: RepoConfig, mirror: Path, sha: str, entries):
        """File-chunk index keyed by the map SHA; only files changed since the indexed SHA are re-embedded (E15)."""
        idx = self.s.file_index(repo.full_name) if self.s.file_index else None
        if idx is None or idx.sha == sha:
            return idx
        if idx.sha is None:
            idx.build(mirror, [e.path for e in entries], sha)
        else:
            idx.refresh(mirror, self.s.mirror.changed_files(mirror, idx.sha, sha), sha)
        return idx

    def _finish(self, rec, run_id: str, item: WorkItem, state: RunState, reason: str | None = None,
                handoff_id: str | None = None, reclaim_local: bool = False, summary: str = "") -> Outcome:
        self.s.runs.set_state(run_id, state, reason)
        self._emit(rec, RunEvent(run_id, "state", "worker", f"{state}" + (f" ({reason})" if reason else "")))
        facts = self._facts.pop(run_id, {})
        terminal = state in (RunState.DONE, RunState.FAILED, RunState.SKIPPED, RunState.CLOSED)
        if terminal or state == RunState.AWAITING_APPROVAL:     # every finished run is an episode (brief: tasks attempted)
            self.s.episodic.record_run(item.repo.full_name, run_id, item.key, str(state), reason, summary or item.title,
                                       task_type=facts.get("task_type"), files=facts.get("files"),
                                       duration_s=round(time.monotonic() - facts["start"], 1) if "start" in facts else None)
        if terminal:
            WorkingMemory(self.s.settings.home / "runs" / run_id / "shared").clear()     # brief: cleared at DONE/FAILED
        out = Outcome(run_id, str(state), reason, handoff_id, reclaim_local)
        self.s.on_run_end(out)
        return out

    def _failure_lesson(self, item: WorkItem, task_type: str | None, reason: str, run_id: str) -> None:
        if self.s.lessons is not None and task_type:
            self.s.lessons.add(item.repo.full_name, task_type, f"An earlier attempt at a {task_type} failed: {reason}", "failure", run_id)

    def _draft(self, shared: Path, item: WorkItem, tr: TriageResult) -> dict:
        try:
            d = json.loads((shared / "pr_draft.json").read_text())
            if not SLUG_RE.match(d.get("branch_slug", "")):
                raise ValueError("bad slug")
            return d
        except (OSError, ValueError, json.JSONDecodeError):
            slug = "-".join(re.findall(r"[a-z0-9]+", item.title.lower())[:5]) or "change"
            return {"branch_slug": slug, "commit_summary": tr.summary[:60], "commit_bullets": [tr.summary], "why": "",
                    "pr_summary": tr.summary, "pr_approach": "", "risks": "", "fallback": True}

    # ---- the run -----------------------------------------------------------
    def run(self, item: WorkItem, run_id: str, seed: Seed | None = None, sandbox_kind: str | None = None) -> Outcome:
        s, seed = self.s, seed or Seed()
        repo = item.repo
        rec = EventRecorder(s.settings.home / "runs" / run_id / "events.jsonl")
        kind = sandbox_kind or s.settings.sandbox
        provider, handle, tr = s.providers[kind], None, None
        translator = EventTranslator(run_id)
        self._facts[run_id] = {"start": time.monotonic()}
        try:
            mpath = s.mirror.ensure(repo.owner, repo.name)
            base_sha = seed.base_sha or s.mirror.head_sha(mpath, f"origin/{repo.default_branch}")
            s.mirror.checkout(mpath, base_sha)
            entries = self._entries(repo, mpath, base_sha)
            issue_text = item.text()
            if seed.triage:
                tr = TriageResult(**seed.triage)
            else:
                tr = triage(s.triage_model(), issue_text,
                            build_repo_map(mpath, base_sha, budget_tokens=s.settings.repo_map_budget_tokens, entries=entries).text,
                            config=self._trace_config(run_id, item, "triage"))
            skill = self.skills[tr.task_type]
            self._facts[run_id]["task_type"] = tr.task_type
            s.runs.update(run_id, task_type=tr.task_type, complexity=tr.complexity, base_sha=base_sha, skill=skill.display_name)
            self._emit(rec, RunEvent(run_id, "notice", "orchestrator",
                                     f"triage: {tr.task_type}, complexity {tr.complexity}/10 → {skill.display_name}"))
            if item.below_threshold_only and tr.complexity > s.settings.complexity_threshold:
                return self._finish(rec, run_id, item, RunState.SKIPPED, "above_threshold")
            if needs_plan(tr, s.settings.complexity_threshold, seed.plan_approved):
                plan = propose_plan(s.triage_model(), issue_text,
                                    build_repo_map(mpath, base_sha, budget_tokens=s.settings.repo_map_budget_tokens, entries=entries).text, tr,
                                    config=self._trace_config(run_id, item, "plan"))
                hid = s.handoffs.create(run_id, repo.full_name, item.key, "continue_complex",
                                        {"plan": plan.model_dump(), "triage": tr.model_dump(), "base_sha": base_sha, **item.payload()})
                return self._finish(rec, run_id, item, RunState.AWAITING_APPROVAL, "complex", hid)
            focused = build_repo_map(mpath, base_sha, budget_tokens=s.settings.repo_map_budget_tokens,
                                     focus_terms=tr.identifiers, entries=entries)
            rs_version, rs_text = s.repo_skill(repo, focused.files) if s.repo_skill else (0, "")
            handle = provider.create(run_id, repo.owner, repo.name, base_sha, repo.install_command)
            s.runs.update(run_id, sandbox_id=handle.sandbox_id)
            (handle.shared_dir / "repo_map.md").write_text(focused.text)
            if seed.saved:
                apply_changes(handle.backend, from_payload(seed.saved))
            notes = seed.notes
            if seed.approved_command:                  # approved by a person: code runs it once, before the Coder (E10)
                r = handle.backend.execute(seed.approved_command, timeout=600)
                s.audit.log("human", "command_run_after_approval", run_id=run_id, target=seed.approved_command,
                            result=str(r.exit_code))
                notes += f"\nA person approved `{seed.approved_command}`; CodePilot+ ran it (exit {r.exit_code}):\n{r.output[-3000:]}"
            wm = WorkingMemory(handle.shared_dir)
            wm.init(item.payload(), tr.task_type, retry_count=seed.prior_failures)
            wm.update(repo_map_path="/shared/repo_map.md", notes=[n for n in [notes.strip()] if n])
            attempts = AttemptState(prior_failures=seed.prior_failures)
            lessons = s.lessons.top(repo.full_name, tr.task_type, item.title) if s.lessons is not None else []
            ctx = RunContext(
                run_id=run_id, repo=repo.full_name, task_type=tr.task_type, complexity=tr.complexity, skill=skill,
                backend=handle.backend, shared_dir=handle.shared_dir, mirror=mpath, base_sha=base_sha,
                test_command=repo.test_command, audit=s.audit, attempts=attempts, map_text=focused.text,
                issue_tool=make_read_claimed_issue_tool(item.issue) if item.issue else None,
                searcher=self._searcher(repo, mpath, base_sha, entries), lessons=lessons, repo_skill=rs_text,
                plan_text=(seed.plan_text + ("\n" + notes if notes else "")).strip(),
                approved_commands=frozenset(),        # an approved command is run once, by code, above (spec E10)
                max_model_calls=s.settings.max_model_calls_per_run, max_tool_calls=s.settings.max_tool_calls_per_run,
                reinstall=(lambda: provider.reinstall(handle, repo.install_command)) if repo.install_command else None,
                protected_paths=list(repo.protected_paths), top_k=s.settings.top_k)
            self._emit(rec, RunEvent(run_id, "notice", "worker", f"context: repo map {focused.tokens} tokens, "
                                     f"{len(lessons)} lesson(s), skill {skill.display_name}",
                                     {"kind": "context", "map_tokens": focused.tokens, "map_files": len(focused.files),
                                      "lessons": len(lessons), "skill": skill.display_name, "plan": bool(seed.plan_text),
                                      "repo_skill_version": rs_version}))
            graph = build_graph(ctx, s.make_models(), InMemorySaver())   # interrupt() needs a checkpointer; never resumed
            minutes = seed.time_cap_minutes or s.settings.max_active_minutes_per_run
            deadline = time.monotonic() + minutes * 60
            start = f"Work on {item.key} in {repo.full_name}. Working state: {WorkingMemory.VIRTUAL_PATH}."
            failures_seen = attempts.total_failures
            for ns, mode, data in graph.stream({"messages": [{"role": "user", "content": start}]},
                                               {"configurable": {"thread_id": run_id}, "recursion_limit": 1000,
                                                "metadata": {"run_id": run_id, "issue": item.key, "repo": repo.full_name},
                                                "callbacks": [s.tracer] if s.tracer else []},
                                               stream_mode=["updates", "messages"], subgraphs=True):
                for e in translator.translate(ns, mode, data):
                    self._emit(rec, e)
                    self._working_memory(wm, e, translator, attempts)
                    phase = self._phase_for(e)
                    if phase and self._advance(run_id, phase):
                        self._emit(rec, RunEvent(run_id, "state", "worker", str(phase)))
                if attempts.total_failures != failures_seen:          # TUI shows "retry n/3"
                    failures_seen = attempts.total_failures
                    s.runs.update(run_id, attempts=failures_seen)
                if translator.interrupts:
                    break
                if run_id in self._cancelled:
                    raise Cancelled(run_id)
                if time.monotonic() > deadline:
                    raise TimeLimitExceeded(f"{minutes} minutes")
            saved = capture_changes(handle.backend, base_sha)
            self._facts[run_id]["files"] = [x.path for x in saved]
            common = {"saved": to_payload(saved), "base_sha": base_sha, "triage": tr.model_dump(), **item.payload(),
                      "diff": unified_diff(mpath, base_sha, saved), "test_report":
                      asdict(attempts.verified or attempts.last_report) if (attempts.verified or attempts.last_report) else None}
            if translator.interrupts:
                gate = translator.interrupts[0]
                if gate["gate"] == "attempts_exhausted":
                    self._failure_lesson(item, tr.task_type, "tests still failing after 3 attempts", run_id)
                    return self._finish(rec, run_id, item, RunState.FAILED, "tests")
                entry = {"continue_fix": "continue_fix", "unexpected_command": "continue_with_decision"}[gate["gate"]]
                hid = s.handoffs.create(run_id, repo.full_name, item.key, entry,
                                        {**common, "gate": gate, "prior_failures": attempts.total_failures, "todos": translator.todos})
                return self._finish(rec, run_id, item, RunState.AWAITING_APPROVAL, entry, hid)
            fc = check_changes(to_file_changes(saved, mpath, base_sha), set(subagent_paths(translator.final_text.get("repo-explorer", ""))),
                               repo.protected_paths, tr.task_type, repo_had_no_tests=attempts.saw_no_tests)
            if not saved:                                           # the agents chose not to change anything: say why
                said = (translator.final_text.get("orchestrator", "") or "").strip().splitlines()
                reason = "no change made — " + (said[0][:200] if said else "the agents ended without a change")
                self._failure_lesson(item, tr.task_type, reason, run_id)
                return self._finish(rec, run_id, item, RunState.FAILED, reason)
            if not fc.ok:
                reason = "file_check: " + "; ".join(fc.problems)
                self._failure_lesson(item, tr.task_type, reason, run_id)
                return self._finish(rec, run_id, item, RunState.FAILED, reason[:300])
            rep = attempts.verified
            if rep is None or rep.outcome != "passed":
                self._failure_lesson(item, tr.task_type, "the change was not shown to pass its tests", run_id)
                return self._finish(rec, run_id, item, RunState.FAILED, "untested")
            hid = s.handoffs.create(run_id, repo.full_name, item.key, "publish",
                                    {**common, "draft": self._draft(handle.shared_dir, item, tr), "flags": fc.flags,
                                     "files": [x.path for x in saved], "orchestrator_summary": translator.final_text.get("orchestrator", "")})
            return self._finish(rec, run_id, item, RunState.AWAITING_APPROVAL, "publish", hid)
        except TriageError as e:
            return self._finish(rec, run_id, item, RunState.FAILED, f"triage: {redact(str(e))[:200]}")
        except SandboxSetupError as e:
            s.audit.log("worker", "run_error", run_id=run_id, result="error",
                        detail={"type": "SandboxSetupError", "output_tail": redact(str(e))[-2000:]})   # the cause is at the end
            return self._finish(rec, run_id, item, RunState.FAILED, setup_reason(e))
        except (ModelCallLimitExceededError, ToolCallLimitExceededError):
            return self._finish(rec, run_id, item, RunState.FAILED, "step_limit")
        except TimeLimitExceeded:
            return self._finish(rec, run_id, item, RunState.FAILED, "time_limit")
        except Cancelled:
            return self._finish(rec, run_id, item, RunState.SKIPPED, "by_user")
        except Exception as e:                                       # noqa: BLE001 — every run must end in a state
            lost = kind == "daytona" and type(e).__module__.split(".")[0] in ("daytona", "daytona_api_client", "daytona_toolbox_api_client")
            s.audit.log("worker", "run_error", run_id=run_id, result="error", detail={"type": type(e).__name__, "msg": redact(str(e))[:500]})
            if lost:
                return self._finish(rec, run_id, item, RunState.FAILED, "sandbox_lost", reclaim_local=True)
            return self._finish(rec, run_id, item, RunState.FAILED, f"error: {type(e).__name__}")
        finally:
            st = s.settings
            s.runs.update(run_id, tokens_in=translator.tokens_in, tokens_out=translator.tokens_out,
                          cost_usd=cost_usd(translator.tokens_in, translator.tokens_out, st.price_in_per_mtok, st.price_out_per_mtok) or 0)
            if handle is not None:
                try:
                    provider.delete(handle)
                except Exception as e:                               # noqa: BLE001 — the sweep removes it later
                    s.audit.log("worker", "sandbox_delete_failed", run_id=run_id, result="error", detail={"msg": redact(str(e))[:200]})

    def _trace_config(self, run_id: str, item, kind: str) -> dict:
        """LangSmith config for model calls made outside the agent graph (triage, plan): same run_id, so they count in the run."""
        return {"callbacks": [self.s.tracer] if self.s.tracer else [], "run_name": kind,
                "metadata": {"run_id": run_id, "issue": item.key, "repo": item.repo.full_name, "kind": kind}}

    def recover(self) -> list[str]:
        """Startup (spec §4.2): in-flight runs → FAILED(crashed), every leftover sandbox deleted."""
        ids = self.s.runs.recover_crashed()
        self.s.handoffs.reopen_interrupted()
        for rid in ids:
            row = self.s.runs.get(rid)
            self.s.episodic.record_run(row["repo"], rid, row["issue_key"], "FAILED", "crashed", row["title"])
        for kind, p in self.s.providers.items():
            try:
                p.sweep(set())
            except Exception as e:                       # noqa: BLE001 — e.g. no Daytona key in local mode: startup goes on
                self.s.audit.log("worker", "sweep_skipped", result="error", target=kind, detail={"type": type(e).__name__})
        return ids
