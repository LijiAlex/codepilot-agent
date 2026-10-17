# src/codepilot/sandbox/daytona.py
"""Daytona sandbox: one remote box per run. The box gets the repository as a git
bundle made on this machine from the local mirror, so no GitHub credential ever
enters the box and the clone has no remote."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend

from codepilot.sandbox.base import NONINTERACTIVE_ENV, SANDBOX_ROUTE, SHARED_ROUTE, SandboxHandle
from codepilot.sandbox.prefixed import CwdSandbox, PrefixedSandbox

LABEL = "codepilot_run"


class SandboxSetupError(RuntimeError):
    pass


def make_client():
    """Explicit api_key so the SDK never goes looking in a .env file (verified: it reads env + .env)."""
    from daytona import Daytona, DaytonaConfig
    return Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"]))


def default_wrap(raw):
    from langchain_daytona import DaytonaSandbox
    return DaytonaSandbox(sandbox=raw, timeout=1800, sync_polling_interval=0.1)


class DaytonaSandboxProvider:
    def __init__(self, home: Path, mirror_root: Path, *, client=None, wrap=default_wrap,
                 snapshot: str | None = None, auto_stop_minutes: int = 30):
        self.runs = Path(home) / "runs"
        self.mirror_root = Path(mirror_root)
        self._client, self.wrap = client, wrap
        self.snapshot, self.auto_stop_minutes = snapshot, auto_stop_minutes

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def _params(self, run_id: str):
        from daytona import CreateSandboxFromSnapshotParams
        return CreateSandboxFromSnapshotParams(
            snapshot=self.snapshot, labels={LABEL: run_id}, env_vars=dict(NONINTERACTIVE_ENV),
            auto_stop_interval=self.auto_stop_minutes, auto_delete_interval=60)   # killed process: box deleted 1 h after stop

    @staticmethod
    def _run(box, cmd: str, timeout: int = 600) -> str:
        r = box.execute(cmd, timeout=timeout)
        if r.exit_code != 0:
            raise SandboxSetupError(f"box command failed ({r.exit_code}): {cmd.split()[0]} …\n{r.output[-2000:]}")
        return r.output

    def create(self, run_id: str, owner: str, name: str, base_sha: str, install_command: str | None) -> SandboxHandle:
        shared = self.runs / run_id / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        raw = self.client.create(self._params(run_id), timeout=120)
        try:
            box = self.wrap(raw)
            # langchain-daytona appends stderr as "\n<stderr>…</stderr>" (verified, sandbox.py)
            home = self._run(box, 'echo "$HOME"').split("<stderr>")[0].strip().splitlines()[-1]
            root = f"{home}/run"
            mirror = self.mirror_root / owner / name / "mirror"
            with tempfile.TemporaryDirectory() as td:
                bundle = Path(td) / "repo.bundle"
                subprocess.run(["git", "bundle", "create", "--quiet", str(bundle), "--all"], cwd=mirror, check=True, timeout=300)
                self._run(box, f"mkdir -p {shlex.quote(root)}")
                up = box.upload_files([(f"{root}/repo.bundle", bundle.read_bytes())])
                if any(getattr(u, "error", None) for u in up):
                    raise SandboxSetupError(f"bundle upload failed: {up}")
            q = shlex.quote
            self._run(box, f"cd {q(root)} && git clone --quiet repo.bundle sandbox && rm repo.bundle"
                           f" && git -C sandbox checkout --quiet --detach {q(base_sha)}"
                           f" && git -C sandbox remote remove origin && python3 -m venv .venv")
            activate = ". .venv/bin/activate"
            if install_command:                 # by code, before any agent runs
                self._run(box, f"cd {q(root)} && {activate} && cd sandbox && {install_command}", timeout=900)
            backend = CompositeBackend(
                default=CwdSandbox(box, root, preamble=activate),
                routes={SANDBOX_ROUTE: PrefixedSandbox(box, f"{root}/sandbox"),
                        SHARED_ROUTE: FilesystemBackend(root_dir=shared, virtual_mode=True)},
                artifacts_root=SHARED_ROUTE,
            )
        except Exception:
            self.client.delete(raw)
            raise
        return SandboxHandle(run_id=run_id, kind="daytona", backend=backend, shared_dir=shared,
                             sandbox_id=raw.id, raw=raw, extra={"box_root": root, "box": box})

    def reinstall(self, handle: SandboxHandle, install_command: str) -> tuple[int, str]:
        """Run by code after the agent changed a manifest (spec §5); the box keeps its egress for this."""
        root = handle.extra["box_root"]
        r = handle.extra["box"].execute(f"cd {shlex.quote(root)} && . .venv/bin/activate && cd sandbox && {install_command}", timeout=900)
        return r.exit_code, r.output[-3000:]

    def delete(self, handle: SandboxHandle) -> None:
        if handle.raw is not None:
            self.client.delete(handle.raw)

    def sweep(self, keep_run_ids: set[str]) -> int:
        n = 0
        for sb in self._codepilot_boxes():
            if (sb.labels or {}).get(LABEL) not in keep_run_ids:
                self.client.delete(sb); n += 1
        return n

    def _codepilot_boxes(self):
        # list() filters by exact label value; we need "has the label", so list all and filter here.
        return [sb for sb in self.client.list() if LABEL in (sb.labels or {})]
