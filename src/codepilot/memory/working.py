# src/codepilot/memory/working.py
"""Working memory: the current task's state, one JSON file at /shared/task.json.
Agents get the path, not the content; code updates it between steps."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

FIELDS = ("issue", "task_type", "repo_map_path", "relevant_files", "diff_path", "test_results", "retry_count", "notes")


class WorkingMemory:
    VIRTUAL_PATH = "/shared/task.json"

    def __init__(self, shared_dir: Path):
        self.path = Path(shared_dir) / "task.json"
        self._lock = threading.Lock()

    def init(self, issue: dict, task_type: str, retry_count: int = 0) -> None:
        self._write({"issue": issue, "task_type": task_type, "repo_map_path": None, "relevant_files": [],
                     "diff_path": None, "test_results": None, "retry_count": retry_count, "notes": []})

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def update(self, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise KeyError(f"unknown working-memory fields: {sorted(unknown)}")
        with self._lock:
            data = self.read()
            data.update(fields)
            self._write(data)
            return data

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)                        # atomic: a crash never leaves half a file
