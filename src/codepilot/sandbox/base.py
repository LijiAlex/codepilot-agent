# src/codepilot/sandbox/base.py
"""Sandbox layout shared by every provider.

Agents see the repository at /sandbox/ (file tools) and as sandbox/ from the
shell (whose working folder is the run folder). Working files the agents write
for code — the diff, the PR draft, task.json, library offload files — live at
/shared/, which is outside the clone and never committed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deepagents import FilesystemPermission

SANDBOX_ROUTE = "/sandbox/"
SHARED_ROUTE = "/shared/"
SECRET_GLOBS = ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.secret", "**/*credentials*"]
NONINTERACTIVE_ENV = {"PAGER": "cat", "GIT_PAGER": "cat", "MANPAGER": "cat", "TQDM_DISABLE": "1",
                      "PIP_PROGRESS_BAR": "off", "LANGSMITH_TRACING": "false", "GIT_TERMINAL_PROMPT": "0",
                      "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def sandbox_permissions(protected: list[str] | None = None) -> list[FilesystemPermission]:
    """Deny rules first: rules are first-match-wins with default allow (verified, deepagents filesystem.py).
    `protected` (the repo's protected_paths) become write-deny rules too, so agents cannot edit them during the run;
    the file check still re-checks the final change (shell writes are not covered by these rules)."""
    secret_paths = [f"{SANDBOX_ROUTE}{g}" for g in SECRET_GLOBS] + [f"{SANDBOX_ROUTE}{g.removeprefix('**/')}" for g in SECRET_GLOBS]
    locked = [f"{SANDBOX_ROUTE}.github/**", f"{SANDBOX_ROUTE}.git/**"]
    for p in protected or []:
        locked.append(f"{SANDBOX_ROUTE}{p}")
        if p.startswith("**/"):
            locked.append(f"{SANDBOX_ROUTE}{p.removeprefix('**/')}")
    return [
        FilesystemPermission(operations=["read", "write"], paths=secret_paths, mode="deny"),
        FilesystemPermission(operations=["write"], paths=list(dict.fromkeys(locked)), mode="deny"),
    ]


@dataclass
class SandboxHandle:
    run_id: str
    kind: str                       # "local" | "daytona"
    backend: Any                    # CompositeBackend
    shared_dir: Path                # host folder behind /shared/
    sandbox_id: str | None = None
    raw: Any = None                 # provider object (Daytona Sandbox) for cleanup
    extra: dict = field(default_factory=dict)


class SandboxProvider(Protocol):
    def create(self, run_id: str, owner: str, name: str, base_sha: str, install_command: str | None) -> SandboxHandle: ...
    def delete(self, handle: SandboxHandle) -> None: ...
    def sweep(self, keep_run_ids: set[str]) -> int: ...
    def reinstall(self, handle: SandboxHandle, install_command: str) -> tuple[int, str]: ...
