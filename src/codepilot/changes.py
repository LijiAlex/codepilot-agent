# src/codepilot/changes.py
"""The change set: what the agents changed in the sandbox, captured by code.

Captured once at proposal time and saved in the publish handoff, so the commit
is built from exactly what was tested and approved (the PR Agent cannot alter it).
Works through the run's CompositeBackend, so it is the same for both providers.
"""
from __future__ import annotations

import base64
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from codepilot.safety.file_check import FileChange

# Never part of a change, even if the repo's .gitignore forgets them.
JUNK = [".pytest_cache", "**/__pycache__", "**/*.pyc", ".venv", "**/*.egg-info", ".mypy_cache", ".ruff_cache"]
REGULAR, EXECUTABLE, SYMLINK = "100644", "100755", "120000"


class ChangeCaptureError(RuntimeError):
    pass


@dataclass
class SavedFile:
    path: str
    status: Literal["A", "M", "D"]
    mode: str                         # new mode; "000000" for deletions
    content_b64: str | None           # None for deletions; symlink = link target

    def content(self) -> bytes | None:
        return None if self.content_b64 is None else base64.b64decode(self.content_b64)


def parse_raw_z(out: str) -> list[tuple[str, str, str]]:
    """Parse `git diff --raw -z` → [(status, new_mode, path)]. Record: ':om nm os ns S\\0path\\0'."""
    parts = out.split("\0")
    res, i = [], 0
    while i < len(parts) - 1:
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        _, new_mode, _, _, status = meta[1:].split(" ")
        res.append((status[0], new_mode, parts[i + 1]))
        i += 2
    return res


def _stdout(output: str) -> str:
    return output.split("\n<stderr>")[0]      # langchain-daytona appends stderr this way


def capture_changes(backend, base_sha: str) -> list[SavedFile]:
    excludes = " ".join(shlex.quote(f":(exclude){j}") for j in JUNK)
    # reset first: anything staged earlier (e.g. an agent's `git add .`) must not bypass the junk filter
    r = backend.execute(f"cd sandbox && git reset -q && git add -A -- . {excludes} && "
                        f"git diff --cached --raw --no-renames -z {shlex.quote(base_sha)}")
    if r.exit_code != 0:
        raise ChangeCaptureError(f"git diff failed: {r.output[-1000:]}")
    entries = parse_raw_z(_stdout(r.output))
    saved: list[SavedFile] = []
    to_download = [(p, m) for s, m, p in entries if s != "D" and m in (REGULAR, EXECUTABLE)]
    got = backend.download_files([f"/sandbox/{p}" for p, _ in to_download]) if to_download else []
    by_path = {p: g for (p, _), g in zip(to_download, got)}
    for status, mode, path in entries:
        if status not in ("A", "M", "D"):
            raise ChangeCaptureError(f"unsupported change type {status} for {path}")
        if status == "D":
            saved.append(SavedFile(path, "D", "000000", None))
        elif mode == SYMLINK:
            t = backend.execute(f"cd sandbox && readlink {shlex.quote('./' + path)}")
            saved.append(SavedFile(path, status, mode, base64.b64encode(_stdout(t.output).strip().encode()).decode()))
        elif mode in (REGULAR, EXECUTABLE):
            d = by_path[path]
            if d.error or d.content is None:
                raise ChangeCaptureError(f"could not read {path}: {d.error}")
            saved.append(SavedFile(path, status, mode, base64.b64encode(d.content).decode()))
        else:
            raise ChangeCaptureError(f"unsupported file mode {mode} for {path}")  # e.g. submodule 160000
    return saved


def to_payload(saved: list[SavedFile]) -> list[dict]:
    return [asdict(s) for s in saved]


def from_payload(items: list[dict]) -> list[SavedFile]:
    return [SavedFile(**i) for i in items]


def _text(b: bytes | None) -> str | None:
    if b is None:
        return None
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return None                       # binary: file check sees no text


def to_file_changes(saved: list[SavedFile], mirror: Path, base_sha: str) -> list[FileChange]:
    """Old text comes from the host mirror at base_sha (spec E13), never from the box."""
    out = []
    for s in saved:
        old = None
        if s.status in ("M", "D"):
            p = subprocess.run(["git", "show", f"{base_sha}:{s.path}"], cwd=mirror, capture_output=True, timeout=60)
            old = _text(p.stdout) if p.returncode == 0 else None
        out.append(FileChange(path=s.path, status=s.status, mode=s.mode, old_text=old, new_text=_text(s.content())))
    return out


def unified_diff(mirror: Path, base_sha: str, saved: list[SavedFile]) -> str:
    """Human-readable diff for the approval panel and proposed_diff.txt."""
    import difflib
    chunks = []
    for fc in to_file_changes(saved, mirror, base_sha):
        if fc.old_text is None and fc.status != "A" or (fc.new_text is None and fc.status != "D"):
            chunks.append(f"Binary or unreadable file {fc.status}: {fc.path}\n")
            continue
        a = (fc.old_text or "").splitlines(keepends=True)
        b = (fc.new_text or "").splitlines(keepends=True)
        chunks.append("".join(difflib.unified_diff(a, b, f"a/{fc.path}", f"b/{fc.path}")) or f"Mode change {fc.mode}: {fc.path}\n")
    return "".join(chunks)


def apply_changes(backend, saved: list[SavedFile]) -> None:
    """Re-apply a saved change in a fresh sandbox (continue_fix / continue_with_decision)."""
    ups = [(f"/sandbox/{s.path}", s.content()) for s in saved if s.status != "D" and s.mode != SYMLINK]
    if ups:
        res = backend.upload_files(ups)
        bad = [u for u in res if getattr(u, "error", None)]
        if bad:
            raise ChangeCaptureError(f"re-apply failed: {bad}")
    cmds = []
    for s in saved:
        q = shlex.quote(f"./{s.path}")       # "./" instead of "--": BSD chmod rejects "--" (verified)
        if s.status == "D":
            cmds.append(f"rm -f {q}")
        elif s.mode == SYMLINK:
            cmds.append(f"ln -sfn {shlex.quote(s.content().decode())} {q}")
        elif s.mode == EXECUTABLE:
            cmds.append(f"chmod +x {q}")
        else:
            cmds.append(f"chmod -x {q}")
    if cmds:
        r = backend.execute("cd sandbox && " + " && ".join(cmds))
        if r.exit_code != 0:
            raise ChangeCaptureError(f"re-apply failed: {r.output[-1000:]}")
