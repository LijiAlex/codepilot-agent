# playground/verify.py
"""Prove the playground is fair: baseline tests pass, every hidden test fails on the playground and passes
after its reference fix (with the full suite still green).
Usage: uv run python playground/verify.py A3_CLONE PYTHON          (PYTHON = an interpreter with A3's deps + pytest)
       uv run python playground/verify.py REPO PYTHON --as-is --issues playground/layered-memory.yaml"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from build import build  # noqa: E402

HERE = Path(__file__).parent


def pytest(python: str, cwd: Path, *args: str) -> int:
    return subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=cwd,
                          capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(cwd / "src")}).returncode


def verify(a3: Path, python: str, issues_file: Path = HERE / "issues.yaml", repo: Path | None = None) -> list[tuple[str, str]]:
    """repo=None: build the playground from the A3 clone. repo=PATH: check issues against that repo's HEAD as is."""
    issues = yaml.safe_load(Path(issues_file).read_text())
    results = []
    with tempfile.TemporaryDirectory() as td:
        if repo is None:
            base = build(a3, Path(td) / "pg")
        else:
            base = Path(td) / "pg"
            base.mkdir()
            archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "HEAD"], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(base)], input=archive, check=True)
        results.append(("baseline suite", "pass" if pytest(python, base) == 0 else "FAIL"))
        for it in issues:
            if not it.get("hidden") and not it.get("conflict_test"):
                continue
            work = Path(td) / it["key"]
            shutil.copytree(base, work)
            if it.get("conflict_test"):          # retry-gate issue: the reference must break the protected contract test
                for r in it["reference"]:
                    p = work / r["file"]
                    assert p.read_text().count(r["old"]) == 1, (it["key"], r["file"])
                    p.write_text(p.read_text().replace(r["old"], r["new"]))
                broke = pytest(python, work, it["conflict_test"]) != 0
                results.append((it["key"], "requested change breaks the protected contract test" if broke else "BAD contract test still passes"))
                continue
            hidden = work / it["hidden"]["path"]
            hidden.write_text(it["hidden"]["content"])
            before = pytest(python, work, it["hidden"]["path"])
            if not it.get("reference"):
                results.append((it["key"], "fails before" if before else "PASSES BEFORE"))
                continue
            for r in it["reference"]:
                p = work / r["file"]
                assert p.read_text().count(r["old"]) == 1, (it["key"], r["file"])
                p.write_text(p.read_text().replace(r["old"], r["new"]))
            after = pytest(python, work, it["hidden"]["path"])
            suite = pytest(python, work)
            ok = before != 0 and after == 0 and suite == 0
            results.append((it["key"], "fails before, passes after, suite green" if ok else f"BAD before={before} after={after} suite={suite}"))
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="A3 clone (playground) or, with --as-is, the repository to check")
    ap.add_argument("python", help="interpreter with the repository's deps + pytest")
    ap.add_argument("--issues", default=str(HERE / "issues.yaml"))
    ap.add_argument("--as-is", action="store_true", help="check the repository's HEAD without building the playground")
    a = ap.parse_args()
    src = Path(a.source).resolve()
    res = verify(src, str(Path(a.python).absolute()), Path(a.issues), src if a.as_is else None)
    for k, v in res:
        print(f"{k:24} {v}")
    sys.exit(0 if all(not (v.startswith("BAD") or v in ("FAIL", "PASSES BEFORE")) for _, v in res) else 1)
