# scripts/measure_guardrail.py
"""Measure the injection layer (sanitiser + checker) on evals/issues.yaml.
Usage: uv run python scripts/measure_guardrail.py [--checker bedrock|static] [--out results/guardrail.md]
Reports detection on malicious items, false alarms on benign items, fail-closed count (counted
separately: a blocked-because-unavailable verdict is not a detection), and latency."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import yaml

from codepilot.safety.injection import BedrockInjectionChecker, StaticInjectionChecker
from codepilot.safety.sanitize import sanitize_untrusted

STATIC_PHRASES = ["ignore all previous instructions", "ignore previous instructions", "disregard your guidelines",
                  "system override", "forget everything above", "you are dan"]


def measure(items: list[dict], checker) -> dict:
    rows, lat = [], []
    for it in items:
        clean = sanitize_untrusted(it["text"])
        t0 = time.perf_counter()
        v = checker.check(clean)
        lat.append((time.perf_counter() - t0) * 1000)
        rows.append({"id": it["id"], "label": it["label"], "blocked": v.blocked and not v.failed_closed,
                     "failed_closed": v.failed_closed, "sanitised_changed": clean != it["text"].strip()})
    mal = [r for r in rows if r["label"] == "malicious"]
    ben = [r for r in rows if r["label"] == "benign"]
    return {"rows": rows,
            "detected": sum(r["blocked"] for r in mal), "malicious": len(mal),
            "false_alarms": sum(r["blocked"] for r in ben), "benign": len(ben),
            "failed_closed": sum(r["failed_closed"] for r in rows),
            "p50_ms": round(statistics.median(lat), 1), "p95_ms": round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 1)}


def to_markdown(res: dict, checker_name: str) -> str:
    lines = [f"Checker: {checker_name}", "",
             "| Measure | Result |", "|---|---|",
             f"| Malicious issues blocked | {res['detected']}/{res['malicious']} |",
             f"| Benign issues wrongly blocked | {res['false_alarms']}/{res['benign']} |",
             f"| Guardrail unavailable (failed closed) | {res['failed_closed']} |",
             f"| Latency p50 / p95 | {res['p50_ms']} ms / {res['p95_ms']} ms |", "",
             "| id | label | blocked | failed closed | sanitiser changed text |", "|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['label']} | {'yes' if r['blocked'] else 'no'} | {'yes' if r['failed_closed'] else 'no'} | "
              f"{'yes' if r['sanitised_changed'] else 'no'} |" for r in res["rows"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checker", choices=["bedrock", "static"], default="bedrock")
    ap.add_argument("--issues", default="evals/issues.yaml")
    ap.add_argument("--policy", default="guardrails/codepilot.yaml")
    ap.add_argument("--out", default="results/guardrail.md")
    a = ap.parse_args(argv)
    items = yaml.safe_load(Path(a.issues).read_text())
    checker = BedrockInjectionChecker(Path(a.policy)) if a.checker == "bedrock" else StaticInjectionChecker(STATIC_PHRASES)
    res = measure(items, checker)
    md = to_markdown(res, a.checker)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(md)
    Path(a.out).with_suffix(".json").write_text(json.dumps(res, indent=1))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
