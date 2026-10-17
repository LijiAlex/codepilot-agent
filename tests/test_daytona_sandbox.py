# tests/test_daytona_sandbox.py
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox

from codepilot.sandbox.daytona import LABEL, DaytonaSandboxProvider, SandboxSetupError


class FakeBox(BaseSandbox):
    """Stand-in for a Daytona box: its own HOME, absolute paths, /bin/sh."""
    def __init__(self, home: Path):
        self.home = home
    @property
    def id(self): return "box-1"
    def execute(self, command, *, timeout=None):
        env = {**os.environ, "HOME": str(self.home)}
        p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout or 120, env=env)
        out = p.stdout + (f"\n<stderr>{p.stderr.strip()}</stderr>" if p.stderr.strip() else "")   # same shape as langchain-daytona
        return ExecuteResponse(output=out, exit_code=p.returncode, truncated=False)
    def upload_files(self, files):
        for path, data in files:
            Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_bytes(data)
        return [FileUploadResponse(path=p, error=None) for p, _ in files]
    def download_files(self, paths):
        return [FileDownloadResponse(path=p, content=Path(p).read_bytes(), error=None) for p in paths]


@dataclass
class FakeRaw:
    id: str
    labels: dict = field(default_factory=dict)


class FakeClient:
    def __init__(self): self.boxes, self.deleted, self.params = [], [], []
    def create(self, params, timeout=60):
        self.params.append(params); raw = FakeRaw(f"sb{len(self.boxes)}", dict(params.labels)); self.boxes.append(raw); return raw
    def delete(self, raw, timeout=60, wait=False): self.deleted.append(raw.id)
    def list(self, query=None): return [b for b in self.boxes if b.id not in self.deleted] + [FakeRaw("other", {})]


@pytest.fixture
def mirror(tmp_path):
    m = tmp_path / "home" / "repos" / "o" / "r" / "mirror"
    (m / "pkg").mkdir(parents=True); (m / "pkg" / "a.py").write_text("x = 1\n")
    for c in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *c], cwd=m, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/o/r.git"], cwd=m, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=m, capture_output=True, text=True).stdout.strip()
    return m, sha


def provider(tmp_path, client):
    def wrap(raw):                      # every box gets its own HOME, like real boxes
        d = tmp_path / "boxes" / raw.id; d.mkdir(parents=True, exist_ok=True); return FakeBox(d)
    return DaytonaSandboxProvider(tmp_path / "home", tmp_path / "home" / "repos", client=client, wrap=wrap), tmp_path / "boxes" / "sb0"


def test_daytona_layout_no_remote_labels_and_shared_on_host(tmp_path, mirror):
    _, sha = mirror
    client = FakeClient()
    prov, box_home = provider(tmp_path, client)
    h = prov.create("run1", "o", "r", sha, install_command="echo installed > ../install.log && echo noise >&2")
    b = h.backend
    assert client.params[0].labels == {LABEL: "run1"} and client.params[0].auto_stop_interval == 30
    assert client.params[0].auto_delete_interval == 60
    assert client.params[0].env_vars["GIT_TERMINAL_PROMPT"] == "0"
    assert h.extra["box_root"] == f"{box_home}/run"
    assert "x = 1" in str(b.read("/sandbox/pkg/a.py"))
    out = b.execute("git -C sandbox remote -v | wc -l && echo $VIRTUAL_ENV && cat install.log")
    lines = out.output.split()
    assert lines[0] == "0" and lines[1].endswith("/run/.venv") and "installed" in out.output, out.output
    assert not (box_home / "run" / "repo.bundle").exists()
    b.write("/shared/task.json", "{}")
    assert (h.shared_dir / "task.json").read_text() == "{}" and str(tmp_path / "home" / "runs") in str(h.shared_dir)
    prov.delete(h)
    assert client.deleted == ["sb0"]


def test_daytona_failed_install_deletes_box(tmp_path, mirror):
    _, sha = mirror
    client = FakeClient()
    prov, _ = provider(tmp_path, client)
    with pytest.raises(SandboxSetupError):
        prov.create("run2", "o", "r", sha, install_command="exit 3")
    assert client.deleted == ["sb0"]


def test_daytona_sweep_only_touches_codepilot_boxes(tmp_path, mirror):
    _, sha = mirror
    client = FakeClient()
    prov, _ = provider(tmp_path, client)
    prov.create("keep", "o", "r", sha, None); prov.create("orphan", "o", "r", sha, None)
    assert prov.sweep({"keep"}) == 1 and client.deleted == ["sb1"]


def test_smoke_script_against_fake_box(tmp_path, mirror):
    import importlib.util
    spec = importlib.util.spec_from_file_location("daytona_smoke", Path(__file__).parent.parent / "scripts" / "daytona_smoke.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    _, sha = mirror
    client = FakeClient()
    prov, _ = provider(tmp_path, client)
    res = mod.smoke(prov, "o", "r", sha)
    assert res["tools"] and res["no_remote"] and res["write_read"] and res["glob_virtual_paths"], res
    assert client.deleted == ["sb0"]



def test_daytona_reinstall_runs_in_the_box_venv(tmp_path, mirror):
    _, sha = mirror
    prov, _ = provider(tmp_path, FakeClient())
    h = prov.create("run1", "o", "r", sha, None)
    code, out = prov.reinstall(h, "echo $VIRTUAL_ENV && pwd")
    assert code == 0 and "/run/.venv" in out and out.strip().endswith("/run/sandbox")


def test_change_capture_through_the_box_adapter(tmp_path, mirror):
    from codepilot.changes import capture_changes
    _, sha = mirror
    prov, box_home = provider(tmp_path, FakeClient())
    h = prov.create("run1", "o", "r", sha, None)
    h.backend.write("/sandbox/pkg/new.py", "n = 1\n")
    h.backend.edit("/sandbox/pkg/a.py", "x = 1", "x = 2")
    saved = {s.path: s for s in capture_changes(h.backend, sha)}
    assert saved["pkg/new.py"].content() == b"n = 1\n" and saved["pkg/a.py"].content() == b"x = 2\n"
