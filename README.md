# CodePilot+

**An AI coding agent that picks up GitHub issues, fixes them in a sandbox, tests the fix, and opens a pull request — but only after a person approves it.**

Engineering teams lose hours every sprint to small, well-defined issues: a bug flagged by a test, a dependency bump, a missing paragraph in the docs. CodePilot+ takes those off the queue. You label an issue `ai-assignable`; a team of AI agents reads it, finds the relevant code, makes the change in an isolated copy of the repository, runs the tests, and prepares a pull request. Nothing reaches GitHub until you approve it in the terminal.

FILL: demo video link (5–7 minutes)

![CodePilot+ in the terminal: a poll runs, issue #13 is blocked by the injection screen, issue #25 is claimed and triaged](docs/tui.gif)

## How it works

```mermaid
flowchart LR
  I[GitHub issue<br/>labelled ai-assignable] --> S[Safety screen]
  S --> T[Triage: what kind of task?]
  T --> A[Agents explore, code, test<br/>in a sandbox]
  A --> H{You approve<br/>in the terminal}
  H -- yes --> PR[Pull request on GitHub]
  PR --> R[Independent AI review]
  PR -- merged --> M[Lesson saved to memory]
```

1. **Watch.** Every 5 minutes CodePilot+ checks your repositories for open issues labelled `ai-assignable`.
2. **Screen.** The issue text is cleaned and checked for prompt injection (instructions hidden in an issue to trick the AI) before any agent reads it.
3. **Triage.** The issue is sorted into one of five task types — bug fix, feature, dependency update, documentation, configuration — and given a complexity score. The type decides which playbook (a *skill*) the agents follow. Issues that look too complex get a written plan for you to approve instead of code.
4. **Solve.** A team of agents works in a fresh sandbox (a throw-away copy of the repository on a remote machine, with no GitHub access): one maps the repository and finds the relevant files, one writes the change, one writes and runs tests.
5. **Approve.** When the change is ready — or when the agents want to do something unusual — the run saves its work and the item appears in the terminal's approval panel. You inspect the diff and approve or reject. Nothing waits idle in the meantime.
6. **Publish.** After approval, CodePilot+ re-checks the issue and the main branch, then commits exactly the approved change and opens one pull request, labelled and linked to the issue.
7. **Learn.** A separate AI reviewer scores every pull request. When a pull request is merged, CodePilot+ saves a short lesson it can recall the next time it meets a similar issue.

## Architecture

![CodePilot+ architecture](docs/architecture.png)

Blue boxes are AI agents or models, green boxes are plain code, and orange boxes are where a person decides. Pink PLUS tags mark what goes beyond the assignment brief.

## What's inside

**The agent team.** Built with [DeepAgents](https://github.com/langchain-ai/deepagents) on LangGraph. An Orchestrator plans each task as a checklist and hands work to a Repo Explorer, a Coder, a Test Agent and a PR Agent. Each agent gets only the tools it needs; the list is checked every time the app starts.

**Context that fits.** A real repository does not fit in a model's context window. CodePilot+ builds a *repo map* — the folder tree plus a one-line summary and the main functions of each file — ranked by relevance and cut to a 4,000-token budget. Agents read files only when they need them. On Django (7,084 files) the map is about 3,978 tokens (estimated at four characters per token). Long agent conversations are summarised automatically: DeepAgents adds its summarization middleware to every agent by default.

**Safety.** Several independent layers:
- Issue text is cleaned (hidden comments, invisible characters) and screened by [NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails); if the screen cannot answer, the issue is held back.
- Every shell command an agent runs is checked first. Commands that are never needed (`rm -rf`, `curl`, `pip install`, `git push`, reading `.env`) are refused with a reason; commands nobody has classified are paused for you to decide.
- Agents can only write inside the sandbox, and never to secret files.
- Your approval is required to open a pull request, for any change touching more than 5 files, and before a third attempt after tests fail twice.

**Memory.** Three kinds, as people have:
- *Working memory* — the current task's state, shared between the agents and cleared when the task ends.
- *Session memory* — a summary of each session, read at start-up so recently failed issues are not retried straight away.
- *Lessons* — what worked (from merged pull requests) and what went wrong (from failures and rejections), stored per repository and task type. The three most similar are recalled for a new issue and given to the Orchestrator for planning and to the agents that do the work, marked as untrusted text because they quote issue titles.

**It improves itself — with your sign-off.** After each run, CodePilot+ looks at what slowed the agents down (a refused command, a file they could not find) and proposes a change to that repository's own skill notes — add, correct or retire one step, never a rewrite. Proposals that keep coming back gain confidence; none take effect until you approve them on the dashboard.

**An independent reviewer.** A GitHub Action reviews every pull request — CodePilot+'s and people's — with a different AI model from a different company, so the system never grades its own work. It scores five criteria (correctness, tests, matches the issue, risk, scope), rates how much work the change is to review (1–5, computed from the size of the diff, not by the AI), comments on specific lines, and labels the pull request `merge-ready` only when the score, the tests and the merge policy all agree. Pull requests from forks are reviewed from the diff alone; their code is never run.

**A dashboard.** A local web page for everything the terminal does not show: every run and the stages it reached, open pull requests with their reviews (merge or close from there), every refused command and the rule behind it, memory and skills, the evaluation report, cost per run, and a complete audit log of who did what.

![Dashboard overview: issues solved, average review score, refused commands, skill changes and cost](docs/dashboard.png)

## Results

The final evaluation (20 October 2026) covered 30 runs on 15 issues across the two test repositories: the 14 test issues written in advance for `codepilot-playground`, and one real issue on `layered-memory`. **Verdict: PASS** — every signal cleared its line. Full report: [`results/eval.md`](results/eval.md).

| Signal | Result | Must reach |
|---|---|---|
| Rule checks passed | 100% (152 checks) | 100% |
| Test issues that reached their expected outcome | 86% (12 of 14) | 81% |
| Triage picked the right task type | 88% (25 runs) | 83% |
| Hidden tests passed | 100% (6 pull requests) | 100% |
| AI judge's mean score on CodePilot+'s pull requests | 9.8 / 10 (10 pull requests) | 9.3 / 10 |
| Judge self-test probes answered correctly | 4 of 4 | 4 of 4 |
| Attacks blocked by the injection screen | 83% (10 of 12) | 78% |
| Harmless issues wrongly blocked | 0% (0 of 15) | at most 10% |

The lines for the four model-scored signals (outcome, triage, judge, attacks) sit 5 points below this run's result, which is recorded as the baseline in [`results/eval_baseline.json`](results/eval_baseline.json); a later run fails if any of them drops by more than that. The other lines are fixed.

The evaluation combines:

| Part | What it checks | Uses an AI model? |
|---|---|---|
| Rule checks | 13 checks on every run: branch name, commit format, pull request description and labels, tests passed after the last change, files touched, no secrets in the diff, no refused command tried again, right skill loaded, repo map within budget | no |
| Test issues with known outcomes | 14 issues written in advance, each with the outcome CodePilot+ should reach — a pull request, a plan, a question for you, or a refusal | no |
| Hidden tests | For each pull request, a test the agents never saw is added to its code afterwards and must pass | no |
| AI judge | The independent reviewer's score on CodePilot+'s pull requests | yes |
| Judge self-test | Four fixed pull requests the judge must get right: three bad ones (a test deleted to make the suite pass, a change unrelated to the issue, a hard-coded key) and one good one | yes |
| Injection screen | 27 labelled issue texts, 12 of them attacks | yes |

Injection screen on the 27 labelled issues — the harmless ones are deliberately code-heavy (stack traces, shell snippets, words like "ignore" and "delete"), because those are the likely false alarms:

| Measure | Result |
|---|---|
| Attacks blocked by the screen | 10 of 12 (the other 2 hid their payload in places the cleaning step removes first, so none of the 12 reach an agent) |
| Harmless issues wrongly blocked | 0 of 15 |
| Median / slow (95th percentile) check time | 0.8 s / 1.3 s |

Example pull requests opened by CodePilot+ and merged after review:

- [layered-memory #1](https://github.com/LijiAlex/layered-memory/pull/1) — keep accented letters in theme slugs instead of turning them into dashes.
- [codepilot-playground #19](https://github.com/LijiAlex/codepilot-playground/pull/19) — the same identifier is reported twice when the answer repeats it (scored 10/10 by the independent reviewer).
- [codepilot-playground #14](https://github.com/LijiAlex/codepilot-playground/pull/14) — a citation marker reported out of range when the answer used three passages.

FILL: `![One run traced in LangSmith](docs/langsmith.png)` — one run from Orchestrator to PR Agent; inputs and outputs are redacted before upload.

## Try it

**You need:** Python 3.12, [uv](https://docs.astral.sh/uv/), a GitHub App installed on your repositories (permissions: Contents, Pull requests and Issues read & write; Metadata read), an Anthropic API key, and a [Daytona](https://www.daytona.io/) key for the sandbox (or `sandbox: local` for a weaker local sandbox on macOS). Optional: a LangSmith key for tracing, an OpenAI key for the independent reviewer.

```bash
git clone https://github.com/LijiAlex/codepilot-agent && cd codepilot-agent
cp .env.example .env                       # fill in your keys
cp codepilot.example.yml codepilot.yml     # list your repositories and their test commands
scripts/with-env codepilot connect OWNER/REPO --write   # labels, setup check, repo map, skill notes
scripts/with-env codepilot selfcheck       # every agent has exactly its intended tools
scripts/with-env codepilot                 # the terminal app
scripts/with-env codepilot dashboard       # the dashboard; open the link it prints
```

`scripts/with-env` loads `.env` into the command's environment; CodePilot+ itself never reads the file.

In the terminal app: label an issue `ai-assignable` and wait for the next check (shown in the issues panel title), or press `i` to type a task yourself. Approve with `a`, reject with `r`, inspect with `v`, skip an issue with `s`, open the full log with `l`, quit with `q`.

To add the independent reviewer to a repository: `uv run python scripts/install_reviewer.py --repo OWNER/REPO --install "pip install -e . pytest" --create`, then add the secrets `OPENAI_API_KEY` and `REVIEWER_APP_KEY` and the variables `REVIEWER_APP_ID`, `REVIEWER_LOGIN` and `CODEPILOT_LOGIN` in the repository's settings.

Run the tests: `uv run pytest` (431 tests).

## Design choices

- **Nobody waits.** When a person's decision is needed, the run saves its work and ends; your decision starts a new run from where it stopped. No sandbox sits idle while you are away.
- **The agents never touch GitHub directly.** They draft the commit message and pull request text; the commit itself is built by code from exactly the change you approved.
- **No credentials in the sandbox.** The repository is copied into it without any GitHub token, so a misbehaving agent has nothing to push with.
- **"Tested" means tested after the last edit.** A passing test run before a later change does not count.
- **Untrusted text stays text.** Issue and pull request text is never rendered as formatting in the terminal or as HTML on the dashboard.
- **Approvals cover what you approved.** Approving a command (for example `make lint`) allows that command for the rest of the run — nothing broader, and never a blocked one.

## Known limitations

- The remote sandbox can reach the internet on the current Daytona plan. The command check refuses network tools and the sandbox holds no credentials, but outbound traffic itself is not blocked.
- The local sandbox (macOS only) is weaker than the remote one.
- The command check is a filter, not the security boundary: running the project's own tests runs its code by design, so the sandbox is the boundary.
- Runs are not repeatable: the model does not support a fixed sampling temperature, so the same issue can be handled differently twice.
- LangSmith keeps traces for 14 days; the screenshot stands in for a link.
- The reviewer's test job runs the pull request's own code, so a pull request from the same repository could fake its test-result file. Fork pull requests never run code; for same-repository ones, the person who merges is the final check.
- The command check does not expand shell variables or `~` (for example `echo $X`). The sandbox environment holds no secrets, so this exposes nothing, but it is a gap in the filter.

## Future improvements

- **Share lessons across repositories.** When the same lesson has been approved for two or more repositories, propose it for the general playbook — additions only, with their own approval.
- **Steadier triage.** Remember an issue's first classification, or classify twice and take the majority, so retries are consistent.
- **Block outbound network in the sandbox** with a paid sandbox tier or an allow-list.
- **Spending limits in dollars** per run and per day (today each agent call is capped in model calls and tool calls, and each run has a 30-minute limit).
- **An autonomous mode.** Open pull requests without waiting for approval and let the independent reviewer and the merge button be the human checkpoint. The trade-off: agent-written code and text would appear on GitHub before anyone has read it, and a bad run would open pull requests someone has to close. (The assignment requires approval before opening a pull request to `main`, so this version always asks.)
- **Run tests for fork pull requests** in a separate sandbox with no secrets, so the reviewer can score them fully.

## For reviewers: the assignment brief

CodePilot+ was built for the AI Engineering Bootcamp capstone brief *Multi-Agent Coding Platform (CodePilot)*. Every requirement is addressed; the table lists where the implementation deliberately differs and why.

| The brief asks for | CodePilot+ does | Why |
|---|---|---|
| The approval panel waits for a keypress | The item waits; the run saves its work and ends | No idle sandbox or agent while a person is away |
| Ask a person before `git push` and on every blocked command | `git push` is refused (the only way to GitHub is the approved publish step); never-needed commands are refused with a reason; only unclassified commands ask a person | Same protection, far fewer interruptions |
| The PR Agent commits with the GitHub Toolkit's write tools | The PR Agent drafts the text; fixed tools open one commit and one pull request after approval | The Toolkit commits one file per call, cannot add labels, and its tool names are rejected by AI providers |
| A local sandbox holding part of the repository | A remote Daytona sandbox per run with the whole repository; a local sandbox as fallback | The local option has no real isolation, and tests need the whole repository |
| The Orchestrator polls GitHub itself | Polling runs in plain code; the Orchestrator sees only the issue it claimed | Cheaper, and untrusted issues reach no agent before screening |
| Subagents spawn subagents | The Coder is a full agent that runs the Test Agent | DeepAgents gives simple subagents no way to spawn others |
| Skills through `deepagents.skills` | Skills are structured files (name, instructions, workflow steps, example prompts, forbidden actions) loaded by CodePilot+ | Needed the brief's structured fields and per-task selection |
| The GitHub Toolkit (`langchain-community`) | Used for reading issues only | The package warns it is no longer actively maintained |
| The diff preview in `working/proposed_diff.txt` | In `/shared/proposed_diff.txt`, the agents' shared working folder | Same folder as the repo map and task state |
| Skip issues above the complexity threshold (bonus) | Labelled issues above it get a plan for you to approve; unlabelled ones are skipped | A person asked for the work, so a plan is more useful than silence |

Bonus items done: self-healing tests (a test-doctor agent fixes broken test setup once), complexity scoring, LangSmith tracing, and the Daytona cloud sandbox.

## Credits

The issue screen reuses my earlier bootcamp project, [guardrail-eval-pipeline](https://github.com/LijiAlex/guardrail-eval-pipeline), as a package; the test repository `codepilot-playground` is a copy of it with deliberately planted defects. Built with DeepAgents, LangGraph, LangChain, NeMo Guardrails, Textual, ChromaDB, PyGithub and Daytona.
