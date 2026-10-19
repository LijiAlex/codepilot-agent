# src/codepilot/cli.py
"""`codepilot` — the TUI by default; a few headless commands for setup, demos and smoke tests."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from codepilot.config import load_settings

DEFAULT_CONFIGS = [Path("codepilot.yml"), Path.home() / ".codepilot" / "codepilot.yml"]


def _config_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    for p in DEFAULT_CONFIGS:
        if p.exists():
            return p
    raise SystemExit("no codepilot.yml found (copy codepilot.example.yml to codepilot.yml)")


def trace_cost_source(project: str):
    """Cost per run from LangSmith traces, cached for a minute; None when LangSmith is not configured."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        return None
    import time
    from langsmith import Client
    from codepilot.dashboard.data import langsmith_costs
    client, cache = Client(), {"at": 0.0, "costs": {}}

    def costs() -> dict:
        if time.monotonic() - cache["at"] > 60:
            cache["costs"], cache["at"] = langsmith_costs(client, project), time.monotonic()
        return cache["costs"]
    return costs


def trace_usage_source(project: str, reviewer_project: str = "codepilot-reviewer"):
    """Tokens and cost per run, injection screen and reviewer from LangSmith, cached for a minute."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        return None
    import time
    from langsmith import Client
    from codepilot.dashboard.data import langsmith_usage
    client, cache = Client(), {"at": 0.0, "usage": None}

    def usage() -> dict:
        if cache["usage"] is None or time.monotonic() - cache["at"] > 60:
            cache["usage"], cache["at"] = langsmith_usage(client, project, reviewer_project), time.monotonic()
        return cache["usage"]
    return usage


def dashboard_token(home: Path) -> str:
    """One access token per CodePilot+ home, kept in a file only the user can read, so a restart does not lock out
    an open tab. Delete the file to issue a new one."""
    f = Path(home) / "dashboard-token"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def start_dashboard_thread(engine, settings, config_dir: Path, port: int):
    """Plus: the dashboard inside the TUI process, sharing its database and its ChromaDB client
    (ChromaDB does not support two processes on one folder)."""
    import threading

    import uvicorn

    from codepilot.dashboard.app import DashboardDeps, create_app
    from codepilot.skills import load_skills
    s = engine.worker.s
    improver = getattr(engine, "improver", None)
    deps = DashboardDeps(trace_usage=trace_usage_source(settings.langsmith_project), trace_costs=trace_cost_source(settings.langsmith_project), map_budget=settings.repo_map_budget_tokens, cost_tracked=settings.price_in_per_mtok is not None and settings.price_out_per_mtok is not None, conn=s.runs.conn, runs_dir=Path(settings.home) / "runs", results_dir=config_dir / "results",
                         repos=[r.full_name for r in settings.repos], repo_factory=engine.cs.publish.repo_factory, audit=s.audit,
                         reviewer_login=settings.reviewer_login, lessons=s.lessons, episodic=s.episodic, skills=load_skills(),
                         improvements=improver.store if improver is not None else None)
    token = dashboard_token(Path(settings.home))
    server = uvicorn.Server(uvicorn.Config(create_app(deps, token, port), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="dashboard")
    thread.start()
    return f"http://127.0.0.1:{port}/#token={token}", thread


def cmd_tui(args) -> int:
    from codepilot.bootstrap import build_engine
    from codepilot.tui.app import CodePilotApp
    path = _config_path(args.config)
    settings = load_settings(path)

    def factory(ui):
        engine = build_engine(settings, ui, path.parent)
        if getattr(args, "dashboard", None):
            url, _ = start_dashboard_thread(engine, settings, path.parent, args.dashboard)
            print(f"CodePilot+ dashboard: {url}")
        return engine
    CodePilotApp(factory).run()
    return 0


def cmd_selfcheck(args) -> int:
    from codepilot.agents.selfcheck import run_selfcheck
    problems = run_selfcheck()
    for p in problems:
        print("PROBLEM:", p)
    print("tool sets OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_replay(args) -> int:
    """Demo fallback: print a recorded run, clearly labelled as a replay."""
    from codepilot.events import replay
    path = Path(args.path)
    if path.is_dir() or not path.suffix:
        path = Path(args.home).expanduser() / "runs" / args.path / "events.jsonl"
    print(f"=== REPLAY of a recorded run ({path.parent.name}) — not live ===")
    for e in replay(path, args.speed):
        print(f"[{e.agent}] {e.kind}: {e.text}")
    return 0


def cmd_once(args) -> int:
    """Headless: one poll, run everything claimed, print outcomes (used by the smoke test)."""
    from codepilot.bootstrap import build_engine
    from codepilot.engine import UiCallbacks
    path = _config_path(args.config)
    settings = load_settings(path)
    eng = build_engine(settings, UiCallbacks(on_notice=lambda t: print("notice:", t),
                                             on_event=lambda e: print(f"[{e.agent}] {e.text}") if args.verbose and e.kind != "token" else None),
                       path.parent)
    eng.startup()
    eng.start(poll_loop=False)          # one poll, here: a second poller would race this one (live 2026-10-18)
    eng.poll_once()
    ok = eng.wait_idle(args.timeout)
    eng.shutdown()
    for r in eng.view()["runs"][:10]:
        print(f"{r['repo']} {r['issue_key']}: {r['state']}" + (f" ({r['reason']})" if r["reason"] else ""))
    return 0 if ok else 2


def cmd_dashboard(args) -> int:
    """Plus: the read-mostly dashboard on 127.0.0.1 (separate process; reads the same database)."""
    import functools
    import secrets

    import uvicorn

    from codepilot.audit import AuditLog
    from codepilot.dashboard.app import DashboardDeps, create_app
    from codepilot.db import connect
    from codepilot.gh.auth import TokenProvider
    from codepilot.improve.gaps import ImprovementStore, lazy_embedder
    from codepilot.improve.repo_skill import RepoSkillStore
    from codepilot.memory.episodic import EpisodicMemory
    from codepilot.skills import load_skills
    path = _config_path(args.config)
    s = load_settings(path)
    home = Path(s.home)
    conn = connect(home / "codepilot.db")

    @functools.cache
    def tokens(full_name: str):
        owner, name = full_name.split("/")
        return TokenProvider.from_env(s.github_app_id_env, s.github_app_key_path_env, owner, name)

    deps = DashboardDeps(trace_usage=trace_usage_source(s.langsmith_project), trace_costs=trace_cost_source(s.langsmith_project), map_budget=s.repo_map_budget_tokens, cost_tracked=s.price_in_per_mtok is not None and s.price_out_per_mtok is not None, conn=conn, runs_dir=home / "runs", results_dir=path.parent / "results", repos=[r.full_name for r in s.repos],
                         repo_factory=lambda n: tokens(n).repo(), audit=AuditLog(conn), reviewer_login=s.reviewer_login,
                         # standalone: no ChromaDB (one process per Chroma folder); lessons show with `codepilot --dashboard PORT`
                         lessons=None, episodic=EpisodicMemory(home / "store.db"),
                         skills=load_skills(), improvements=ImprovementStore(conn, RepoSkillStore(conn), embed=lazy_embedder()))
    token = dashboard_token(home)
    print(f"CodePilot+ dashboard: http://127.0.0.1:{args.port}/#token={token}  (this link is the password; Ctrl-C to stop)")
    uvicorn.run(create_app(deps, token, args.port), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_connect(args) -> int:
    """Plus: make one repository ready in one command (see codepilot.connect)."""
    from codepilot.connect import connect
    from codepilot.db import connect as db_connect
    from codepilot.gh.auth import TokenProvider
    from codepilot.improve.repo_skill import RepoSkillStore
    from codepilot.memory.semantic import FileIndex, default_embedder
    from codepilot.repomap.mirror import HostMirror
    path = _config_path(args.config)
    s = load_settings(path)
    owner, name = args.repo.split("/", 1)
    home = Path(s.home)
    tp = TokenProvider.from_env(s.github_app_id_env, s.github_app_key_path_env, owner, name)
    client = None
    if args.snapshot:
        from codepilot.sandbox.daytona import make_client
        client = make_client()
    rep = connect(owner, name, home, repo_factory=lambda n: tp.repo(), mirror=HostMirror(home),
                  skills=RepoSkillStore(db_connect(home / "codepilot.db")),
                  file_index=None if args.no_index else FileIndex(home / "chroma", args.repo, default_embedder()),
                  daytona_client=client, config_path=path if args.write else None)
    print(f"connected {rep.repo} (default branch {rep.default_branch}); labels created: {rep.labels_created or 'none'}")
    print(f"setup: install `{rep.setup.install_command}`, tests `{rep.setup.test_command}`")
    for n in rep.setup.notes:
        print("note:", n)
    print(f"repo map: {rep.map_tokens} tokens, {rep.map_files} files; repo skill v{rep.repo_skill_version}; "
          f"indexed chunks: {rep.indexed_chunks if rep.indexed_chunks is not None else 'skipped'}; snapshot: {rep.snapshot or 'none'}")
    if rep.config_written:
        print(f"added to {path}")
    else:
        print("codepilot.yml entry (add under repos:):\n" + rep.config_block)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codepilot", description="CodePilot+: GitHub issues to approved pull requests")
    ap.add_argument("--config", help="path to codepilot.yml")
    ap.add_argument("--dashboard", type=int, metavar="PORT", help="Plus: also serve the dashboard from the TUI process")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="start the TUI (default)")
    sub.add_parser("selfcheck", help="check every agent gets exactly its designed tools")
    r = sub.add_parser("replay", help="print a recorded run (labelled as a replay)")
    r.add_argument("path", help="run id or path to events.jsonl")
    r.add_argument("--speed", type=float, default=0.0, help="0 = instant; 1 = real time; 4 = 4x")
    r.add_argument("--home", default="~/.codepilot")
    o = sub.add_parser("once", help="headless: poll once and process what was claimed")
    o.add_argument("--timeout", type=float, default=1800)
    o.add_argument("--verbose", action="store_true")
    d = sub.add_parser("dashboard", help="Plus: open the dashboard on 127.0.0.1")
    d.add_argument("--port", type=int, default=8765)
    c = sub.add_parser("connect", help="Plus: make a repository ready (labels, setup, map, repo skill, index)")
    c.add_argument("repo", help="owner/name")
    c.add_argument("--write", action="store_true", help="add the repository to codepilot.yml")
    c.add_argument("--snapshot", action="store_true", help="also build a Daytona snapshot with its dependencies")
    c.add_argument("--no-index", action="store_true", help="skip the embedding index (no model download)")
    args = ap.parse_args(argv)
    handler = {"selfcheck": cmd_selfcheck, "replay": cmd_replay, "once": cmd_once, "dashboard": cmd_dashboard,
               "connect": cmd_connect}.get(args.cmd, cmd_tui)
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
