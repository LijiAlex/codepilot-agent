# src/codepilot/sandbox/local.py
"""Local fallback sandbox: a clone per run, a minimal environment, and macOS sandbox-exec. The profile denies network,
denies reading secret folders and CodePilot+'s own data (App key, databases, config), and denies writing anywhere except
the run's own folders and the temp dirs. Profiles live outside every agent-visible path, and the shell backend's own
route is read-only, so an agent cannot rewrite its sandbox. Weaker than a remote box; the README says so."""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from deepagents.backends.protocol import EditResult, WriteResult

from codepilot.sandbox.base import NONINTERACTIVE_ENV, SANDBOX_ROUTE, SHARED_ROUTE, SandboxHandle

HOME = Path.home()
DENY_READ = [HOME / ".secrets", HOME / ".ssh", HOME / ".config" / "gh", HOME / ".aws",
             HOME / "Library" / "Keychains", HOME / ".netrc", HOME / ".gitconfig"]
WRITABLE_SYSTEM = [Path("/private/tmp"), Path("/private/var/folders"), Path("/dev")]


def _profile(run_dir: Path, deny_read: list[Path], codepilot_home: Path, *, network: bool) -> str:
    """SBPL: the last matching rule wins. Reads: deny the user's whole home folder (shell profiles hold API keys) and
    CodePilot+'s own folder, then allow back only this run and the Python installation. Writes: deny everywhere, allow
    temp dirs, deny CodePilot+'s folder again (it may sit under a temp dir), then allow only this run's folders."""
    rd, cp = run_dir.resolve(), codepilot_home.resolve()
    run_rw = [rd / "sandbox", rd / "home", rd / ".venv"]
    readable = [rd, Path(sys.base_prefix).resolve(), Path(sys.executable).resolve().parent]   # interpreter may live under ~
    lines = ["(version 1)", "(allow default)"]
    if not network:
        lines.append("(deny network*)")
    lines += [f'(deny file-read* (subpath "{Path(p).resolve()}"))' for p in [HOME, *deny_read]]
    lines += [f'(allow file-read* (subpath "{p}"))' for p in dict.fromkeys(readable)]
    # path resolution (realpath) stats each parent of an allowed folder; allow metadata only, never contents
    roots = [HOME.resolve(), cp]
    ancestors = {a for p in readable for a in p.parents if any(a == r or r in a.parents for r in roots)}
    lines += [f'(allow file-read-metadata (literal "{a}"))' for a in sorted(ancestors)]
    lines.append("(deny file-write*)")
    lines += [f'(allow file-write* (subpath "{p}"))' for p in WRITABLE_SYSTEM]
    lines.append(f'(deny file-write* (subpath "{cp}"))')            # profiles, keys, databases: never writable
    lines += [f'(allow file-write* (subpath "{p}"))' for p in run_rw]
    return "\n".join(lines) + "\n"


class SandboxedShellBackend(LocalShellBackend):
    """Runs every command through sandbox-exec. Its own file route (paths outside /sandbox/ and /shared/) is read-only:
    deepagents cannot attach permission rules to the default route of an executing backend, so writes are refused here."""

    def __init__(self, *args, profile_path: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile_path = profile_path

    def execute(self, command: str, *, timeout: int | None = None):
        wrapped = f"sandbox-exec -f {shlex.quote(str(self.profile_path))} /bin/sh -c {shlex.quote(command)}"
        return super().execute(wrapped, timeout=timeout)

    def write(self, file_path, content):
        return WriteResult(error=f"permission denied: {file_path} is outside /sandbox/ and /shared/")

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return EditResult(error=f"permission denied: {file_path} is outside /sandbox/ and /shared/")

    def upload_files(self, files):
        from deepagents.backends.protocol import FileUploadResponse
        return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]

    def delete(self, file_path):
        return WriteResult(error=f"permission denied: {file_path} is outside /sandbox/ and /shared/")


