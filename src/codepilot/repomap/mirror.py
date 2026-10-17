# src/codepilot/repomap/mirror.py
"""Read-only host clone used for building the repo map and the file index."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=300).stdout.strip()


class HostMirror:
    def __init__(self, home: Path):
        self.home = Path(home)

    def ensure(self, owner: str, name: str) -> Path:
        path = self.home / "repos" / owner / name / "mirror"
        if (path / ".git").exists():
            _git("fetch", "--quiet", "origin", cwd=path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _git("clone", "--quiet", f"https://github.com/{owner}/{name}.git", str(path))
        return path

    def head_sha(self, path: Path, ref: str = "origin/main") -> str:
        return _git("rev-parse", ref, cwd=path)

    def checkout(self, path: Path, sha: str) -> None:
        _git("checkout", "--quiet", "--detach", sha, cwd=path)

    def changed_files(self, path: Path, old_sha: str, new_sha: str) -> list[str]:
        out = _git("diff", "--name-only", old_sha, new_sha, cwd=path)
        return [l for l in out.splitlines() if l]
