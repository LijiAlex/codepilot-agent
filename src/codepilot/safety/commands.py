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


@dataclass
class Classification:
    kind: Literal["allowed", "blocked", "unknown"]
    reason: str


def _segment(seg: str) -> Classification:
    try:
        argv = shlex.split(seg)
    except ValueError:
        return Classification("blocked", "unparseable command")
    if not argv:
        return Classification("allowed", "empty")
    prog = argv[0].rsplit("/", 1)[-1]
    for a in argv[1:]:
        if a.startswith("/") or ".." in a.split("/"):
            return Classification("blocked", f"path outside the sandbox: {a}")
        if SENSITIVE.search(a):
            return Classification("blocked", f"secret-like file: {a}")
    if prog in BLOCKED:
        return Classification("blocked", f"{prog} is never needed inside the sandbox")
    if prog == "rm" or (prog == "find" and FIND_BLOCKED & set(argv)):
        return Classification("blocked", "deleting or running files by shell is not allowed; use the file tools")
    if prog in {"python", "python3"}:
        if "-c" in argv:
            return Classification("blocked", "inline python is not allowed")
        if "-m" in argv:
            mod = argv[argv.index("-m") + 1] if argv.index("-m") + 1 < len(argv) else ""
            if mod in {"pip", "ensurepip", "venv", "http.server"}:
                return Classification("blocked", f"python -m {mod} is not allowed")
    if prog == "uv":
        return Classification("blocked", "package changes go through the dependency file; code installs them")
    if prog == "git":
        sub = next((a for a in argv[1:] if not a.startswith("-") and a not in {"sandbox"}), "")
        if "-C" in argv or sub in GIT_BLOCKED or any(a.startswith(("--output", "-o=")) for a in argv):
            return Classification("blocked", f"git {sub or '-C'} is not allowed")
        if sub and sub not in GIT_ALLOWED:
            return Classification("unknown", f"git {sub} is not on the allowed list")
        return Classification("allowed", "git read/stage command")
    if prog in ALLOWED:
        return Classification("allowed", prog)
    return Classification("unknown", f"{prog} is not on the allowed or blocked list")


def classify_command(cmd: str) -> Classification:
    if "$(" in cmd or "`" in cmd:
        return Classification("blocked", "command substitution is not allowed")
    if re.search(r"[<>]", re.sub(r"\d?>&\d", "", cmd)):     # any redirection except 2>&1-style stream joins
        return Classification("blocked", "redirection is not allowed; use the file tools")
    results = [_segment(s) for s in _SPLIT.split(cmd.strip()) if s]
    for kind in ("blocked", "unknown"):
        for r in results:
            if r.kind == kind:
                return r
    return Classification("allowed", "all segments allowed")
