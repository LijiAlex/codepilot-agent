# src/codepilot/bootstrap.py
"""Production wiring: build every service from Settings. Anything that needs the network or a credential
(GitHub tokens, models, Bedrock, the embedding model) is created lazily, on first use."""
from __future__ import annotations

import functools
import os
from pathlib import Path

from codepilot.agents.models import make_model, register_profiles
from codepilot.agents.selfcheck import run_selfcheck
from codepilot.audit import AuditLog
from codepilot.config import Settings
from codepilot.continuations import ContinuationServices
from codepilot.db import connect
from codepilot.engine import Engine, UiCallbacks
from codepilot.gh.auth import TokenProvider
from codepilot.handoffs import HandoffStore
from codepilot.improve.gaps import ImprovementStore, Improver
from codepilot.improve.repo_skill import RepoSkillStore
from codepilot.memory.episodic import EpisodicMemory
from codepilot.memory.semantic import FileIndex, LessonStore, default_embedder
from codepilot.observability import make_tracer
from codepilot.poller import PollServices, Poller
from codepilot.publish import PublishServices
from codepilot.repomap.mirror import HostMirror
from codepilot.runs import RunStore
from codepilot.safety.injection import BedrockInjectionChecker
from codepilot.sandbox.daytona import DaytonaSandboxProvider
from codepilot.sandbox.local import LocalSandboxProvider
from codepilot.watcher import PRWatcher, WatchServices
from codepilot.worker import Services, Worker

AGENTS = ("orchestrator", "repo-explorer", "coder", "test-agent", "test-doctor", "pr-agent")


def build_engine(settings: Settings, ui: UiCallbacks, config_dir: Path = Path(".")) -> Engine:
    home = Path(settings.home)
    conn = connect(home / "codepilot.db")
    runs, handoffs, audit = RunStore(conn), HandoffStore(conn), AuditLog(conn)
    episodic = EpisodicMemory(home / "store.db")

    @functools.cache
    def tokens(full_name: str) -> TokenProvider:
        owner, name = full_name.split("/")
        return TokenProvider.from_env(settings.github_app_id_env, settings.github_app_key_path_env, owner, name)

    def repo_factory(full_name: str):
        return tokens(full_name).repo()

    @functools.cache
    def embedder():
        return default_embedder()

    def lazy_embed(texts):
        return embedder()(texts)

    lessons = LessonStore(home / "chroma", lazy_embed)

    @functools.cache
    def file_index(full_name: str) -> FileIndex:
        return FileIndex(home / "chroma", full_name, lazy_embed)

    @functools.cache
    def solver():
        m = make_model(settings.solver_model)
        register_profiles(m)
        return m

    skill_store = RepoSkillStore(conn)

    def repo_skill(cfg, map_files):
        skill_store.ensure_v1(cfg, map_files)
        return skill_store.render(cfg.full_name)

    policy = settings.guardrail_policy_path
    policy = policy if policy.is_absolute() else (Path(config_dir) / policy)
    checker = BedrockInjectionChecker(policy)
    key_dir = [Path(os.environ[settings.github_app_key_path_env]).expanduser().parent] if os.environ.get(settings.github_app_key_path_env) else []
    providers = {"local": LocalSandboxProvider(home, home / "repos", extra_deny=key_dir),     # ~/.codepilot is always denied
                 "daytona": DaytonaSandboxProvider(home, home / "repos", snapshot=settings.daytona_snapshot,
                                                   auto_stop_minutes=settings.daytona_auto_stop_minutes)}
    services = Services(settings=settings, runs=runs, handoffs=handoffs, audit=audit, mirror=HostMirror(home),
                        providers=providers, make_models=lambda: {a: solver() for a in AGENTS}, triage_model=solver,
                        episodic=episodic, lessons=lessons, file_index=file_index, tracer=make_tracer(settings.langsmith_project),
                        repo_skill=repo_skill)
    worker = Worker(services)
    ps = PublishServices(runs=runs, handoffs=handoffs, audit=audit, repo_factory=repo_factory, make_model=solver,
                         on_notice=ui.on_notice)
    poller = Poller(PollServices(settings=settings, runs=runs, handoffs=handoffs, audit=audit, episodic=episodic,
                                 checker=checker, repo_factory=repo_factory, on_notice=ui.on_notice))
    watcher = PRWatcher(WatchServices(runs=runs, handoffs=handoffs, audit=audit, episodic=episodic,
                                      repo_factory=repo_factory, lessons=lessons, on_notice=ui.on_notice, runs_dir=home / "runs"))
    by_name = {r.full_name: r for r in settings.repos}
    cs = ContinuationServices(publish=ps, repo_config=by_name.__getitem__, lessons=lessons, cancel_run=worker.cancel)
    improver = Improver(conn, home / "runs", ImprovementStore(conn, skill_store), solver, audit)
    return Engine(settings, worker, poller, watcher, cs, episodic, ui, selfcheck=run_selfcheck, improver=improver)
