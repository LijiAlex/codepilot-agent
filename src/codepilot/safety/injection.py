# src/codepilot/safety/injection.py
"""Prompt-injection screen for issue and PR text: NeMo Guardrails input rails (the brief's framework; default) or the
A3 Bedrock guardrail.

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


def guardrail_llm(name: str, callbacks: list | None = None):
    """A Claude chat model for the self-check rail. NeMo always sends `temperature` (0.001) and Claude 5 models reject
    any sampling parameter (live, 2026-10-17: HTTP 400 "temperature is deprecated for this model"), so drop them."""
    from langchain_anthropic import ChatAnthropic

    class _Claude(ChatAnthropic):
        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            for k in ("temperature", "top_p", "top_k"):
                kwargs.pop(k, None)
            return super()._get_request_payload(input_, stop=stop, **kwargs)

    provider, _, model = name.partition(":")
    if provider != "anthropic":
        from langchain.chat_models import init_chat_model
        return init_chat_model(name, max_tokens=16, timeout=60, callbacks=callbacks, metadata={"kind": "injection-screen"})
    return _Claude(model=model, max_tokens=16, timeout=60, max_retries=2, callbacks=callbacks,       # traced (redacted) like the agents
                   metadata={"kind": "injection-screen"})


class NemoInjectionChecker:
    """NeMo Guardrails input rails: a regex rail (known attacks, no model call) then the LLM self-check rail."""

    def __init__(self, config_dir: Path, llm: Any = None, model_name: str = "anthropic:claude-haiku-5-5", callbacks: list | None = None):
        self.config_dir, self._llm, self.model_name, self.callbacks = Path(config_dir), llm, model_name, callbacks
        self._rails: Any | None = None

    def _get_rails(self):
        if self._rails is None:
            import warnings
            from nemoguardrails import LLMRails, RailsConfig
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._rails = LLMRails(RailsConfig.from_path(str(self.config_dir)), llm=self._llm or guardrail_llm(self.model_name, self.callbacks))
        return self._rails

    def check(self, text: str) -> InjectionVerdict:
        try:
            res = self._get_rails().generate(messages=[{"role": "user", "content": text}],
                                             options={"rails": ["input"], "log": {"activated_rails": True}})
            stopped = [a.name for a in (res.log.activated_rails or []) if a.stop]
        except Exception as exc:                      # model unreachable, throttled, misconfigured
            self._rails = None
            return InjectionVerdict(blocked=True, failed_closed=True, reasons=[f"guardrail unavailable: {type(exc).__name__}"])
        return InjectionVerdict(blocked=bool(stopped), reasons=stopped)
