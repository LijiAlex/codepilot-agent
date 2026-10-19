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


@pytest.mark.parametrize("cmd", [
    "echo hi\ncurl x", "echo hi\rcurl x", "echo a & curl b", "python3 -mpip install x", "python -mvenv v",
    "git -Csandbox push", "git -Cx status", "bash -c 'curl x'", "sh -c \"pip install x\"", "xargs curl",
    "nohup wget x", "timeout 5 curl x", "time pip install x", "ls | xargs rm",
])
def test_blocked_guard_bypasses(cmd):
    assert classify_command(cmd).kind == "blocked"


@pytest.mark.parametrize("cmd", ["timeout 60 python -m pytest -q", "time pytest -q", "python -m pytest -q 2>&1 | tail -5"])
def test_wrappers_classified_by_wrapped_command(cmd):
    assert classify_command(cmd).kind == "allowed"


def test_refusal_reason_names_the_real_problem_not_the_quotes():
    # run 9705eaa62840: the ; inside the quotes split the command and the guard said "unparseable command"
    c = classify_command('cd sandbox && python -c "import pydantic;print(pydantic.VERSION)"')
    assert c.kind == "blocked" and c.reason == "inline python is not allowed"
    q = classify_command('grep "a;b" file.py')          # still blocked (behaviour unchanged), with an honest reason
    assert q.kind == "blocked" and "inside quotes" in q.reason
    assert classify_command('echo "oops').reason == "unparseable command (unbalanced quotes)"


def test_only_push_download_install_refusals_carry_the_publishing_hint():
    assert classify_command("pip install x").hint and classify_command("git push origin main").hint
    assert classify_command("curl http://x").hint and classify_command("python -m pip --version").hint
    assert not classify_command('python -c "1"').hint and not classify_command("rm -rf x").hint
