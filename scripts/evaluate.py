# scripts/evaluate.py
"""CodePilot+ evaluation: rule checks on every run, the independent reviewer as judge, judge probes, golden issues with
hidden tests, and the injection-screen measurement — one verdict, written to results/eval.json and results/eval.md.

Usage: scripts/with-env python scripts/evaluate.py [--python PLAYGROUND_PYTHON] [--reuse-probes | --no-probes]
       [--no-github] [--set-baseline]
  --python        interpreter with the playground's dependencies; without it hidden tests read `unavailable`
  --reuse-probes  keep the probe results from the last results/eval.json (no model call)
  --set-baseline  record this run's model-scored figures as the baseline (results/eval_baseline.json)
Reads only: SQLite, run folders, GitHub (GitHub App token). The only model calls are the 4 probes (OpenAI, the
reviewer's own model)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_playground import evaluate as golden_evaluate  # noqa: E402

from codepilot.config import load_settings  # noqa: E402
from codepilot.evals import collect, heuristics, judge, report  # noqa: E402

PLAYGROUND = "LijiAlex/codepilot-playground"


def main(argv=None) -> int:
    home = Path.home() / ".codepilot"
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(home / "codepilot.db"))
    ap.add_argument("--config", default=str(ROOT / "codepilot.yml"))
    ap.add_argument("--python", default=None)
    ap.add_argument("--reuse-probes", action="store_true")
    ap.add_argument("--no-probes", action="store_true")
    ap.add_argument("--no-github", action="store_true")
    ap.add_argument("--set-baseline", action="store_true")
    ap.add_argument("--out-dir", default=str(ROOT / "results"))
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    s = load_settings(Path(a.config))
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    issues = yaml.safe_load((ROOT / "playground" / "issues.yaml").read_text())
    filed = json.loads((ROOT / "playground" / "filed.json").read_text())
    golden_types = {(PLAYGROUND, f"#{filed[i['key']]}"): i["type"] for i in issues if i["key"] in filed}

    repo_factory = None
    if not a.no_github:
        from codepilot.gh.auth import TokenProvider
        cache = {}

        def repo_factory(full: str):
            if full not in cache:
                owner, name = full.split("/")
                cache[full] = TokenProvider.from_env(s.github_app_id_env, s.github_app_key_path_env, owner, name).repo()
            return cache[full]

    facts, judged = collect.run_facts(conn, home / "runs", golden_types, repo_factory, s.reviewer_login)
    policy = yaml.safe_load((ROOT / "reviewer" / "policy.yaml").read_text())
    per_run = [heuristics.check_run(f, s.repo_map_budget_tokens, policy["protected"]) for f in facts]

    owner, name = PLAYGROUND.split("/")
    golden = golden_evaluate(conn, issues, filed, PLAYGROUND, home / "runs", home / "repos" / owner / name / "mirror", a.python)
    golden = collect.golden_ended(conn, PLAYGROUND, golden)
    hidden = [g["resolved"] for g in golden if g.get("resolved") is not None]

    prev = out / "eval.json"
    if a.no_probes:
        probes = []
    elif a.reuse_probes and prev.exists():
        probes = json.loads(prev.read_text()).get("probe_rows", [])
    else:
        from langchain_openai import ChatOpenAI
        probes = judge.run_probes(ChatOpenAI(model=os.environ.get("REVIEWER_MODEL", "gpt-6.1-sol"), timeout=180, max_retries=2))

    base_path = out / "eval_baseline.json"
    baseline = json.loads(base_path.read_text()) if base_path.exists() else {}
    rep = report.build(heuristics.tally(per_run), golden, hidden, [j["score"] for j in judged if j["score"] is not None], probes,
                       collect.guardrail_summary(out / "guardrail.json"), {} if a.set_baseline else baseline)
    if a.set_baseline:
        base_path.write_text(json.dumps(report.baseline_from(rep), indent=1) + "\n")
        rep = report.build(rep["checks"], golden, hidden, [j["score"] for j in judged if j["score"] is not None], probes,
                           collect.guardrail_summary(out / "guardrail.json"), report.baseline_from(rep))
    rep |= {"meaning": heuristics.MEANING, "judge": judged, "probe_rows": probes, "golden_rows": golden,
            "runs": [{"run_id": f.run_id, "repo": f.repo, "issue": f.issue_key, "state": f.state, "pr": f.pr.number if f.pr else None,
                      "checks": [asdict(c) for c in checks]} for f, checks in zip(facts, per_run)]}
    prev.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    md = report.to_markdown(rep)
    (out / "eval.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
