# tests/test_file_check.py
from codepilot.safety.file_check import FileChange, check_changes
from codepilot.config import DEFAULT_PROTECTED

def fc(path, status="M", old="", new=""):
    return FileChange(path=path, status=status, mode="100644", old_text=old, new_text=new)

def test_empty_diff_fails():
    assert not check_changes([], set(), DEFAULT_PROTECTED, "bug_fix").ok

def test_protected_path_fails():
    r = check_changes([fc(".github/workflows/ci.yml")], {".github/workflows/ci.yml"}, DEFAULT_PROTECTED, "config_change")
    assert not r.ok and any("protected" in p for p in r.problems)

def test_weakened_test_detected():
    old = "def test_a():\n    assert f() == 1\n"
    new = "import pytest\n@pytest.mark.skip\ndef test_a():\n    pass\n"
    r = check_changes([fc("tests/test_a.py", old=old, new=new), fc("pkg/a.py")], {"pkg/a.py", "tests/test_a.py"}, DEFAULT_PROTECTED, "bug_fix")
    assert not r.ok and any("weakened" in p for p in r.problems)

def test_unexpected_path_flagged_and_more_than_five():
    changes = [fc(f"pkg/m{i}.py") for i in range(6)]
    r = check_changes(changes, {"pkg/m0.py"}, DEFAULT_PROTECTED, "feature_addition")
    assert "more_than_5_files" in r.flags and any(f.startswith("unexpected_file:") for f in r.flags) and r.ok

def test_dependency_manifest_allowed_for_dependency_task():
    r = check_changes([fc("pyproject.toml", old="a==1", new="a==2")], set(), DEFAULT_PROTECTED, "dependency_update")
    assert r.ok and "adds_dependency" not in r.flags


def test_docs_allowed_for_documentation_and_feature_and_no_tests_flag():
    ch = [FileChange("README.md", "M", "100644", "a", "b")]
    assert check_changes(ch, {"pkg/a.py"}, [], "documentation").flags == []
    assert check_changes(ch, {"pkg/a.py"}, [], "bug_fix").flags == ["unexpected_file:README.md"]
    assert "no_tests_in_repo" in check_changes(ch, set(), [], "documentation", repo_had_no_tests=True).flags


def test_protected_globs_match_at_repo_root():
    for p in (".env", "server.pem", "credentials.json"):
        r = check_changes([fc(p)], {p}, DEFAULT_PROTECTED, "bug_fix")
        assert not r.ok and any("protected path" in m for m in r.problems), p
