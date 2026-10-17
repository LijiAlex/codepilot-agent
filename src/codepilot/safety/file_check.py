# src/codepilot/safety/file_check.py
"""Code check run on the final change before it is offered for approval.

Runs no tests (proof of work is the Test Agent's job); checks what changed.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Literal

MANIFESTS = {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg", "setup.py", "package.json"}
LOCKFILES = {"uv.lock", "poetry.lock", "package-lock.json"}
_ASSERT = re.compile(r"^\s*assert\b", re.M)
_SKIP = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|unittest\.skip", re.M)
_TESTDEF = re.compile(r"^\s*def test_\w+", re.M)


@dataclass
class FileChange:
    path: str
    status: Literal["A", "M", "D"]
    mode: str
    old_text: str | None
    new_text: str | None


@dataclass
class FileCheckResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}"


DOC_FILES = ("README", "CHANGELOG", "CONTRIBUTING", "docs/")


def _is_doc(path: str) -> bool:
    return path.startswith(DOC_FILES) or path.endswith((".md", ".rst"))


def check_changes(changes: list[FileChange], expected: set[str], protected: list[str], task_type: str,
                  repo_had_no_tests: bool = False) -> FileCheckResult:
    """Problems fail the run; flags are shown on the approval item (spec §4.2 step 10, E6, E14)."""
    r = FileCheckResult(ok=True)
    if not changes:
        r.problems.append("the change is empty")
    if len(changes) > 5:
        r.flags.append("more_than_5_files")
    for c in changes:
        p = c.path
        allowed_manifest = task_type == "dependency_update" and (p in MANIFESTS or p in LOCKFILES)
        if p.startswith(("shared/", "conversation_history/", "large_tool_results/")):
            r.problems.append(f"working file must not be committed: {p}")
            continue
        if not allowed_manifest and any(fnmatch.fnmatch(p, pat) for pat in protected):
            r.problems.append(f"protected path changed: {p}")
            continue
        if p.rsplit("/", 1)[-1] in MANIFESTS and task_type != "dependency_update":
            r.flags.append("adds_dependency")
        skill_allows = _is_doc(p) and task_type in ("documentation", "feature_addition")      # E14: files the skill allows
        if expected and p not in expected and not _is_test(p) and not allowed_manifest and not skill_allows:
            r.flags.append(f"unexpected_file:{p}")
        if _is_test(p) and c.status in ("M", "D"):
            old, new = c.old_text or "", c.new_text or ""
            if c.status == "D" and _TESTDEF.search(old):
                r.problems.append(f"weakened tests: deleted {p}")
            elif len(_ASSERT.findall(new)) < len(_ASSERT.findall(old)) or (_SKIP.search(new) and not _SKIP.search(old)) \
                    or len(_TESTDEF.findall(new)) < len(_TESTDEF.findall(old)):
                r.problems.append(f"weakened tests in {p}")
    if repo_had_no_tests:
        r.flags.append("no_tests_in_repo")
    r.flags = sorted(set(r.flags))
    r.ok = not r.problems
    return r
