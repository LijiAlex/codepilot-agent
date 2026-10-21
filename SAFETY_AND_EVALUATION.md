# 🛡️ Safety and evaluation

[← Back to the README](README.md)

Part 1 follows an issue through every guard; Part 2 shows how the system was evaluated and the results.

## Part 1: How CodePilot+ is kept safe

An issue passes through five stages, and each stage has its own guards. The 🏷️ Type column shows what each guard is:

| Symbol | Meaning | Guards |
|---|---|---|
| ⚙️ | Deterministic code: same input, same result | All except the four below |
| 🤖 | LLM-based judgement | The NeMo Guardrails self-check (stage 1) and the independent reviewer (stage 4) |
| 👤 | A human decides | Approving each change (stage 3) and merging (stage 4) |

### 1. Before any agent reads the issue

**Threat:** instructions planted in issue text.

| 🛡️ Guard | 🏷️ Type |
|---|---|
| Only `ai-assignable` issues, from authors in `author_allowlist` when it is set; other people's comments dropped | ⚙️ allowlist |
| Hidden content (HTML comments, invisible characters) stripped; the rest wrapped as untrusted | ⚙️ input sanitisation |
| NeMo Guardrails regex input rail | ⚙️ pattern matching |
| NeMo Guardrails self-check input rail (Claude Haiku); the issue waits if it cannot run | 🤖 LLM classifier, fail-closed |

📊 **Measured on 27 labelled issues:** 🛡️ 10/12 attacks blocked · ✅ 0/15 false positives · ⏱️ 0.8 s per check · 🔒 0/12 attacks reached an agent: the sanitiser stripped the hidden payload from both misses

### 2. While the agents work

**Threat:** a misled or mistaken agent doing harm.

| 🛡️ Guard | 🏷️ Type |
|---|---|
| Fresh Daytona sandbox per run, with no GitHub token or remote | ⚙️ sandboxing |
| Every shell command allowed, refused with a reason, or sent to a human | ⚙️ command allow/deny list |
| Fixed tool set per agent, checked on every call | ⚙️ least privilege |
| Per-agent write access (Explorer and Orchestrator read-only, Test Agent only tests); never `.github/`, `.git/` or protected paths | ⚙️ filesystem permissions |
| Secret files (`.env`, keys) refused and hidden | ⚙️ path deny list |
| Model and tool call caps, 30-minute limit, loop detector | ⚙️ resource limits |

`git push`, `rm`, `curl`, `wget`, `pip install` and paths outside the sandbox are refused outright rather than sent to a human: CodePilot+ never needs them, since publishing, file edits, network access and dependency installs all go through tools.

📊 **Measured:** 🚫 24 risky commands refused across all runs (dashboard Safety tab) · ✅ 0 of them ran · 🔁 0 retries of a refused command in the evaluation

### 3. Before anything reaches GitHub

**Threat:** an unverified or unwanted change is published.

| 🛡️ Guard | 🏷️ Type |
|---|---|
| Tests must pass after the last edit, through `run_tests` | ⚙️ verification gate |
| Fails on empty changes, protected files or weakened tests; flags more than 5 files or new dependencies | ⚙️ policy checks |
| A human approves every change in the terminal UI | 👤 human-in-the-loop |
| Issue and main branch re-checked; exactly the approved change is published | ⚙️ pre-publish validation |

📊 **Measured in the evaluation:** ✅ 152/152 rule checks (including tests after the last edit) · 🧪 6/6 hidden tests passed on the pull requests

### 4. After the pull request opens

**Threat:** a weak or risky pull request is merged.

| 🛡️ Guard | 🏷️ Type |
|---|---|
| OpenAI model (a different company from the Claude agents) scores every CodePilot+ pull request (others on request) | 🤖 LLM-as-judge |
| Score capped at 4 for security concerns, 6 if tests did not pass; merge-ready needs 8+, passing tests, at most 200 lines and no protected files | ⚙️ policy gate |
| Fork pull requests judged from the diff; their code never runs | ⚙️ isolation |
| Merge only with a current merge-ready review, clicked by a human | ⚙️ gate + 👤 human click |

📊 **Measured in the evaluation:** ⭐ 9.8/10 average review score · 🎯 4/4 judge probes on the right side of the merge-ready line

### 5. Throughout

**Threat:** secrets leaking, or actions nobody can trace.

| 🛡️ Guard | 🏷️ Type |
|---|---|
| Keys and tokens redacted from traces and logs | ⚙️ redaction |
| Dashboard on localhost only, with a token and a strict Content Security Policy | ⚙️ access control |
| Append-only audit log of every action | ⚙️ audit logging |

## Part 2: How we know it works

The test issues ran through the live system as real GitHub issues; the evaluation then scored the results against pass marks fixed in advance. Final evaluation, 20 October 2026, 30 runs: **PASS**. Full reports: [results/eval.md](results/eval.md) and [results/guardrail.md](results/guardrail.md).

| Question | Method | Result | Pass mark |
|---|---|---|---|
| Does every run follow the rules? | 12 rule-based checks per run; triage is scored on its own row (offline) | ✅ 152 of 152 | 100% |
| Does each issue end the right way? | Golden set of 14 issues (offline) | ✅ 12 of 14 (9 counting only the labelled outcome) | 60% |
| Does triage pick the right task type? | Multi-class classification accuracy (offline) | ✅ 22 of 25 | 70% |
| Do the fixes really work? | Hidden fail-to-pass tests (offline) | ✅ 6 of 6 | 100% |
| Are the pull requests good? | LLM-as-judge (online, every PR) | ✅ 9.8 / 10 | 7 / 10 |
| Can the judge be trusted? | Judge meta-evaluation, 4 probes (offline) | ✅ 4 of 4 | 4 of 4 |
| Does the injection screen work? | Binary classifier evaluation, 27 issues (offline) | ✅ 10 of 12 caught, 0 of 15 false positives | 80%, ≤ 10% |
| Has anything got worse? | Regression test against a baseline (offline) | ✅ Within 5 points | No bigger drop |

Cost, tokens and what slowed each run are traced online in LangSmith and shown on the dashboard; they are not part of the verdict.

### Running the evaluation

On demand, from the repository root:

```bash
scripts/with-env python scripts/evaluate.py            # scores the recorded runs → results/eval.md and the dashboard
scripts/with-env python scripts/measure_guardrail.py   # measures the injection screen → results/guardrail.md
```
