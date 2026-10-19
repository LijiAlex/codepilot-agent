# src/codepilot/safety/commands.py
"""Classify a shell command: allowed, blocked (never needed) or unknown (ask a person).

Each segment of a compound command (`&&`, `||`, `;`, `|`) is classified; the
worst result wins. Command substitution, backticks and output redirection are
blocked outright because they hide what actually runs or writes.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

ALLOWED = {"python", "python3", "pytest", "ls", "cat", "head", "tail", "grep", "find", "wc", "echo",
           "pwd", "true", "sort", "uniq", "diff", "cd", "git", "tree"}
GIT_ALLOWED = {"status", "diff", "log", "show", "add", "apply", "rev-parse", "ls-files", "branch", "stash", "restore", "checkout"}
GIT_BLOCKED = {"push", "remote", "fetch", "pull", "clone", "config", "submodule"}
BLOCKED = {"curl", "wget", "ssh", "scp", "sftp", "nc", "sudo", "su", "claude", "gh", "pip", "pip3", "npm", "npx",
           "yarn", "pnpm", "brew", "apt", "apt-get", "chmod", "chown", "dd", "mkfs", "kill", "pkill", "env", "printenv", "export",
           "sed", "awk", "perl", "ruby", "node", "tee"}      # sed `e`/`w` and awk `system()` run commands or write files
FIND_BLOCKED = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
SENSITIVE = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|.*\.secret|.*credentials.*|id_rsa.*|\.ssh/.*|\.netrc|\.npmrc|\.pypirc)$")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
SHELLS = {"sh", "bash", "zsh", "dash"}
WRAPPERS = {"time", "nohup", "xargs", "timeout"}          # classified by the command they run


NETWORK_OR_INSTALL = {"curl", "wget", "ssh", "scp", "sftp", "nc", "gh", "pip", "pip3", "npm", "npx", "yarn", "pnpm", "brew", "apt", "apt-get"}
PUBLISH_HINT = "Publishing happens after human approval; do not try to push, download or install."


@dataclass
class Classification:
    kind: Literal["allowed", "blocked", "unknown"]
    reason: str
    hint: str = ""                  # extra advice for the agent, only when it fits the reason (push / download / install)


def _segment(seg: str) -> Classification:
    try:
        argv = shlex.split(seg)
    except ValueError:
        return Classification("blocked", "unparseable command")
    if not argv:
        return Classification("allowed", "empty")
    prog = argv[0].rsplit("/", 1)[-1]
    if prog in WRAPPERS:
        rest = argv[1:]
        while rest and (rest[0].startswith("-") or (prog == "timeout" and rest[0][:1].isdigit())):
            rest = rest[1:]
        if not rest:
            return Classification("blocked" if prog == "xargs" else "allowed", f"{prog} with no command")
        return _segment(shlex.join(rest))
    if prog in SHELLS:
        if "-c" in argv and argv.index("-c") + 1 < len(argv):
            return classify_command(argv[argv.index("-c") + 1])
        return Classification("unknown", f"{prog} runs a script the guard cannot see")
    for a in argv[1:]:
        if a.startswith("/") or ".." in a.split("/"):
            return Classification("blocked", f"path outside the sandbox: {a}")
        if SENSITIVE.search(a):
            return Classification("blocked", f"secret-like file: {a}")
    if prog in BLOCKED:
        return Classification("blocked", f"{prog} is never needed inside the sandbox", PUBLISH_HINT if prog in NETWORK_OR_INSTALL else "")
    if prog == "rm" or (prog == "find" and FIND_BLOCKED & set(argv)):
        return Classification("blocked", "deleting or running files by shell is not allowed; use the file tools")
    if prog in {"python", "python3"}:
        if "-c" in argv or any(a.startswith("-c") and not a.startswith("--") for a in argv[1:]):
            return Classification("blocked", "inline python is not allowed")
        mods = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-m"] + [a[2:] for a in argv[1:] if a.startswith("-m") and len(a) > 2]
        for mod in mods:
            if mod.split(".")[0] in {"pip", "ensurepip", "venv"} or mod == "http.server":
                return Classification("blocked", f"python -m {mod} is not allowed", PUBLISH_HINT)
    if prog == "uv":
        return Classification("blocked", "package changes go through the dependency file; code installs them", PUBLISH_HINT)
    if prog == "git":
        sub = next((a for a in argv[1:] if not a.startswith("-") and a not in {"sandbox"}), "")
        if any(a.startswith("-C") for a in argv[1:]) or sub in GIT_BLOCKED or any(a.startswith(("--output", "-o=")) for a in argv):
            return Classification("blocked", f"git {sub or '-C'} is not allowed", PUBLISH_HINT if sub in GIT_BLOCKED else "")
        if sub and sub not in GIT_ALLOWED:
            return Classification("unknown", f"git {sub} is not on the allowed list")
        return Classification("allowed", "git read/stage command")
    if prog in ALLOWED:
        return Classification("allowed", prog)
    return Classification("unknown", f"{prog} is not on the allowed or blocked list")


def classify_command(cmd: str) -> Classification:
    if "\n" in cmd or "\r" in cmd:
        return Classification("blocked", "multi-line commands are not allowed; run one command at a time")
    if re.search(r"(?<![&>])&(?![&>])", cmd):           # a lone & runs the next command in the background
        return Classification("blocked", "background commands (&) are not allowed")
    if "$(" in cmd or "`" in cmd:
        return Classification("blocked", "command substitution is not allowed")
    if re.search(r"[<>]", re.sub(r"\d?>&\d", "", cmd)):     # any redirection except 2>&1-style stream joins
        return Classification("blocked", "redirection is not allowed; use the file tools")
    results = [_segment(s) for s in _SPLIT.split(cmd.strip()) if s]
    for kind in ("blocked", "unknown"):
        for r in results:
            if r.kind == kind:
                return _explain_unparseable(cmd) if r.reason == "unparseable command" else r
    return Classification("allowed", "all segments allowed")


def _explain_unparseable(cmd: str) -> Classification:
    """The split above is not quote-aware, so `python -c "a;b"` falls apart into halves that do not parse.
    The command stays blocked either way; this only finds the honest reason (e.g. inline python)."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return Classification("blocked", "unparseable command (unbalanced quotes)")
    segs, cur = [], []
    for t in tokens:
        if t in ("&&", "||", ";", "|"):
            segs.append(cur); cur = []
        else:
            cur.append(t)
    segs.append(cur)
    for r in (_segment(shlex.join(s)) for s in segs if s):
        if r.kind == "blocked":
            return r
    return Classification("blocked", "; | or & inside quotes is not supported; run a simpler command")
