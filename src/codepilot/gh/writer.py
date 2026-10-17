# src/codepilot/gh/writer.py
"""The PR Agent's fixed GitHub tools. They replace the Toolkit write tools:
one multi-file commit built from the saved, approved change set (the agent passes
text, never file contents), validated names, labels and review request.
Every call is idempotent, so a retried publish never duplicates a branch, commit or PR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from github import GithubException, InputGitTreeElement

from codepilot.changes import SavedFile

BRANCH_RE = re.compile(r"^codepilot/(issue-\d+|task-[0-9a-z]{4,12})-[a-z0-9]+(-[a-z0-9]+)*$")
SUBJECT_RE = re.compile(r"^(fix|feat|docs|chore|build|test|refactor)\((#\d+|task-[0-9a-z]{4,12})\): \S.{0,71}$")
PR_LABELS = ["codepilot-generated", "needs-review"]
LABEL_COLORS = {"codepilot-generated": "5319e7", "needs-review": "fbca04", "ai-assignable": "0e8a16"}


class PublishError(ValueError):
    pass


def run_trailer(run_id: str) -> str:
    return f"CodePilot-Run: {run_id}"


def validate_branch(name: str, issue_number: int | None) -> None:
    if len(name) > 80 or not BRANCH_RE.match(name):
        raise PublishError(f"branch name must look like codepilot/issue-N-short-slug: {name!r}")
    if issue_number is not None and not name.startswith(f"codepilot/issue-{issue_number}-"):
        raise PublishError(f"branch must be for issue #{issue_number}: {name!r}")


def validate_message(message: str, issue_number: int | None, run_id: str) -> None:
    lines = message.strip().splitlines()
    if not lines or not SUBJECT_RE.match(lines[0]):
        raise PublishError("first line must be like 'fix(#12): short summary' (max 72 chars after the prefix)")
    if issue_number is not None and not lines[0].startswith(f"fix(#{issue_number}): "):
        raise PublishError(f"first line must start with 'fix(#{issue_number}): ' (brief format)")
    if issue_number is not None and f"Closes #{issue_number}" not in message:
        raise PublishError(f"message must contain 'Closes #{issue_number}'")
    if run_trailer(run_id) not in message:
        raise PublishError(f"message must end with '{run_trailer(run_id)}'")


@dataclass
class PublishResult:
    branch: str
    commit_sha: str
    pr_number: int
    pr_url: str
    review_requested: bool


class GitWriter:
    def __init__(self, repo, *, run_id: str, issue_number: int | None, reporter: str | None, saved: list[SavedFile], base_sha: str):
        self.repo, self.run_id, self.issue_number, self.reporter = repo, run_id, issue_number, reporter
        self.saved, self.base_sha = saved, base_sha

    # ---- tools -------------------------------------------------------------
    def create_branch(self, name: str, start_sha: str | None = None) -> str:
        validate_branch(name, self.issue_number)
        try:
            return self.repo.get_git_ref(f"heads/{name}").object.sha       # already there: reuse
        except GithubException as e:
            if e.status != 404:
                raise
        return self.repo.create_git_ref(f"refs/heads/{name}", start_sha or self.base_sha).object.sha

    def create_commit(self, branch: str, message: str) -> str:
        validate_branch(branch, self.issue_number)
        message = message.strip() + ("" if run_trailer(self.run_id) in message else f"\n\n{run_trailer(self.run_id)}")
        validate_message(message, self.issue_number, self.run_id)
        ref = self.repo.get_git_ref(f"heads/{branch}")
        parent = self.repo.get_git_commit(ref.object.sha)
        if run_trailer(self.run_id) in parent.message:
            return parent.sha                                                # this run's commit is already there
        if "CodePilot-Run: " in parent.message:
            raise PublishError(f"branch {branch} already holds another run's commit; use a new branch name")
        if not self.saved:
            raise PublishError("the saved change is empty")
        elements = []
        for s in self.saved:
            if s.status == "D":
                elements.append(InputGitTreeElement(s.path, "100644", "blob", sha=None))
            else:
                blob = self.repo.create_git_blob(s.content_b64, "base64")
                elements.append(InputGitTreeElement(s.path, s.mode, "blob", sha=blob.sha))
        tree = self.repo.create_git_tree(elements, base_tree=parent.tree)
        commit = self.repo.create_git_commit(message, tree, [parent])
        ref.edit(commit.sha)
        return commit.sha

    def create_pull_request(self, branch: str, title: str, body: str) -> tuple[int, str, bool]:
        validate_branch(branch, self.issue_number)
        if not title.startswith("[CodePilot] ") or len(title) > 120:
            raise PublishError("title must start with '[CodePilot] ' and be at most 120 characters")
        owner = self.repo.full_name.split("/")[0]
        existing = list(self.repo.get_pulls(state="open", head=f"{owner}:{branch}"))
        pr = existing[0] if existing else self.repo.create_pull(base=self.repo.default_branch, head=branch, title=title, body=body)
        self.ensure_labels(PR_LABELS)
        pr.add_to_labels(*PR_LABELS)
        requested = False
        if self.reporter:
            try:
                pr.create_review_request(reviewers=[self.reporter])
                requested = True
            except GithubException as e:
                if e.status != 422:              # 422: reporter is not a collaborator → @mention instead (pre-mortem E6)
                    raise
                if not existing:
                    pr.create_issue_comment(f"@{self.reporter} CodePilot+ opened this PR for your issue; please review.")
        return pr.number, pr.html_url, requested

    # ---- helpers -----------------------------------------------------------
    def ensure_labels(self, names: list[str]) -> None:
        ensure_labels(self.repo, names)


def ensure_labels(repo, names: list[str]) -> list[str]:
    """Create any missing labels; returns the ones created."""
    have = {label.name for label in repo.get_labels()}
    created = [n for n in names if n not in have]
    for n in created:
        repo.create_label(n, LABEL_COLORS.get(n, "ededed"))
    return created
