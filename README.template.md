# CodePilot+

CodePilot+ turns GitHub issues labelled `ai-assignable` into tested pull requests. A team of AI agents (built with DeepAgents and LangGraph) explores the repository, changes the code in an isolated sandbox, runs the tests and drafts the pull request; a person approves every change in a terminal UI before anything reaches GitHub. Tags used below: **Brief** = required by the assignment, **Bonus** = the assignment's bonus list, **Plus** = added by CodePilot+.

## Demo

FILL: YouTube (unlisted) link to the ~7-minute demo, then the TUI GIF (under 5 MB): `![CodePilot+ TUI](docs/tui.gif)`.

## Setup

    uv tool install git+https://github.com/LijiAlex/codepilot-agent
    cp codepilot.example.yml codepilot.yml        # list your repositories, test and install commands
    export ANTHROPIC_API_KEY=...                   # solver model
    export CODEPILOT_GH_APP_ID=... CODEPILOT_GH_APP_KEY_PATH=~/.codepilot/keys/app.pem
    export DAYTONA_API_KEY=...                     # remote sandbox (or set `sandbox: local`)
    export AWS_PROFILE=...                         # Bedrock injection guardrail
    codepilot selfcheck && codepilot

GitHub App permissions: Contents, Pull requests and Issues read & write; Metadata read; no Workflows. FILL: anything that changed during the build.

## Architecture

FILL: `docs/architecture.png` (exported from the reviewer brief's block diagram) and two sentences: one process (TUI + poller + worker + PR watcher over SQLite); one Daytona box per run.

## How it works

1. **Poll (Brief)** every 5 minutes through the GitHub Toolkit; skip issues already in progress, failed recently or already on a branch; sanitise and screen the text (Brief guardrails, A3 injection check).
2. **Triage (Brief, Bonus complexity score)**: one of five task types picks a skill; above the threshold a read-only plan goes to the TUI first (Plus).
3. **Explore → code → test (Brief)**: Repo Explorer (repo map + keyword/embedding search) → Coder → Test Agent, in a fresh sandbox with no credentials.
4. **Gates (Brief)**: nothing waits on a person — a decision becomes a TUI item and the run ends (Plus).
5. **Publish (Brief)**: after approval, code re-checks the issue and `main`, then the PR Agent opens one commit and one PR with labels and a review request.
6. **Memory (Brief)**: working state per run, episodes per session, lessons from merged PRs (Plus: also from failures and rejections).

## Example pull request

FILL: link to a merged CodePilot+ PR on `layered-memory` and one on `codepilot-playground`.

## Guardrails

FILL: paste `results/guardrail.md` (malicious blocked, benign wrongly blocked, failed-closed, latency) and the scoreboard rows for refused commands and unexpected-command decisions.

## Results

FILL: paste `results/playground.md` (expected outcome reached, hidden tests passed, tokens and minutes per issue) and `scripts/scoreboard.py` output.

Context engineering (FILL from the dashboard's Context page and `scripts/map_proof.py`): repo map tokens vs the 4,000 budget, files shown, top-K = 10 files returned by the Explorer, files read before the right one, lessons recalled per run, and the large-repository proof (Django: 7,084 files → 3,978 tokens).

## Observability

FILL: `![LangSmith trace](docs/langsmith.png)` — one run's trace (Orchestrator → Explorer → Coder → Test Agent → PR Agent); inputs and outputs are redacted before upload. Every action is also in the local audit log (`codepilot dashboard` → Audit).

## Key design decisions

- Handoffs instead of waiting: a run never holds a sandbox while a person decides.
- Fixed publish tools: the commit is built from the exact approved change; the agent supplies only text.
- Per-agent tool lists and write permissions, checked at every start-up.
- Remote sandbox per run, cloned from a git bundle: no GitHub credential ever enters it.
- FILL: anything decided during the build.

## Known limitations

- Daytona's free tier cannot block outbound network traffic; the sandbox holds no credentials and the shell guard refuses network tools, but egress itself is open (measured: FILL).
- The local fallback sandbox (macOS `sandbox-exec`: no network, no writes outside the run, CodePilot+'s own files unreadable) is weaker than a remote box.
- The command guard is a policy filter, not the security boundary: `python` and `pytest` run repository code by design, so the sandbox is the boundary.
- The Independent Reviewer skips pull requests from forks, and its "tests green" signal comes from a job that runs the PR's own code.
- LangSmith keeps traces 14 days; README screenshots stand in for links.
- FILL: limits found while testing.

## Brief vs CodePilot+

FILL: copy the table from spec §17 (approval panel waits → handoffs; git-push prompt → publish approval; Toolkit write tools → fixed tools; NeMo → A3 injection guardrail; local sandbox → Daytona with local fallback; `Permission` → `FilesystemPermission`; subagents spawning subagents → compiled Coder; Toolkit reads only; triage before the graph) and add these rows:
- Polling with the Toolkit runs in plain code, not inside the Orchestrator agent (cheaper; the agent only sees its claimed issue).
- Skills are structured objects loaded by CodePilot+ (`codepilot:` front matter) rather than mounted through `deepagents.skills`.
- Labelled issues above the complexity threshold get a plan for a person to approve instead of being skipped; unassigned ones above it are skipped (brief bonus).
- "In-progress issue IDs" are database claims (one active run per issue), shared by every thread.
- The repo map is built by code at run start and written to `/shared/repo_map.md` (the agents' virtual file system), not by the Explorer with `write_file`.
- Blocked commands are refused with a reason, not escalated; only commands nobody has classified go to a person.
- Lint-on-edit uses `py_compile`; the repo map's LLM file descriptions are off; a release regex scan replaces gitleaks.

## Credits

Injection guardrail: my Assignment 3 project, used as a package (`guardrail-eval-pipeline` v0.2.0). Playground: a copy of that project with planted defects. Built with DeepAgents, LangGraph, LangChain, Textual, ChromaDB, PyGithub and Daytona.
