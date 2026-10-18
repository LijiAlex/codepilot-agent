# scripts/install_reviewer.py
"""Copy the independent reviewer into a target repository through the GitHub API (no git push).

    uv run python scripts/install_reviewer.py --repo OWNER/NAME --install "pip install -e . pytest" [--create]

Without --create it only lists what would be written. Writing .github/workflows needs a gh token with the
`workflow` scope (`gh auth refresh -s workflow`). Secrets and variables are set separately by the repo owner.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from pathlib import Path

REVIEWER = Path(__file__).resolve().parent.parent / "reviewer"


def files_for(install: str) -> dict[str, str]:
    wf = (REVIEWER / "workflow.yml").read_text()
    wf = re.sub(r'(?m)^(\s*INSTALL:\s*)".*?"', lambda m: f'{m.group(1)}"{install}"', wf, count=1)
    policy = (REVIEWER / "policy.yaml").read_text()
    return {".github/workflows/codepilot-review.yml": wf,
            ".github/codepilot-review/review.py": (REVIEWER / "review.py").read_text(),
            ".github/codepilot-review/requirements.txt": (REVIEWER / "requirements.txt").read_text(),
            ".github/codepilot-review/policy.yaml": policy,
            ".github/codepilot-review.yaml": policy}


def put(repo: str, path: str, text: str, branch: str) -> str:
    sha = subprocess.run(["gh", "api", f"repos/{repo}/contents/{path}?ref={branch}", "--jq", ".sha"],
                         capture_output=True, text=True).stdout.strip()
    body = {"message": f"Add CodePilot+ independent reviewer: {path}", "branch": branch,
            "content": base64.b64encode(text.encode()).decode()}
    if sha and not sha.startswith("{"):
        body["sha"] = sha
    r = subprocess.run(["gh", "api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"],
                       input=json.dumps(body), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{path}: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout)["commit"]["sha"][:7]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--install", required=True, help="install command for the repo's tests job")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--create", action="store_true")
    a = ap.parse_args(argv)
    for path, text in files_for(a.install).items():
        print(f"{'write' if a.create else 'would write'} {a.repo}:{path} ({len(text)} bytes)"
              + (f" → {put(a.repo, path, text, a.branch)}" if a.create else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
