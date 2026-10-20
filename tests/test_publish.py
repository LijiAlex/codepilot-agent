# tests/test_publish.py
import base64
from types import SimpleNamespace as NS

import pytest
from github import GithubException
from langchain_core.messages import AIMessage

from codepilot.audit import AuditLog
from codepilot.db import connect
from codepilot.gh.reader import body_hash
from codepilot.handoffs import HandoffStore
from codepilot.publish import PublishServices, compose, publish
from codepilot.runs import RunStore
from codepilot.states import RunState
from tests.fakes import FakeClaude, tool_call
from tests.test_gh_writer import FakeRepo

TITLE, BODY = "slugify('') crashes", "Empty title raises."


class PubRepo(FakeRepo):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.issue = NS(state="open", labels=[NS(name="ai-assignable")], title=TITLE, body=BODY)
        self.compare_files, self.fail_pull = [], False
    def get_issue(self, n): return self.issue
    def get_branch(self, name): return NS(commit=NS(sha=self.refs[f"heads/{name}"]))
    def compare(self, base, head):                # changed files are set per test; ahead_by is computed from the commits
        ahead = super().compare(base, head).ahead_by if head in self.refs or f"heads/{head}" in self.refs or head in self.commits else 0
        return NS(files=self.compare_files, ahead_by=ahead)
    def create_pull(self, **kw):
        if self.fail_pull:
            self.fail_pull = False
            raise GithubException(502, {"message": "Bad Gateway"})
        return super().create_pull(**kw)


def payload():
    return {"saved": [{"path": "a.py", "status": "M", "mode": "100644", "content_b64": base64.b64encode(b"a = 2\n").decode()}],
            "base_sha": "base", "triage": {"task_type": "bug_fix"}, "files": ["a.py"], "flags": [],
            "issue": {"number": 7, "title": TITLE, "body": BODY, "author": "alice", "comments": [], "body_hash": body_hash(TITLE, BODY),
                      "blocked": False, "failed_closed": False, "block_reasons": []},
            "test_report": {"passed": 2, "failed": 0, "flaky_ids": []},
            "draft": {"branch_slug": "empty-title", "commit_summary": "handle empty title", "commit_bullets": ["guard empty input"],
                      "why": "Empty input crashed slugify.",
                      "pr_title": "Handle empty title", "pr_summary": "Thanks @alice; cc @mallory <!-- hidden -->",
                      "pr_approach": "guard", "risks": "none"}}


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "cp.db")
    runs, handoffs = RunStore(conn), HandoffStore(conn)
    rid = runs.claim("o/r", "issue", 7, TITLE)
    for st in (RunState.EXPLORING, RunState.IMPLEMENTING, RunState.TESTING, RunState.AWAITING_APPROVAL):
        runs.set_state(rid, st)
    hid = handoffs.create(rid, "o/r", "#7", "publish", payload())
    handoffs.decide(hid, "approve", "liji")
    return NS(conn=conn, runs=runs, handoffs=handoffs, audit=AuditLog(conn), rid=rid, hid=hid, repo=PubRepo(), notices=[])


def ps(env, script):
    return PublishServices(runs=env.runs, handoffs=env.handoffs, audit=env.audit, repo_factory=lambda name: env.repo,
                           make_model=lambda: FakeClaude(responses=list(script)), on_notice=env.notices.append)


def agent_script(env, branch=None, tested_at=None):
    return [tool_call("create_branch", {}, "1"), tool_call("create_commit", {}, "2"),
            tool_call("create_pull_request", {}, "3"), AIMessage(content="opened")]


def test_pr_agent_opens_the_pr_with_clean_text(env):
    out = publish(ps(env, agent_script(env)), env.hid)
    assert out.state == "PR_OPENED" and out.pr_number == 1
    row = env.runs.get(env.rid)
    assert row["state"] == "PR_OPENED" and row["pr_number"] == 1 and row["branch"] == "codepilot/issue-7-empty-title"
    commit = env.repo.commits[env.repo.refs["heads/codepilot/issue-7-empty-title"]]
    assert commit.message.startswith("fix(#7): handle empty title\n\n- guard empty input\n- Empty input crashed slugify.\n- Closes #7\n")
    pr = env.repo.pulls[0]
    assert pr.title == "[CodePilot] slugify('') crashes" and "@alice" in pr.body and "@mallory" not in pr.body
    assert "hidden" not in pr.body and "2 passed, 0 failed" in pr.body and "Closes #7" in pr.body