class LocalSandboxProvider:
    def __init__(self, home: Path, mirror_root: Path, extra_deny: list[Path] | None = None):
        self.home = Path(home)
        self.runs = self.home / "runs"
        self.profiles = self.home / "profiles"          # outside every run folder: no agent path reaches it
        self.mirror_root = Path(mirror_root)
        # CodePilot+'s own data is never readable from inside: App key, databases, config (spec: agents never read secrets)
        self.deny = [*DENY_READ, self.home, *(extra_deny or [])]

    def create(self, run_id: str, owner: str, name: str, base_sha: str, install_command: str | None) -> SandboxHandle:
        run_dir = self.runs / run_id
        sandbox, shared, fake_home = run_dir / "sandbox", run_dir / "shared", run_dir / "home"
        for d in (shared, fake_home, self.profiles):
            d.mkdir(parents=True, exist_ok=True)
        mirror = self.mirror_root / owner / name / "mirror"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(mirror), str(sandbox)], check=True, timeout=300)
        subprocess.run(["git", "checkout", "--quiet", "--detach", base_sha], cwd=sandbox, check=True, timeout=60)
        subprocess.run(["git", "remote", "remove", "origin"], cwd=sandbox, check=True, timeout=30)
        venv = run_dir / ".venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=300)
        env = {"PATH": f"{venv}/bin:/usr/bin:/bin", "HOME": str(fake_home), "VIRTUAL_ENV": str(venv), **NONINTERACTIVE_ENV}
        profile = self.profiles / f"{run_id}.sb"
        profile.write_text(_profile(run_dir, self.deny, self.home, network=False))
        install_profile = self.profiles / f"{run_id}-install.sb"
        install_profile.write_text(_profile(run_dir, self.deny, self.home, network=True))
        if install_command:                     # by code, before any agent runs; network allowed, nothing secret readable
            self._install(sandbox, env, install_profile, install_command)
        backend = CompositeBackend(
            default=SandboxedShellBackend(root_dir=run_dir, virtual_mode=True, env=env, timeout=300, profile_path=profile),
            routes={SANDBOX_ROUTE: FilesystemBackend(root_dir=sandbox, virtual_mode=True),
                    SHARED_ROUTE: FilesystemBackend(root_dir=shared, virtual_mode=True)},
            artifacts_root=SHARED_ROUTE,
        )
        return SandboxHandle(run_id=run_id, kind="local", backend=backend, shared_dir=shared, sandbox_id=str(run_dir),
                             extra={"env": env, "sandbox": sandbox, "install_profile": install_profile})

    @staticmethod
    def _install(sandbox: Path, env: dict, profile: Path, install_command: str, check: bool = True) -> tuple[int, str]:
        p = subprocess.run(["sandbox-exec", "-f", str(profile), "/bin/sh", "-c", install_command], cwd=sandbox,
                           env={**env, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}, capture_output=True, text=True, timeout=900)
        if check and p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, install_command, p.stdout, p.stderr)
        return p.returncode, (p.stdout + p.stderr)[-3000:]

    def reinstall(self, handle: SandboxHandle, install_command: str) -> tuple[int, str]:
        """After the agent changed a manifest (spec §5). The manifest is agent-written code (setup.py), so it runs under
        the same profile with network allowed: still no secret reads, writes still confined to the run."""
        return self._install(handle.extra["sandbox"], handle.extra["env"], handle.extra["install_profile"], install_command, check=False)

    def delete(self, handle: SandboxHandle) -> None:
        shutil.rmtree(self.runs / handle.run_id / "sandbox", ignore_errors=True)
        shutil.rmtree(self.runs / handle.run_id / ".venv", ignore_errors=True)
        for p in self.profiles.glob(f"{handle.run_id}*.sb"):
            p.unlink(missing_ok=True)

    def sweep(self, keep_run_ids: set[str]) -> int:
        n = 0
        for d in self.runs.glob("*"):
            if d.is_dir() and d.name not in keep_run_ids and (d / "sandbox").exists():
                shutil.rmtree(d / "sandbox", ignore_errors=True); shutil.rmtree(d / ".venv", ignore_errors=True); n += 1
        return n
