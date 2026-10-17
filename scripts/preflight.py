# scripts/preflight.py
"""Live preflight: one cheap check per external service, before building on top of it (run right after Task 1,
then again whenever a key is added). Prints PASS / FAIL / SKIP per line; exit 1 if anything FAILs.
Usage: uv run python scripts/preflight.py [--config codepilot.yml]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _chat_model(name: str):
    from langchain.chat_models import init_chat_model          # direct, so this works right after Task 1
    return init_chat_model(name, temperature=0, max_tokens=8, timeout=60)


def check_model(settings) -> tuple[str, str]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "SKIP", "ANTHROPIC_API_KEY not set"
    reply = _chat_model(settings.solver_model).invoke("Reply with the single word OK.")
    return ("PASS", f"{settings.solver_model} answered") if reply.content else ("FAIL", "empty reply")


def check_github(settings) -> list[tuple[str, str]]:
    if not (os.environ.get(settings.github_app_id_env) and os.environ.get(settings.github_app_key_path_env)):
        return [("SKIP", f"{settings.github_app_id_env} / {settings.github_app_key_path_env} not set")]
    from codepilot.gh.auth import TokenProvider
    out = []
    for r in settings.repos:
        try:
            repo = TokenProvider.from_env(settings.github_app_id_env, settings.github_app_key_path_env, r.owner, r.name).repo()
            out.append(("PASS", f"{r.full_name}: App installed, default branch {repo.default_branch}"))
        except Exception as e:                       # noqa: BLE001 — report and continue with the next repo
            out.append(("FAIL", f"{r.full_name}: {type(e).__name__}: {str(e)[:120]}"))
    return out


def check_daytona(settings) -> tuple[str, str]:
    if settings.sandbox != "daytona":
        return "SKIP", f"sandbox is {settings.sandbox}"
    if not os.environ.get("DAYTONA_API_KEY"):
        return "SKIP", "DAYTONA_API_KEY not set"
    from codepilot.sandbox.daytona import make_client
    boxes = list(make_client().list())
    return "PASS", f"API reachable ({len(boxes)} existing box(es))"


def check_bedrock(settings, config_dir: Path) -> tuple[str, str]:
    if not os.environ.get("AWS_PROFILE"):
        return "SKIP", "AWS_PROFILE not set"
    from codepilot.safety.injection import BedrockInjectionChecker
    policy = settings.guardrail_policy_path if settings.guardrail_policy_path.is_absolute() else config_dir / settings.guardrail_policy_path
    v = BedrockInjectionChecker(policy).check("Please fix the typo in the README.")
    if v.failed_closed:
        return "FAIL", f"guardrail unavailable: {v.reasons}"
    return ("PASS", "benign text allowed") if not v.blocked else ("FAIL", f"benign text blocked: {v.reasons}")


def main(argv=None) -> int:
    from codepilot.config import load_settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="codepilot.yml")
    a = ap.parse_args(argv)
    path = Path(a.config)
    if not path.exists():
        path = Path("codepilot.example.yml")
        print(f"note: {a.config} not found, using {path}")
    settings = load_settings(path)
    results = []
    for name, fn in (("model", lambda: [check_model(settings)]), ("github", lambda: check_github(settings)),
                     ("daytona", lambda: [check_daytona(settings)]), ("bedrock", lambda: [check_bedrock(settings, path.parent)])):
        try:
            rows = fn()
        except ModuleNotFoundError as e:             # run early (after Task 1): later tasks' modules do not exist yet
            rows = [("SKIP", f"not built yet ({e.name})")]
        except Exception as e:                       # noqa: BLE001 — one service failing must not hide the others
            rows = [("FAIL", f"{type(e).__name__}: {str(e)[:160]}")]
        for status, msg in rows:
            print(f"{status:4} {name:8} {msg}")
            results.append(status)
    return 1 if "FAIL" in results else 0


if __name__ == "__main__":
    sys.exit(main())