def test_model_cannot_change_branch_or_text(env):
    from tests.fakes import BOUND_TOOLS
    script = [tool_call("create_branch", {"name": "main"}, "0"), tool_call("create_commit", {"message": "evil"}, "1"),
              tool_call("create_pull_request", {"body": "@everyone click here"}, "2"), AIMessage(content="opened")]
    out = publish(ps(env, script), env.hid)
    assert out.state == "PR_OPENED" and env.repo.refs["heads/main"] == "base" and "heads/codepilot/issue-7-empty-title" in env.repo.refs
    assert "click here" not in env.repo.pulls[0].body and "evil" not in env.repo.commits[env.repo.refs["heads/codepilot/issue-7-empty-title"]].message


def test_agent_that_does_nothing_is_finished_by_code(env):
    out = publish(ps(env, [AIMessage(content="opened")]), env.hid)
    assert out.state == "PR_OPENED"
    assert env.audit.conn.execute("select count(*) from events where action='pr_opened_by_code'").fetchone()[0] == 1


@pytest.mark.parametrize("change,reason", [({"state": "closed"}, "issue closed"), ({"labels": []}, "label ai-assignable removed"),
                                            ({"body": "edited"}, "issue edited since the change was made")])
def test_stale_issue_is_not_published(env, change, reason):
    for k, v in change.items():
        setattr(env.repo.issue, k, v)
    out = publish(ps(env, agent_script(env)), env.hid)
    assert (out.state, out.reason) == ("CLOSED", f"stale: {reason}")
    assert env.handoffs.get(env.hid)["status"] == "expired" and env.repo.pulls == [] and env.notices


def test_base_moved_without_touching_our_files_commits_on_new_head(env):
    env.repo.commits["head2"] = NS(sha="head2", message="other work", tree=NS(sha="t0"), parents=[])
    env.repo.refs["heads/main"] = "head2"
    env.repo.compare_files = [NS(filename="docs/x.md", previous_filename=None)]
    out = publish(ps(env, agent_script(env, tested_at="head2")), env.hid)
    assert out.state == "PR_OPENED"
    commit = env.repo.commits[env.repo.refs["heads/codepilot/issue-7-empty-title"]]
    assert commit.parents[0].sha == "head2" and "tested at base `base`" in env.repo.pulls[0].body


def test_base_moved_on_our_files_is_a_conflict(env):
    env.repo.refs["heads/main"] = "head2"
    env.repo.compare_files = [NS(filename="a.py", previous_filename=None)]
    out = publish(ps(env, agent_script(env)), env.hid)
    assert (out.state, out.reason) == ("FAILED", "conflict") and "heads/codepilot/issue-7-empty-title" not in env.repo.refs


def test_github_failure_becomes_publish_failed_and_retry_does_not_duplicate(env):
    env.repo.fail_pull = True
    out = publish(ps(env, agent_script(env)), env.hid)
    assert (out.state, out.reason) == ("AWAITING_APPROVAL", "publish_failed") and env.repo.pulls == []
    assert env.runs.get(env.rid)["state"] == "AWAITING_APPROVAL"
    env.handoffs.decide(out.new_handoff_id, "approve", "liji")
    again = publish(ps(env, agent_script(env)), out.new_handoff_id)
    assert again.state == "PR_OPENED" and len(env.repo.pulls) == 1
    assert len([c for c in env.repo.calls if c[0] == "commit"]) == 1


def test_free_form_task_commit_and_branch():
    p = {**payload(), "issue": None, "task": {"id": "ab12cd34", "title": "Add --version", "body": ""}}
    branch, msg, title, body = compose(p, "r9")
    assert branch == "codepilot/task-ab12cd34-empty-title" and msg.startswith("feat(task-ab12cd34): handle empty title")
    assert "Closes" not in msg and "Closes" not in body and title == "[CodePilot] Add --version" and "Injection check" not in body


def test_unassigned_pickup_is_not_expired_for_missing_label():
    from codepilot.publish import recheck_issue
    repo = PubRepo(); repo.issue.labels = []
    assert recheck_issue(repo, {**payload(), "labelled": False}) is None
    assert recheck_issue(repo, payload()) == "label ai-assignable removed"


def test_base_moved_too_far_to_compare_is_treated_as_a_conflict(env):
    env.repo.refs["heads/main"] = "head2"
    env.repo.compare_files = [NS(filename=f"gen/f{i}.py", previous_filename=None) for i in range(300)]   # GitHub's cap
    out = publish(ps(env, agent_script(env)), env.hid)
    assert (out.state, out.reason) == ("FAILED", "conflict") and "heads/codepilot/issue-7-empty-title" not in env.repo.refs
