# playground/file_issues.py
"""File the playground issues with the GitHub CLI (run by the user; outward-facing).
Usage: uv run python playground/file_issues.py --repo LijiAlex/codepilot-playground            # dry run: print only
       uv run python playground/file_issues.py --repo LijiAlex/layered-memory --issues playground/layered-memory.yaml \
              --filed playground/filed-layered-memory.json [--create]
       uv run python playground/file_issues.py --repo LijiAlex/codepilot-playground --create   # creates labels + issues
Writes playground/filed.json (issue key → number) for scripts/evaluate_playground.py."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent
LABELS = {"ai-assignable": "0e8a16", "codepilot-generated": "5319e7", "needs-review": "fbca04"}


def number_from_url(url: str) -> int:
    m = re.search(r"/issues/(\d+)\s*$", url.strip())
    if not m:
        raise ValueError(f"not an issue URL: {url!r}")
    return int(m.group(1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--only", nargs="*", help="issue keys to file (default: all)")
    ap.add_argument("--issues", default=str(HERE / "issues.yaml"))
    ap.add_argument("--filed", default=str(HERE / "filed.json"))
    a = ap.parse_args(argv)
    issues = [i for i in yaml.safe_load(Path(a.issues).read_text())
              if (i["key"] in a.only if a.only else not i.get("hold_back"))]          # held-back issues only via --only
    filed_path = Path(a.filed)
    filed = json.loads(filed_path.read_text()) if filed_path.exists() else {}
    if a.create:
        for name, color in LABELS.items():
            subprocess.run(["gh", "label", "create", name, "--repo", a.repo, "--color", color, "--force"], check=True)
    for it in issues:
        if it["key"] in filed:
            print(f"skip {it['key']}: already #{filed[it['key']]}")
            continue
        cmd = ["gh", "issue", "create", "--repo", a.repo, "--title", it["title"], "--body", it["body"]]
        for label in it["labels"]:
            cmd += ["--label", label]
        if not a.create:
            print("would run:", " ".join(cmd[:6]), "…")
            continue
        url = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        filed[it["key"]] = number_from_url(url)
        filed_path.write_text(json.dumps(filed, indent=1))
        print(f"{it['key']} → #{filed[it['key']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
