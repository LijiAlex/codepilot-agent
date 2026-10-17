# tests/test_local_sandbox.py
import subprocess
from pathlib import Path
import shutil
import pytest
from codepilot.sandbox.local import LocalSandboxProvider

@pytest.fixture
def mirror(tmp_path):
    m = tmp_path / "home" / "repos" / "o" / "r" / "mirror"
    m.mkdir(parents=True)
    (m / "pkg").mkdir(); (m / "pkg" / "a.py").write_text("x = 1\n")
    (m / "tests").mkdir(); (m / "tests" / "test_a.py").write_text("def test_a():\n    assert 1\n")
    for c in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *c], cwd=m, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/o/r.git"], cwd=m, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=m, capture_output=True, text=True).stdout.strip()
    return m, sha

@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="local fallback uses macOS sandbox-exec")
def test_local_sandbox_layout_isolation_and_cleanup(tmp_path, mirror):
    m, sha = mirror
    secret = tmp_path / "secretdir"; secret.mkdir(); (secret / "key.txt").write_text("TOPSECRET")
    prov = LocalSandboxProvider(home=tmp_path / "home", mirror_root=tmp_path / "home" / "repos", extra_deny=[secret])
    h = prov.create("run1", "o", "r", sha, install_command=None)
    b = h.backend
    assert "x = 1" in str(b.read("/sandbox/pkg/a.py"))
    out = b.execute("cat sandbox/pkg/a.py && git -C sandbox remote -v | wc -l")
    assert "x = 1" in out.output and out.output.strip().endswith("0")     # no remote left
    denied = b.execute(f"cat {secret}/key.txt")
    assert "TOPSECRET" not in denied.output and denied.exit_code != 0
    net = b.execute("python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"")
    assert net.exit_code != 0
    home = b.execute("echo $HOME")
    assert str(Path.home()) != home.output.strip()
    b.write("/shared/diff.txt", "d")
    assert (h.shared_dir / "diff.txt").exists() and not (tmp_path / "home" / "runs" / "run1" / "sandbox" / "diff.txt").exists()
    prov.delete(h)
    assert not (tmp_path / "home" / "runs" / "run1" / "sandbox").exists()


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="local fallback uses macOS sandbox-exec")
def test_local_reinstall_runs_outside_the_sandbox_profile(tmp_path, mirror):
    m, sha = mirror
    prov = LocalSandboxProvider(home=tmp_path / "home", mirror_root=tmp_path / "home" / "repos")
    h = prov.create("run2", "o", "r", sha, install_command=None)
    code, out = prov.reinstall(h, "python -c \"import sys; print(sys.prefix)\"")
    assert code == 0 and out.strip().endswith(".venv")          # ran with the run's venv (inside the install profile)
    prov.delete(h)


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="local fallback uses macOS sandbox-exec")
def test_escape_attempts_are_blocked(tmp_path, mirror):
    m, sha = mirror
    home = tmp_path / "home"
    (home / "keys").mkdir(parents=True); (home / "keys" / "app.pem").write_text("PRIVATEKEY")
    real_home_target = Path.home() / f".codepilot-sandbox-test-{tmp_path.name}"     # a real location agents must not touch
    prov = LocalSandboxProvider(home=home, mirror_root=home / "repos")
    h = prov.create("run3", "o", "r", sha, install_command=None)
    b = h.backend
    try:
        # 1. writing outside the run fails: in the user's real home and in CodePilot+'s own folder
        r = b.execute(f"python -c \"open('{real_home_target}', 'w').write('x')\"")
        assert r.exit_code != 0 and not real_home_target.exists()
        r = b.execute(f"python -c \"open('{home}/keys/new.pem', 'w').write('x')\"")
        assert r.exit_code != 0 and not (home / "keys" / "new.pem").exists()
    finally:
        real_home_target.unlink(missing_ok=True)
    # 2. CodePilot+'s own data (the App key) is unreadable, but the run itself stays readable
    assert "PRIVATEKEY" not in b.execute(f"cat {home}/keys/app.pem").output
    assert "x = 1" in b.execute("cat sandbox/pkg/a.py").output
    # 3. the profile cannot be rewritten through the file tools (default route is read-only) or from the shell
    assert b.write("/sandbox.sb", "(version 1)\n(allow default)\n").error
    profile = home / "profiles" / "run3.sb"
    assert b.execute(f"echo '(version 1)(allow default)' | tee {profile}").exit_code != 0
    assert "(deny network*)" in profile.read_text()
    # 4. writes inside the repository and temp still work
    assert b.execute("python -c \"open('sandbox/new.txt','w').write('ok')\"").exit_code == 0
    # 5. reinstall (agent-controlled manifest) runs sandboxed: network allowed, secrets still unreadable, writes confined
    code, out = prov.reinstall(h, f"cat {home}/keys/app.pem; python -c \"open('{home}/keys/y.pem','w')\"")
    assert "PRIVATEKEY" not in out and not (home / "keys" / "y.pem").exists()
    prov.delete(h)
    assert not profile.exists()


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="local fallback uses macOS sandbox-exec")
def test_real_home_files_are_unreadable_even_during_reinstall(tmp_path, mirror):
    m, sha = mirror
    marker = Path.home() / f".codepilot-read-test-{tmp_path.name}"      # stands in for ~/.zshrc with exported keys
    marker.write_text("export ANTHROPIC_API_KEY=FAKE-MARKER")
    try:
        prov = LocalSandboxProvider(home=tmp_path / "home", mirror_root=tmp_path / "home" / "repos")
        h = prov.create("run4", "o", "r", sha, install_command=None)
        assert "FAKE-MARKER" not in h.backend.execute(f"cat {marker}").output
        code, out = prov.reinstall(h, f"cat {marker}; python -c \"print(open('{marker}').read())\"")
        assert "FAKE-MARKER" not in out
        assert h.backend.execute("python -c \"import json, sqlite3; print('ok')\"").output.strip().startswith("ok")   # interpreter still works
        prov.delete(h)
    finally:
        marker.unlink(missing_ok=True)
