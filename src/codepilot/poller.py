# src/codepilot/poller.py
"""Poll each repository for work (brief: every N minutes, via the GitHubToolkit), check it, claim it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from codepilot.gh.reader import candidates, existing_work, fetch_snapshot, make_wrapper
from codepilot.states import ACTIVE, RunState
from codepilot.worker import WorkItem


@dataclass
class PollServices:
    settings: Any
    runs: Any
    handoffs: Any
    audit: Any
    episodic: Any
    checker: Any                                # InjectionChecker
    repo_factory: Callable[[str], Any]
    on_notice: Callable[[str], None] = lambda text: None


@dataclass
class PollResult:
    claimed: list[tuple[WorkItem, str]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)     # issue key → why
    expired: list[str] = field(default_factory=list)


class Poller:
    def __init__(self, ps: PollServices):
        self.ps = ps
        self._blocked: set[tuple[str, int, str]] = set()      # (repo, number, body_hash) already refused this session

    def expire_closed(self, repo, full_name: str) -> list[str]:
        """E1: pending handoffs whose issue was closed are expired; their runs CLOSED(stale)."""
        out = []
        for h in self.ps.handoffs.pending():
            if h["repo"] != full_name or not h["issue_key"].startswith("#"):
                continue
            if repo.get_issue(int(h["issue_key"][1:])).state != "open":
                if not self.ps.handoffs.expire(h["id"], "issue closed"):
                    continue                                  # decided in the meantime
                self.ps.runs.set_state(h["run_id"], RunState.CLOSED, "stale: issue closed")
                self.ps.on_notice(f"{full_name}{h['issue_key']} was closed on GitHub — its approval item was withdrawn")
                out.append(h["issue_key"])
        return out

    def poll_repo(self, cfg) -> PollResult:
        ps, res = self.ps, PollResult()
        repo = ps.repo_factory(cfg.full_name)
        res.expired = self.expire_closed(repo, cfg.full_name)
        wrapper = make_wrapper(repo)
        recently_failed = ps.episodic.recently_failed(cfg.full_name)
        for cand in candidates(repo, wrapper, cfg.author_allowlist, cfg.poll_unassigned_below_threshold):
            key = f"#{cand.number}"
            if ps.runs.conn.execute(f"select 1 from runs where repo=? and issue_key=? and state in ({','.join('?' * len(ACTIVE))})",
                                    (cfg.full_name, key, *ACTIVE)).fetchone():
                res.skipped[key] = "already being processed"
                continue
            if key in recently_failed:
                res.skipped[key] = "failed in a recent session"
                continue
            branch = existing_work(repo, cand.number)
            if branch:
                res.skipped[key] = f"branch {branch} already on GitHub"
                continue
            snap = fetch_snapshot(wrapper, cand.number, ps.checker, cfg.author_allowlist)
            if snap.blocked:
                mark = (cfg.full_name, cand.number, snap.body_hash)
                if snap.failed_closed:                   # guardrail outage: say so, retry next poll, record nothing
                    ps.on_notice(f"{cfg.full_name} {key}: guardrail unavailable, will retry")
                    res.skipped[key] = "guardrail unavailable (will retry)"
                    continue
                if mark not in self._blocked:
                    self._blocked.add(mark)
                    rid = ps.runs.claim(cfg.full_name, "issue", cand.number, snap.title)
                    if rid:
                        reason = "guardrail unavailable (failed closed)" if snap.failed_closed else "injection suspected"
                        ps.runs.set_state(rid, RunState.SKIPPED, reason)
                        ps.audit.log("guardrail", "issue_blocked", run_id=rid, target=key,
                                     result="failed_closed" if snap.failed_closed else "blocked",
                                     detail={"reasons": snap.block_reasons[:5]})
                        ps.on_notice(f"⛔ {cfg.full_name} {key} blocked by the injection guardrail: {reason}")
                res.skipped[key] = "blocked by the injection check"
                continue
            rid = ps.runs.claim(cfg.full_name, "issue", cand.number, snap.title)
            if rid is None:
                res.skipped[key] = "claimed elsewhere"
                continue
            ps.audit.log("poller", "issue_claimed", run_id=rid, target=key)
            res.claimed.append((WorkItem(repo=cfg, issue=snap, below_threshold_only=not cand.labelled), rid))
        return res

    def poll(self) -> PollResult:
        total = PollResult()
        for cfg in self.ps.settings.repos:
            r = self.poll_repo(cfg)
            total.claimed += r.claimed
            total.skipped.update({f"{cfg.full_name}{k}": v for k, v in r.skipped.items()})
            total.expired += r.expired
        return total
