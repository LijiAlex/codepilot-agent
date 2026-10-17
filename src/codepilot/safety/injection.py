# src/codepilot/safety/injection.py
"""Prompt-injection screen for issue and PR text, backed by the A3 guardrail.

Any failure to reach the guardrail blocks the text ("fails closed") and is
reported separately from a real detection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass
class InjectionVerdict:
    blocked: bool
    failed_closed: bool = False
    reasons: list[str] = field(default_factory=list)


class InjectionChecker(Protocol):
    def check(self, text: str) -> InjectionVerdict: ...


class BedrockInjectionChecker:
    def __init__(self, policy_path: Path, guardrail_factory: Callable[[], Any] | None = None):
        self.policy_path = Path(policy_path)
        self._factory = guardrail_factory or self._default_factory
        self._guardrail: Any | None = None

    def _default_factory(self) -> Any:
        from guardrail_eval_pipeline.config import load_policy
        from guardrail_eval_pipeline.guardrails.bedrock import BedrockGuardrail
        return BedrockGuardrail.from_policy(load_policy(self.policy_path))

    def check(self, text: str) -> InjectionVerdict:
        try:
            if self._guardrail is None:
                self._guardrail = self._factory()
            v = self._guardrail.check_input(text)
        except Exception as exc:                      # unreachable, throttled, misconfigured
            self._guardrail = None
            return InjectionVerdict(blocked=True, failed_closed=True, reasons=[f"guardrail unavailable: {type(exc).__name__}"])
        return InjectionVerdict(blocked=bool(v.blocked), failed_closed=bool(getattr(v, "failed_closed", False)),
                                reasons=list(getattr(v, "reasons", []) or []))


class StaticInjectionChecker:
    """Offline checker for tests and demos without AWS; never the production default."""
    def __init__(self, blocked_phrases: list[str]):
        self.phrases = [p.lower() for p in blocked_phrases]

    def check(self, text: str) -> InjectionVerdict:
        hits = [p for p in self.phrases if p in text.lower()]
        return InjectionVerdict(blocked=bool(hits), reasons=hits)
