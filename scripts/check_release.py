# scripts/check_release.py
"""Pre-publish hygiene for the public repo (pre-mortem E1, E12, E14, E16, E17). Read-only; prints problems.
Usage: uv run python scripts/check_release.py [--forbidden ~/.codepilot/release-forbidden.txt]
The forbidden-words file lives OUTSIDE the repo (one word per line, case-insensitive): listing them here would leak them."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

NOREPLY = "59106185+LijiAlex@users.noreply.github.com"
README_SECTIONS = ["## 🎬 See it in action", "## 📦 What's included", "## ⚙️ How it works", "## 🧱 Tech stack", "## 🚀 Setup and usage",
                   "## 🛡️ Safety and evaluation", "## 🧭 Key design decisions", "## ⚠️ Known limitations", "## 🙏 Credits"]  # demo, setup, diagram, decisions
SECRET_PATTERNS = [re.compile(p) for p in (r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-ant-[A-Za-z0-9\-_]{20,}",
                                           r"sk-[A-Za-z0-9]{32,}", r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]
MAX_MEDIA = 5 * 1024 * 1024


def tracked(repo: Path) -> list[str]:
    return subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True).stdout.splitlines()


def check(repo: Path, forbidden: list[str]) -> list[str]:
    problems: list[str] = []
    files = tracked(repo)
    words = [w.strip().lower() for w in forbidden if w.strip() and not w.startswith("#")]
    for rel in files:
        p = repo / rel
        name = rel.rsplit("/", 1)[-1]
        if name == ".env" or name.startswith(".env.") and name != ".env.example" or rel.endswith((".pem", ".key")) \
                or rel.endswith(".db") or "/.codepilot/" in f"/{rel}":
            problems.append(f"secret-like or local-state file is tracked: {rel}")
        if p.suffix.lower() in (".gif", ".mp4", ".mov", ".png") and p.stat().st_size > MAX_MEDIA:
            problems.append(f"media over 5 MB (GitHub limit 10 MB, README GIF target 5 MB): {rel}")
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        low = text.lower()
        for w in words:
            if w in low:
                problems.append(f"forbidden word {w!r} in {rel}")
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                problems.append(f"possible secret ({pat.pattern[:12]}…) in {rel}")
    emails = set(subprocess.run(["git", "log", "--format=%ae%n%ce"], cwd=repo, capture_output=True, text=True, check=True).stdout.split())
    for e in sorted(emails - {NOREPLY, "noreply@anthropic.com", "noreply@github.com"}):
        problems.append(f"commit email is not the personal noreply address: {e}")
    readme = repo / "README.md"
    if not readme.exists():
        problems.append("README.md missing")
    else:
        text = readme.read_text()
        problems += [f"README section missing: {s}" for s in README_SECTIONS if s not in text]
        if "FILL:" in text:
            problems.append(f"README still has {text.count('FILL:')} FILL: marker(s)")
        tier = re.compile(r"\b(core|stretch)\s+(goals?|features?|tiers?|scope|items?)\b|\bon hold\b", re.I)
        for m in tier.finditer(text):
            problems.append(f"README mentions a build tier ({m.group(0)!r}); use Brief / Bonus / Plus only")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--forbidden", default=str(Path.home() / ".codepilot" / "release-forbidden.txt"))
    a = ap.parse_args(argv)
    fp = Path(a.forbidden).expanduser()
    if not fp.exists():
        print(f"forbidden-words file not found: {fp} (create it outside the repo first)")
        return 2
    problems = check(Path(a.repo), fp.read_text().splitlines())
    for p in problems:
        print("PROBLEM:", p)
    print("release check passed" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
