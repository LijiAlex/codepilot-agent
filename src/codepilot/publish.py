# src/codepilot/publish.py
"""After a person approves: code re-checks the issue and the base branch, then the PR Agent opens the PR
with the fixed tools (create_branch, create_commit, create_pull_request). Idempotent; never auto-resolves conflicts."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from github import GithubException
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.tools import StructuredTool

from codepilot.changes import from_payload
from codepilot.gh.reader import LABEL, body_hash
from codepilot.gh.writer import GitWriter, PublishError, run_trailer
from codepilot.safety.allowlist import make_tool_allowlist
from codepilot.safety.sanitize import sanitize_untrusted
from codepilot.states import RunState

COMPARE_FILE_CAP = 300
PUBLISH_TOOLS = {"create_branch", "create_commit", "create_pull_request"}

PUBLISH_PROMPT = """You are the PR Agent. A person approved this change. Open the pull request with your three tools, in
order: create_branch, create_commit, create_pull_request. They take no arguments: the branch, commit and PR text were
fixed when the change was approved. If a tool reports a problem, stop and reply with it. Reply "opened" when done."""


@dataclass
class PublishServices:
    runs: Any
    handoffs: Any
    audit: Any
    repo_factory: Callable[[str], Any]          # "owner/name" → PyGithub Repository (repo-scoped token)
    make_model: Callable[[], Any]
    on_notice: Callable[[str], None] = lambda text: None


@dataclass
class PublishOutcome:
    state: str
    reason: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    new_handoff_id: str | None = None
    notes: list[str] = field(default_factory=list)


def _clean(text: str, reporter: str | None) -> str:
    text = sanitize_untrusted(text or "", 6000)
    return re.sub(r"@([A-Za-z0-9][A-Za-z0-9-]*)", lambda m: m.group(0) if m.group(1) == reporter else m.group(1), text)


def compose(payload: dict, run_id: str, tested_at: str | None = None) -> tuple[str, str, str, str]:
    d, tr = payload["draft"], payload["triage"]
    issue = payload.get("issue")
    reporter = issue["author"] if issue else None
    slug = d["branch_slug"]
    if issue:
        n = issue["number"]
        branch, ref = f"codepilot/issue-{n}-{slug}", f"#{n}"
    else:
        branch, ref = f"codepilot/task-{payload['task']['id']}-{slug}", f"task-{payload['task']['id']}"
    lines = [f"- {_clean(b, None)}" for b in d.get("commit_bullets", [])[:6]]
    if d.get("why"):
        lines.append(f"- {_clean(d['why'], None)}")
    if issue:
        lines.append(f"- Closes #{issue['number']}")
    summary = _clean(d["commit_summary"], None).replace("\n", " ")[:60]
    prefix = "fix" if issue else "feat"          # brief: fix(#N) for issues; spec §4.4: feat(task-ID) for free-form tasks
    message = f"{prefix}({ref}): {summary}\n\n" + "\n".join(lines) + f"\n\n{run_trailer(run_id)}"
    source_title = issue["title"] if issue else payload["task"]["title"]
    title = f"[CodePilot] {_clean(source_title, None).replace(chr(10), ' ')}"[:120]     # brief: [CodePilot] {issue title}
    rep = payload.get("test_report") or {}
    tests = f"{rep.get('passed', 0)} passed, {rep.get('failed', 0)} failed" + (f", flaky: {', '.join(rep['flaky_ids'])}" if rep.get("flaky_ids") else "")
    files = "\n".join(f"- `{f}`" for f in payload.get("files", []))
    flags = ", ".join(payload.get("flags", [])) or "none"
    body = (f"## Summary\n{_clean(d.get('pr_summary', ''), reporter)}\n\n## Approach\n{_clean(d.get('pr_approach', ''), reporter)}\n\n"
            f"## Files changed\n{files}\n\n## Test results\n{tests} (run in an isolated sandbox"
            + (f"; tested at base `{payload['base_sha'][:10]}`, committed on `{tested_at[:10]}`" if tested_at else "") + ")\n\n"
            f"## Pre-PR checks\n- File check: passed (flags: {flags})\n"
            + ("- Injection check on the issue: passed\n" if issue else "") + "- Approved by a person in the CodePilot+ TUI\n\n"
            f"## Risks\n{_clean(d.get('risks', ''), reporter)}\n\n"
            + (f"Closes #{issue['number']}\n" if issue else "") + f"\n<sub>CodePilot+ run `{run_id}`</sub>")
    return branch, message, title, body


def recheck_issue(repo, payload: dict) -> str | None:
    issue = payload.get("issue")
    if not issue:
        return None                                    # free-form task: nothing to re-check (E7)
    gh = repo.get_issue(issue["number"])
    if gh.state != "open":
        return "issue closed"
    if payload.get("labelled", True) and LABEL not in {label.name for label in gh.labels}:     # unassigned pick-ups had no label
        return f"label {LABEL} removed"
    if body_hash(gh.title or "", gh.body or "") != issue["body_hash"]:
        return "issue edited since the change was made"
    return None


def publish(ps: PublishServices, handoff_id: str) -> PublishOutcome:
    h = ps.handoffs.get(handoff_id)
    payload, run_id = ps.handoffs.payload(handoff_id), h["run_id"]
    repo = ps.repo_factory(h["repo"])
    stale = recheck_issue(repo, payload)
    if stale:
        ps.handoffs.mark(handoff_id, "expired")
        ps.runs.set_state(run_id, RunState.CLOSED, f"stale: {stale}")
        ps.on_notice(f"{h['repo']} {h['issue_key']}: not published — {stale}")
        return PublishOutcome("CLOSED", f"stale: {stale}")
    base = payload["base_sha"]
    head = repo.get_branch(repo.default_branch).commit.sha
    start, tested_at = base, None
    if head != base:
        moved = set()
        files = repo.compare(base, head).files
        for f in files:
            moved.add(f.filename)
            if f.previous_filename:
                moved.add(f.previous_filename)
        touched = {s["path"] for s in payload["saved"]}
        if len(files) >= COMPARE_FILE_CAP:                  # GitHub lists only the first 300: overlap cannot be ruled out
            moved |= touched
        if moved & touched:
            ps.runs.set_state(run_id, RunState.FAILED, "conflict")
            ps.on_notice(f"{h['repo']} {h['issue_key']}: main changed {sorted(moved & touched)} since the run — not published")
            return PublishOutcome("FAILED", "conflict")
        start, tested_at = head, head                  # E2: base moved, touched files unchanged
    branch, message, title, body = compose(payload, run_id, tested_at)
    issue = payload.get("issue")
    writer = GitWriter(repo, run_id=run_id, issue_number=issue["number"] if issue else None,
                       reporter=issue["author"] if issue else None, saved=from_payload(payload["saved"]), base_sha=start)
    result: dict[str, Any] = {}

    # The tools take no arguments (technical review S10): the model decides only *whether* to call them; the branch,
    # commit message, title and body are the composed, sanitised values the person approved.
    def create_branch() -> str:
        """Create the branch for this change (from the approved base commit)."""
        try:
            return f"branch {branch} ready at {writer.create_branch(branch, start)[:10]}"
        except PublishError as e:
            return f"rejected: {e}"

    def create_commit() -> str:
        """Commit the approved change to the branch, as one commit."""
        try:
            return f"commit {writer.create_commit(branch, message)[:10]}"
        except PublishError as e:
            return f"rejected: {e}"

    def create_pull_request() -> str:
        """Open the pull request to the default branch with labels and a review request."""
        try:
            n, url, requested = writer.create_pull_request(branch, title, body)
            result.update(number=n, url=url, requested=requested)
            return f"opened {url}"
        except PublishError as e:
            return f"rejected: {e}"

    tools = [StructuredTool.from_function(f) for f in (create_branch, create_commit, create_pull_request)]
    try:
        agent = create_agent(ps.make_model(), tools, system_prompt=PUBLISH_PROMPT, name="pr-agent",
                             middleware=[make_tool_allowlist(PUBLISH_TOOLS, "pr-agent", ps.audit, run_id),
                                         ModelCallLimitMiddleware(run_limit=12, exit_behavior="end")])
        try:
            agent.invoke({"messages": [{"role": "user", "content": f"Open the approved pull request for {h['issue_key']} ({branch})."}]})
        except GithubException:
            raise
        except Exception as e:                          # noqa: BLE001 — model trouble: code finishes the job (E3)
            ps.audit.log("worker", "pr_agent_error", run_id=run_id, result="error", detail={"type": type(e).__name__})
        if "number" not in result:                      # agent ended without opening the PR: code calls the tools (E3)
            writer.create_branch(branch, start)
            writer.create_commit(branch, message)
            n, url, requested = writer.create_pull_request(branch, title, body)
            result.update(number=n, url=url, requested=requested)
            ps.audit.log("worker", "pr_opened_by_code", run_id=run_id, target=url)
    except (GithubException, PublishError) as e:
        hid = ps.handoffs.create(run_id, h["repo"], h["issue_key"], "publish_failed", {**payload, "error": str(e)[:500]})
        ps.on_notice(f"{h['repo']} {h['issue_key']}: publishing failed ({type(e).__name__}); approve again to retry")
        return PublishOutcome("AWAITING_APPROVAL", "publish_failed", new_handoff_id=hid)
    ps.runs.update(run_id, branch=branch, pr_number=result["number"], pr_url=result["url"])
    ps.runs.set_state(run_id, RunState.PR_OPENED)
    ps.audit.log("pr-agent", "pr_opened", run_id=run_id, target=result["url"], detail={"review_requested": result["requested"]})
    notes = [] if result["requested"] else ["reporter is not a collaborator: mentioned in a comment instead"]
    return PublishOutcome("PR_OPENED", None, result["number"], result["url"], notes=notes)
