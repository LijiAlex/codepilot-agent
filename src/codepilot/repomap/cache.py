# src/codepilot/repomap/cache.py
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from codepilot.repomap.build import FileEntry, parse_file


class RepoMapCache:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sha: str) -> Path:
        return self.dir / f"{sha}.json"

    def get(self, sha: str) -> list[FileEntry] | None:
        p = self._path(sha)
        return [FileEntry(**d) for d in json.loads(p.read_text())] if p.exists() else None

    def put(self, sha: str, entries: list[FileEntry]) -> None:
        self._path(sha).write_text(json.dumps([asdict(e) for e in entries]))

    def latest_sha(self) -> str | None:
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return files[-1].stem if files else None

    def refresh(self, root: Path, old_sha: str, new_sha: str, changed: list[str]) -> list[FileEntry]:
        """Re-parse only files changed between two commits (brief: invalidate via git diff)."""
        entries = {e.path: e for e in (self.get(old_sha) or [])}
        for rel in changed:
            p = Path(root) / rel
            if p.exists() and p.is_file():
                entries[rel] = parse_file(Path(root), p)
            else:
                entries.pop(rel, None)
        result = sorted(entries.values(), key=lambda e: e.path)
        self.put(new_sha, result)
        return result
