# tests/test_gh_writer.py
"""GitWriter against an in-memory GitHub that applies trees like the real git-data API
(base_tree + elements; sha=None deletes). Request shapes use real InputGitTreeElement."""
import base64
from types import SimpleNamespace as NS

import pytest
from github import GithubException

from codepilot.changes import SavedFile
from codepilot.gh.writer import GitWriter, PublishError, validate_branch, validate_message


def b64(s: bytes) -> str:
    return base64.b64encode(s).decode()


class FakePR:
    def __init__(self, repo, number, head, title, body, reviewer_ok=True):
        self.repo, self.number, self.head, self.title, self.body = repo, number, head, title, body
        self.html_url = f"https://github.com/o/r/pull/{number}"
        self.labels, self.reviewers, self.comments, self.reviewer_ok = set(), [], [], reviewer_ok
    def add_to_labels(self, *ls): self.labels.update(ls)
    def create_review_request(self, reviewers):
        if not self.repo.reviewer_ok:
            raise GithubException(422, {"message": "Reviews may only be requested from collaborators."})
        self.reviewers += reviewers
    def create_issue_comment(self, body): self.comments.append(body)


class FakeRepo:
    full_name, default_branch = "o/r", "main"

    def __init__(self, reviewer_ok=True):
        self.reviewer_ok = reviewer_ok
        self.blobs, self.trees, self.commits, self.refs, self.pulls, self.labels = {}, {}, {}, {}, [], {"ai-assignable"}
        self.calls = []
        self.trees["t0"] = {"a.py": ("100644", "blob-a"), "b.py": ("100644", "blob-b")}
        self.commits["base"] = NS(sha="base", message="init", tree=NS(sha="t0"), parents=[])
        self.refs["heads/main"] = "base"

    def _ref(self, name):
        repo = self
        return NS(ref=f"refs/{name}", object=NS(sha=self.refs[name]), edit=lambda sha, force=False: repo.refs.__setitem__(name, sha))

    def get_git_ref(self, ref):
        if ref not in self.refs:
            raise GithubException(404, {"message": "Not Found"})
        return self._ref(ref)

    def create_git_ref(self, ref, sha):
        name = ref.removeprefix("refs/")
        if name in self.refs:
            raise GithubException(422, {"message": "Reference already exists"})
        self.refs[name] = sha; self.calls.append(("ref", name)); return self._ref(name)

    def get_git_commit(self, sha): return self.commits[sha]

    def create_git_blob(self, content, encoding):
        assert encoding == "base64"; base64.b64decode(content, validate=True)
        sha = f"blob{len(self.blobs)}"; self.blobs[sha] = content; return NS(sha=sha)

    def create_git_tree(self, tree, base_tree):
        files = dict(self.trees[base_tree.sha])
        for el in tree:
            ident = el._identity                      # exactly what PyGithub sends
            if ident["sha"] is None:
                assert ident["path"] in files, "GitHub rejects deleting a missing path"
                files.pop(ident["path"])
            else:
                files[ident["path"]] = (ident["mode"], ident["sha"])
        sha = f"t{len(self.trees)}"; self.trees[sha] = files; return NS(sha=sha)

    def create_git_commit(self, message, tree, parents):
        sha = f"c{len(self.commits)}"
        self.commits[sha] = NS(sha=sha, message=message, tree=tree, parents=parents); self.calls.append(("commit", sha)); return self.commits[sha]

    def get_pulls(self, state, head):
        return [p for p in self.pulls if f"o:{p.head}" == head]

    def create_pull(self, base, head, title, body):
        pr = FakePR(self, len(self.pulls) + 1, head, title, body); self.pulls.append(pr); self.calls.append(("pull", pr.number)); return pr

    def get_labels(self): return [NS(name=n) for n in self.labels]
    def create_label(self, name, color): self.labels.add(name)


SAVED = [SavedFile("a.py", "M", "100644", b64(b"a = 2\n")), SavedFile("b.py", "D", "000000", None),
         SavedFile("run.sh", "A", "100755", b64(b"echo\n")), SavedFile("img.bin", "A", "100644", b64(b"\x00\xff"))]
MSG = "fix(#7): handle empty title\n\n- strip input\n\nCloses #7"


def writer(repo, saved=SAVED, run_id="r1", reporter="alice"):
    return GitWriter(repo, run_id=run_id, issue_number=7, reporter=reporter, saved=saved, base_sha="base")


def publish(w, branch="codepilot/issue-7-empty-title"):
    w.create_branch(branch)
    sha = w.create_commit(branch, MSG)
    return sha, w.create_pull_request(branch, "[CodePilot] Handle empty title", "body")


def test_one_commit_with_all_changes_labels_and_review_request():
    repo = FakeRepo()
    sha, (num, url, requested) = publish(writer(repo))
    files = repo.trees[repo.commits[sha].tree.sha]
    assert set(files) == {"a.py", "run.sh", "img.bin"} and files["run.sh"][0] == "100755"
    assert base64.b64decode(repo.blobs[files["img.bin"][1]]) == b"\x00\xff"
    assert repo.commits[sha].parents[0].sha == "base" and "CodePilot-Run: r1" in repo.commits[sha].message
    assert repo.refs["heads/codepilot/issue-7-empty-title"] == sha
    pr = repo.pulls[0]
    assert {"codepilot-generated", "needs-review", "codepilot-review-pending"} <= pr.labels and {"codepilot-generated", "needs-review", "codepilot-review-pending"} <= repo.labels
    assert requested and pr.reviewers == ["alice"] and num == 1


def test_retry_is_idempotent():
    repo = FakeRepo()
    first = publish(writer(repo))
    second = publish(writer(repo))
    assert first == second
    assert [c for c in repo.calls if c[0] != "ref"] == [("commit", "c1"), ("pull", 1)]


def test_non_collaborator_reporter_gets_mention_once():
    repo = FakeRepo(reviewer_ok=False)
    _, (_, _, requested) = publish(writer(repo))
    publish(writer(repo))
    assert not requested and repo.pulls[0].comments == ["@alice CodePilot+ opened this PR for your issue; please review."]


def test_branch_with_another_runs_commit_is_refused():
    repo = FakeRepo()
    publish(writer(repo, run_id="r1"))
    with pytest.raises(PublishError, match="another run"):
        writer(repo, run_id="r2").create_commit("codepilot/issue-7-empty-title", MSG)


@pytest.mark.parametrize("name", ["main", "codepilot/issue-8-x", "codepilot/issue-7-Bad_Slug", "codepilot/issue-7-" + "a" * 80, "feature/x"])
def test_bad_branch_names(name):
    with pytest.raises(PublishError):
        validate_branch(name, 7)


@pytest.mark.parametrize("msg", ["fixed stuff\n\nCloses #7\n\nCodePilot-Run: r1", "fix(#8): x\n\nCloses #7\n\nCodePilot-Run: r1",
                                 "feat(#7): x\n\n- Closes #7\n\nCodePilot-Run: r1",
                                 "fix(#7): x\n\nCodePilot-Run: r1", "fix(#7): x\n\nCloses #7"])
def test_bad_messages(msg):
    with pytest.raises(PublishError):
        validate_message(msg, 7, "r1")


def test_bad_title_and_empty_change():
    repo = FakeRepo()
    w = writer(repo)
    w.create_branch("codepilot/issue-7-x")
    with pytest.raises(PublishError):
        w.create_pull_request("codepilot/issue-7-x", "Handle empty title", "b")
    with pytest.raises(PublishError, match="empty"):
        writer(repo, saved=[]).create_commit("codepilot/issue-7-x", MSG)
