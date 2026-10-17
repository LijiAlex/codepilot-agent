# scripts/github_smoke.py
"""Read-only live check of the GitHub App setup (no writes).
Usage: uv run python scripts/github_smoke.py OWNER NAME
Needs CODEPILOT_GH_APP_ID and CODEPILOT_GH_APP_KEY_PATH in the environment."""
from __future__ import annotations

import sys

from codepilot.gh.auth import TokenProvider
from codepilot.gh.reader import candidates, existing_work, make_wrapper


def main() -> int:
    owner, name = sys.argv[1], sys.argv[2]
    tp = TokenProvider.from_env("CODEPILOT_GH_APP_ID", "CODEPILOT_GH_APP_KEY_PATH", owner, name)
    print("installation id:", tp.installation_id)
    tp.token()
    print("repo-scoped token minted (value not shown)")
    repo = tp.repo()
    print("default branch:", repo.default_branch)
    nums = [c.number for c in candidates(repo, make_wrapper(repo), [])]
    print("ai-assignable issues (via GitHubToolkit get_issues):", nums)
    for n in nums:
        print(f"  #{n} existing branch:", existing_work(repo, n))
    print("labels:", sorted(label.name for label in repo.get_labels()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
