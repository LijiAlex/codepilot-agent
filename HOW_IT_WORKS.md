# ⚙️ How CodePilot+ works

[← Back to the README](README.md)

The architecture, then one diagram for each flow: solving an issue, reviewing a pull request, improving the skills, recalling lessons, and connecting a repository.

Boxes are coloured and tagged: blue = AI agent or model, green (CODE) = plain code, orange (HUMAN) = a human decides. Tags: BRIEF = required by the assignment, BONUS = its optional extras, PLUS = beyond the brief.

<a id="architecture"></a>

## 🗺️ Architecture

![CodePilot+ architecture: GitHub issues pass the poller and injection screen; the Orchestrator, Repo Explorer, Coder, Test Agent and PR Agent work in a Daytona sandbox; a human approves; the GitHub App publishes; the reviewer Action scores the pull request; runs, lessons and skills are stored for the dashboard and the next run](docs/architecture.png)

<a id="flows"></a>

## 🛠️ Solver

![Solver flow: issue, poller, injection screen, triage, context, the agents in the sandbox, save and end, your approval, publish, pull request](docs/flows/solver.png)

## ⭐ PR review

![PR review flow: GitHub Action runs tests and an OpenAI review, code caps the score and decides merge-ready, you merge, the PR watcher saves a lesson](docs/flows/pr-review.png)

## 🔁 Self-improvement loop

![Self-improvement loop: run ends, trace analysis, skill gap report, one proposal, confidence, you decide, repo skill v+1, used by the next run](docs/flows/self-improvement.png)

## 🧠 Lessons

![Lessons: merged PRs and failed or rejected runs become lessons in ChromaDB; the 3 most similar are recalled for a new issue](docs/flows/lessons.png)

## 🔌 Connecting a repository

![Connecting a repository: check access, create labels, detect setup, build repo map, repo skill v1, code index, codepilot.yml entry](docs/flows/connect.png)
