# scripts/map_proof.py
"""Repo-map proof on a large repository (Plus): how long scanning and ranking take, and what fits in the budget.
Usage: uv run python scripts/map_proof.py PATH_TO_CLONE [--budget 4000] [--focus word ...]"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from codepilot.repomap.build import _pagerank, build_repo_map, scan


def prove(root: Path, budget: int, focus: list[str]) -> dict:
    t0 = time.perf_counter()
    entries = scan(root)
    t1 = time.perf_counter()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip() or "unknown"
    m = build_repo_map(root, sha, budget_tokens=budget, focus_terms=focus, entries=entries)
    t2 = time.perf_counter()
    rank = _pagerank(entries, {})
    langs: dict[str, int] = {}
    for e in entries:
        langs[e.language] = langs.get(e.language, 0) + 1
    return {"files": len(entries), "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])[:6]),
            "symbols": sum(len(e.symbols) for e in entries), "scan_s": round(t1 - t0, 2), "map_s": round(t2 - t1, 2),
            "map_tokens": m.tokens, "budget": budget, "files_shown": m.text.count("\n") - m.text.split("## Files")[0].count("\n") - 1,
            "top": [p for p, _ in sorted(rank.items(), key=lambda kv: -kv[1])[:8]], "text": m.text}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--budget", type=int, default=4000)
    ap.add_argument("--focus", nargs="*", default=[])
    a = ap.parse_args(argv)
    r = prove(Path(a.path), a.budget, a.focus)
    print(f"{r['files']} files ({r['languages']}), {r['symbols']} symbols; scan {r['scan_s']} s, rank+fit {r['map_s']} s")
    print(f"map: {r['map_tokens']} tokens (budget {r['budget']}), {r['files_shown']} files shown")
    print("most central files:", ", ".join(r["top"]))
    return 0 if r["map_tokens"] <= r["budget"] else 1


if __name__ == "__main__":
    sys.exit(main())
