# tests/test_changes.py
import os
import subprocess
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend

from codepilot.changes import capture_changes, apply_changes, from_payload, to_file_changes, to_payload, unified_diff


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_run(tmp_path: Path, name: str, mirror: Path, sha: str):
    run = tmp_path / name
    run.mkdir()
    git(tmp_path, "clone", "-q", str(mirror), str(run / "sandbox"))
    git(run / "sandbox", "checkout", "-q", "--detach", sha)
    backend = CompositeBackend(
        default=LocalShellBackend(root_dir=run, virtual_mode=True, env={"PATH": os.environ["PATH"]}),
        routes={"/sandbox/": FilesystemBackend(root_dir=run / "sandbox", virtual_mode=True)})
    return run / "sandbox", backend


def make_mirror(tmp_path: Path):
    m = tmp_path / "mirror"
    m.mkdir()
    (m / "a.py").write_text("a = 1\n"); (m / "b.py").write_text("b = 1\n"); (m / "run.sh").write_text("echo hi\n")
    os.symlink("a.py", m / "link.py")
    git(m, "init", "-q", "-b", "main"); git(m, "add", ".")
    git(m, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return m, git(m, "rev-parse", "HEAD").strip()


def edit_everything(repo: Path):
    (repo / "a.py").write_text("a = 2\n")
    (repo / "b.py").unlink()
    (repo / "run.sh").chmod(0o755)
    (repo / "pkg").mkdir(); (repo / "pkg" / "c.py").write_text("c = 1\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01\xff")
    (repo / "link.py").unlink(); os.symlink("pkg/c.py", repo / "link.py")
    (repo / ".pytest_cache").mkdir(); (repo / ".pytest_cache" / "x").write_text("junk")
    (repo / "pkg" / "__pycache__").mkdir(); (repo / "pkg" / "__pycache__" / "c.pyc").write_bytes(b"junk")


def test_capture_every_kind_of_change_and_skip_junk(tmp_path):
    mirror, sha = make_mirror(tmp_path)
    repo, backend = make_run(tmp_path, "run1", mirror, sha)
    edit_everything(repo)
    saved = {s.path: s for s in capture_changes(backend, sha)}
    assert set(saved) == {"a.py", "b.py", "run.sh", "pkg/c.py", "bin.dat", "link.py"}
    assert saved["a.py"].status == "M" and saved["a.py"].content() == b"a = 2\n"
    assert saved["b.py"].status == "D" and saved["b.py"].content_b64 is None
    assert saved["run.sh"].mode == "100755" and saved["run.sh"].content() == b"echo hi\n"
    assert saved["pkg/c.py"].status == "A"
    assert saved["bin.dat"].content() == b"\x00\x01\xff"
    assert saved["link.py"].mode == "120000" and saved["link.py"].content() == b"pkg/c.py"


def test_file_changes_take_old_text_from_mirror_and_handle_binary(tmp_path):
    mirror, sha = make_mirror(tmp_path)
    repo, backend = make_run(tmp_path, "run1", mirror, sha)
    edit_everything(repo)
    saved = from_payload(to_payload(capture_changes(backend, sha)))          # JSON round trip
    fcs = {f.path: f for f in to_file_changes(saved, mirror, sha)}
    assert fcs["a.py"].old_text == "a = 1\n" and fcs["a.py"].new_text == "a = 2\n"
    assert fcs["b.py"].old_text == "b = 1\n" and fcs["b.py"].new_text is None
    assert fcs["bin.dat"].new_text is None and fcs["pkg/c.py"].old_text is None
    d = unified_diff(mirror, sha, saved)
    assert "-a = 1" in d and "+a = 2" in d and "Binary or unreadable file A: bin.dat" in d and "Mode change 100755: run.sh" in d


def test_apply_reproduces_the_same_change_in_a_fresh_sandbox(tmp_path):
    mirror, sha = make_mirror(tmp_path)
    repo1, b1 = make_run(tmp_path, "run1", mirror, sha)
    edit_everything(repo1)
    saved = capture_changes(b1, sha)
    repo2, b2 = make_run(tmp_path, "run2", mirror, sha)
    apply_changes(b2, saved)
    again = capture_changes(b2, sha)
    assert sorted(map(repr, again)) == sorted(map(repr, saved))


def test_junk_staged_by_an_agent_is_still_excluded(tmp_path):
    mirror, sha = make_mirror(tmp_path)
    repo, backend = make_run(tmp_path, "run1", mirror, sha)
    (repo / ".pytest_cache").mkdir(); (repo / ".pytest_cache" / "x").write_text("junk")
    (repo / "a.py").write_text("a = 3\n")
    git(repo, "add", "-f", ".")
    assert [s.path for s in capture_changes(backend, sha)] == ["a.py"]


def test_no_change_gives_empty_list(tmp_path):
    mirror, sha = make_mirror(tmp_path)
    _, backend = make_run(tmp_path, "run1", mirror, sha)
    assert capture_changes(backend, sha) == []
