# tests/test_prefixed_sandbox.py
import subprocess
from pathlib import Path
from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse, FileDownloadResponse
from deepagents.backends import CompositeBackend, FilesystemBackend
from codepilot.sandbox.prefixed import PrefixedSandbox, CwdSandbox

class LocalExecSandbox(BaseSandbox):
    """Stand-in for a remote box: absolute paths, commands via /bin/sh."""
    @property
    def id(self): return "local-box"
    def execute(self, command, *, timeout=None):
        p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout or 60)
        return ExecuteResponse(output=p.stdout + p.stderr, exit_code=p.returncode, truncated=False)
    def upload_files(self, files):
        out = []
        for path, data in files:
            Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_bytes(data)
            out.append(FileUploadResponse(path=path, error=None))
        return out
    def download_files(self, paths):
        return [FileDownloadResponse(path=p, content=Path(p).read_bytes(), error=None) for p in paths]

def test_prefixed_paths_in_and_out(tmp_path):
    box_run = tmp_path / "box" / "run"; (box_run / "sandbox" / "pkg").mkdir(parents=True)
    (box_run / "sandbox" / "pkg" / "slugs.py").write_text("def slugify(t):\n    return t\n")
    inner = LocalExecSandbox()
    backend = CompositeBackend(default=CwdSandbox(inner, str(box_run)),
                               routes={"/sandbox/": PrefixedSandbox(inner, str(box_run / "sandbox")),
                                       "/shared/": FilesystemBackend(root_dir=tmp_path / "shared", virtual_mode=True)},
                               artifacts_root="/shared/")
    w = backend.write("/sandbox/pkg/new.py", "x = 1\n")
    assert (box_run / "sandbox" / "pkg" / "new.py").read_text() == "x = 1\n", w
    r = backend.read("/sandbox/pkg/slugs.py")
    assert "slugify" in str(r)
    g = backend.glob("**/*.py", "/sandbox/")
    paths = [m["path"] if isinstance(m, dict) else m.path for m in (g.matches if hasattr(g, "matches") else g)]
    assert any(p.endswith("/pkg/slugs.py") for p in paths) and not any(str(box_run) in p for p in paths), paths
    e = backend.execute("cat sandbox/pkg/new.py")
    assert "x = 1" in e.output

def test_prefixed_edit_ls_grep_delete(tmp_path):
    box = tmp_path / "box" / "run"; (box / "sandbox").mkdir(parents=True)
    (box / "sandbox" / "a.py").write_text("value = 1\n")
    inner = LocalExecSandbox()
    backend = CompositeBackend(default=CwdSandbox(inner, str(box)),
                               routes={"/sandbox/": PrefixedSandbox(inner, str(box / "sandbox"))})
    backend.edit("/sandbox/a.py", "value = 1", "value = 2")
    assert (box / "sandbox" / "a.py").read_text() == "value = 2\n"
    ls = backend.ls("/sandbox/")
    entries = ls.entries if hasattr(ls, "entries") else ls
    names = [e["path"] if isinstance(e, dict) else e.path for e in entries]
    assert any(n.endswith("/a.py") for n in names) and not any(str(box) in n for n in names), names
    # BaseSandbox.grep uses `grep -Z` (GNU). Remote boxes are Linux; macOS ships BSD grep.
    probe = subprocess.run(f"grep -rHnFZ value {box / 'sandbox'}", shell=True, capture_output=True, text=True).stdout
    if "\0" in probe:
        gr = backend.grep("value", "/sandbox/")
        assert str(box) not in str(gr) and "a.py" in str(gr)
    backend.delete("/sandbox/a.py")
    assert not (box / "sandbox" / "a.py").exists()
