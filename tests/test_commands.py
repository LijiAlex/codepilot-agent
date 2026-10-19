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


@pytest.mark.parametrize("cmd,rule", [
    ('python -c "import pydantic;print(pydantic.VERSION)"', "inline-python"), ("python -m pip show pydantic", "pip-install"),
    ("uv add x", "pip-install"), ("curl http://x", "network"), ("git push origin main", "git-remote"), ("rm -rf x", "delete-files"),
    ("cat ../../.env", "outside-sandbox"), ("cat sandbox/.env", "secret-file"), ("echo $(curl x)", "command-substitution"),
    ("ls > x", "redirection"), ("echo a\ncurl b", "multi-line"), ("echo a & curl b", "background"), ("sudo ls", "blocked-tool"),
    ("make test", "unknown-command"),
])
def test_every_refusal_names_the_rule_that_fired(cmd, rule):
    from codepilot.safety.commands import rule_for
    assert rule_for(classify_command(cmd).reason) == rule


def test_an_approved_part_is_allowed_in_any_combination_but_nothing_else():
    from codepilot.safety.commands import unknown_parts
    approved = unknown_parts("cd sandbox && make lint; echo exit=$?")          # live 2026-10-19, issue #9
    assert approved == frozenset({"make lint"})
    assert classify_command("cd sandbox && make lint", approved).kind == "allowed"
    assert classify_command("cd sandbox && make lint; grep -n python Makefile", approved).kind == "allowed"
    assert classify_command("make test", approved).kind == "unknown"                 # only what was approved
    assert classify_command("make lint && rm -rf build", approved).kind == "blocked"  # approval never unblocks
    assert classify_command("cd sandbox && make lint").kind == "unknown"             # nothing approved: still asks


def test_an_output_join_does_not_change_what_was_approved():
    # live 2026-10-20, issue #9: `make lint 2>&1 | tail` was approved as "make lint '2>&1'", so plain `make lint` asked again
    from codepilot.safety.commands import unknown_parts
    approved = unknown_parts("cd sandbox && make lint 2>&1 | tail -n 20")
    assert approved == frozenset({"make lint"})
    assert classify_command("cd sandbox && cat Makefile && make lint; echo done", approved).kind == "allowed"
    assert classify_command("make lint 2>&1", frozenset({"make lint"})).kind == "allowed"
    old = unknown_parts("make lint '2>&1'")                       # approvals already stored the old way still work
    assert classify_command("make lint", old).kind == "allowed"


def test_read_only_inspection_commands_do_not_need_a_person():
    # live 2026-10-20, issue #9: the agent paused to ask about `od -c Makefile` and `git check-ignore -v Makefile`
    assert classify_command("cd sandbox && od -c Makefile").kind == "allowed"
    assert classify_command("git check-ignore -v Makefile").kind == "allowed"
