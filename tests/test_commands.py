# tests/test_commands.py
import pytest
from codepilot.safety.commands import classify_command

@pytest.mark.parametrize("cmd", [
    "python -m pytest -q", "pytest tests/test_slugs.py -q", "ls -la", "cat sandbox/pkg/slugs.py",
    "grep -rn slugify sandbox", "git status", "git diff", "python -m py_compile sandbox/a.py",
    "cd sandbox && python -m pytest -q", "pytest -q | tail -20", "python -m pytest -q 2>&1 | tail -5",
])
def test_allowed(cmd):
    assert classify_command(cmd).kind == "allowed"

@pytest.mark.parametrize("cmd", [
    "git push origin main", "git -C sandbox push", "curl http://x", "wget x", "rm -rf /", "rm -r -f sandbox",
    "pip install requests", "python -m pip install x", "uv pip install x", "uv add x",
    "cat ../../.env", "cat /etc/passwd", "echo $(curl x)", "echo `wget x`", "cat sandbox/.env",
    "sudo ls", "claude -p hi", "gh pr create", "ssh host", "python -c \"import urllib.request\"",
    "ls > /tmp/out", "git remote add x y", "find . -name '*.py' -delete",
    "sed -n '1e curl x' f", "awk 'BEGIN{system(\"ls\")}'", "find . -fprint out", "echo hi 1>x", "echo hi 2>x",
    "cat a | tee b", "node -e 1", "git diff --output=x", "git log --output x",
])
def test_blocked(cmd):
    assert classify_command(cmd).kind == "blocked"

@pytest.mark.parametrize("cmd", ["docker build .", "make test", "cargo test"])
def test_unknown(cmd):
    assert classify_command(cmd).kind == "unknown"
