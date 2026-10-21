# Setting up and using CodePilot+

From nothing to CodePilot+ fixing issues on your own GitHub repository. Follow the steps in order.

## 🎯 What you will be able to do

- 🏷️ Label an issue `ai-assignable` (or type a task); AI agents fix and test it in a cloud sandbox.
- 🖥️ Watch the work live in the terminal UI, inspect each change, approve or reject it. Nothing reaches GitHub without your approval.
- ⭐ Have every CodePilot+ pull request (and any other, on request) scored by an independent AI reviewer from a different company.
- 📊 On the dashboard: merge pull requests, approve improvements to your repository's playbook, review every run.

## 🧰 What you need

- 🐍 Python 3.12 and uv (Astral's Python package manager)
- 📁 A GitHub repository of your own, where you can create and install GitHub Apps
- 🔑 API keys: Anthropic (the agents), OpenAI (the reviewer), LangSmith (tracing), Daytona (the sandbox)
- 🔧 git with access to your repository from this machine (needed for private repositories)
- 🔧 The GitHub CLI (`gh`), signed in, to install the reviewer

## 🧩 How the pieces fit together

- 🖥️ **Terminal UI** (your machine): polls GitHub through your **first GitHub App**, starts runs, and is where you approve changes.
- 📦 **Each run**: a fresh **Daytona sandbox** with a copy of your repository and no GitHub credentials.
- ✅ **On approval**: the first GitHub App opens the pull request.
- ⭐ **Reviewer**: a GitHub Action in your repository, acting as your **second GitHub App**, scores CodePilot+'s pull requests (and any other, on request) with the OpenAI model.
- 📊 **Dashboard** (your machine): everything CodePilot+ recorded; you merge pull requests and approve skill changes here.

## 1️⃣ Get the code

```bash
git clone https://github.com/LijiAlex/codepilot-agent
cd codepilot-agent
cp .env.example .env                    # your keys; never committed
cp codepilot.example.yml codepilot.yml  # watched repositories and settings
```

## 2️⃣ Add your API keys

In `.env`:

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | The agents run on Claude |
| `DAYTONA_API_KEY` | Each run gets a fresh Daytona sandbox |
| `LANGSMITH_API_KEY` | Every model call is traced |
| `OPENAI_API_KEY` | The reviewer's model. Used here by the evaluation script; the reviewer itself gets it as a GitHub secret in step 7️⃣ |

`CODEPILOT_GH_APP_ID` and `CODEPILOT_GH_APP_KEY_PATH` come in step 3️⃣.

CodePilot+ never reads `.env` itself: `scripts/with-env` loads it into the one command you run, so every command below that needs a key starts with it. It runs through `uv`, so dependencies install on the first such command.

## 3️⃣ Create the GitHub App that CodePilot+ acts as

CodePilot+ talks to GitHub only as this App, so its pull requests come from the App, not your account.

1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App.
2. Name it (for example `yourname-codepilot`), give any homepage URL, and untick Webhook → Active.
3. Repository permissions: *Contents*, *Pull requests* and *Issues* → Read and write (*Metadata* becomes Read automatically).
4. Create it. Note the App ID and the slug (the name in the page URL), then generate a private key; keep the downloaded `.pem` file outside the repository.
5. Install App → only your repository.
6. In `.env`: `CODEPILOT_GH_APP_ID` = the App ID, `CODEPILOT_GH_APP_KEY_PATH` = the full path to the `.pem` file.

## 4️⃣ Connect your repository

```bash
scripts/with-env codepilot connect OWNER/REPO --write     # for example alice/my-project
```

It checks the App can reach the repository, creates the `ai-assignable` and review labels, detects how to install and test the project, builds the repo map, skill notes and code index, and adds the repository to `codepilot.yml` (`install_command`, `test_command`, any detected `protected_paths`). Safe to run again (it then prints `labels created: none`). The first run downloads an embedding model and can take a few minutes.

Example output:

```text
connected alice/my-project (default branch main); labels created: ai-assignable, codepilot-generated, needs-review, codepilot-review-pending, codepilot-review-completed
setup: install `pip install -e . pytest`, tests `python -m pytest -q`
repo map: 2824 tokens, 70 files; repo skill v1; indexed chunks: 709; snapshot: none
added to codepilot.yml
```

- ✔️ **`setup:` line:** if the install or test command is wrong, fix `install_command` or `test_command` in `codepilot.yml`.
- ✔️ **`codepilot.yml`:** your repository is now under `repos:`. Add files the agents must never change (for example contract tests) as `protected_paths`, like the commented example.

## 5️⃣ Limit who can assign work

In your repository's entry in `codepilot.yml`:

```yaml
    author_allowlist: [your-github-login]
```

Without it, issues from anyone are accepted once labelled.

## 6️⃣ Check the agents' tools

```bash
scripts/with-env codepilot selfcheck     # expect: tool sets OK
```

## 7️⃣ Add the independent reviewer

A GitHub Action that scores CodePilot+'s pull requests, and any other when you add the `codepilot-review-pending` label, acting as a second GitHub App so its reviews are separate from CodePilot+'s pull requests.

1. Create a second App as in step 3️⃣ (for example `yourname-codepilot-reviewer`, Webhook → Active unticked) with *Pull requests* and *Issues* → Read and write, *Contents* → Read. Generate its private key and install it on the same repository.
2. Let `gh` write workflow files:
   ```bash
   gh auth refresh -s workflow
   ```
3. Preview, then write the reviewer to the repository's `main` branch (set `--install` to whatever installs your project and pytest):
   ```bash
   uv run python scripts/install_reviewer.py --repo OWNER/REPO --install "pip install -e . pytest"           # preview
   uv run python scripts/install_reviewer.py --repo OWNER/REPO --install "pip install -e . pytest" --create  # write
   ```
   Not `main`? Add `--branch YOUR-BRANCH` to both. A protected branch makes the write fail.
4. Repository Settings → Secrets and variables → Actions:
   - **Secrets:** `OPENAI_API_KEY`; `REVIEWER_APP_KEY` (the reviewer App's whole `.pem` file); `LANGSMITH_API_KEY` (traces the reviewer too).
   - **Variables:** `REVIEWER_APP_ID`; `REVIEWER_LOGIN` (reviewer App slug + `[bot]`, for example `yourname-codepilot-reviewer[bot]`); `CODEPILOT_LOGIN` (first App slug + `[bot]`).
5. In `codepilot.yml`, replace the example top-level `reviewer_login` with your `REVIEWER_LOGIN`.

## 8️⃣ Label an issue for CodePilot+

Open an issue on your repository describing a small, well-defined problem (a bug with steps to reproduce, missing documentation, a dependency to update) and label it `ai-assignable`. Only open, labelled issues from authors in `author_allowlist` are picked up.

## 9️⃣ Start the terminal UI and the dashboard

Two terminals, both in the `codepilot-agent` folder:

```bash
scripts/with-env codepilot             # terminal 1: the terminal UI
scripts/with-env codepilot dashboard   # terminal 2: the dashboard (port 8765; --port to change)
```

- 🖥️ The terminal UI polls every repository in `codepilot.yml` at start, then every 5 minutes, so it picks up your issue straight away. Each run uses your API credit. Press `i` to type a task instead of filing an issue.
- 📊 The dashboard prints a link like `http://127.0.0.1:8765/#token=…`. The link is the password: open it whole, keep it private. Reachable only from your machine.
- 🔀 Or run both in one process: `scripts/with-env codepilot --dashboard 8765`.

## 🔟 Approve the change and merge it

1. When the run finishes, the change appears in the Human Approval panel: `Tab` to that panel, select it, `v` to inspect, `a` to approve.
2. CodePilot+ opens the pull request; within minutes the reviewer posts its score.
3. Once it is labelled `merge-ready`, click Merge in the dashboard's Pull requests tab. On its next poll, the terminal UI saves a lesson from it.

## 🖥️ Using the terminal UI

![The terminal UI with its four panels](docs/tui.gif)

| Panel | Shows |
|---|---|
| 📋 GitHub Issues (top left) | Every picked-up issue and its status; the title shows the last and next poll |
| ⚙️ Active Task (top right) | The run in progress: state, agent, skill and context |
| 📜 Agent Logs (bottom left) | Every step and tool call, including issues blocked by the injection screen |
| 👤 Human Approval (bottom right) | Everything waiting for your decision |

Approving re-checks the issue and main branch, commits exactly that change and opens one labelled pull request.

Keys: `Tab` move between panels · `v` inspect · `a` approve · `r` reject · `i` new task · `s` skip issue · `l` full log · `q` quit.

## 📊 Using the dashboard

![The dashboard's Overview tab](docs/dashboard.png)

| Tab | What you see or do |
|---|---|
| Overview | Issues solved, average review score, refused commands, skill changes, cost |
| Runs | Every run and its stages; open one for its diff, agent log and what slowed it down |
| Pull requests | Open pull requests and review scores; **merge** or **close** once merge-ready |
| Evaluation | The evaluation report and verdict, after `scripts/evaluate.py` has run |
| Safety | Every refused command and the rule that refused it |
| Audit log | Every action by an agent, a human or the system; cannot be edited |
| Improvements | Proposed changes to your repository's skill notes; **approve** or **reject** |
| Memory | Lessons from merged pull requests and failed runs |
| Skills | Task playbooks and every version of your repository's skill notes |
| Context | Context each run was given, against its budget |

## 🏷️ The labels CodePilot+ uses

`connect` creates the first five; the reviewer adds `merge-ready` the first time a pull request earns it.

| Label | On | Added by | Means |
|---|---|---|---|
| `ai-assignable` | Issue | You | Hand this issue to CodePilot+ |
| `codepilot-generated` | PR | CodePilot+, when opening the PR | Written by CodePilot+ |
| `needs-review` | PR | CodePilot+ when opening; the reviewer on any PR no human has approved yet | A human still has to review it; removed once a human approves on GitHub |
| `codepilot-review-pending` | PR | CodePilot+ when opening; you, to ask again | Requests a review; removed when the review is posted |
| `codepilot-review-completed` | PR | The reviewer | The review is posted |
| `merge-ready` | PR | The reviewer | Score 8+, tests pass, no security concerns, at most 200 lines changed, none of the reviewer's protected files (`.github/`, `.env`, keys, `uv.lock`, `poetry.lock`) touched, and one human approval if a person opened it; mergeable from the dashboard; removed if that stops being true |

## 🧪 Running the tests

```bash
uv run pytest     # the project's automated tests; no keys needed
```
