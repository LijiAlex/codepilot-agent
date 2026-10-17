# src/codepilot/observability.py
"""LangSmith tracing (Bonus) with secrets redacted before anything leaves the machine."""
from __future__ import annotations

import os
from typing import Any

from codepilot.audit import redact


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def make_tracer(project: str = "codepilot-plus"):
    """A LangChain tracer whose client redacts every input and output; None when LangSmith is not configured."""
    for k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[k] = "false"           # LangChain's env-enabled tracer does not redact: only ours may send traces
    if not os.environ.get("LANGSMITH_API_KEY"):
        return None
    from langchain_core.tracers.langchain import LangChainTracer
    from langsmith import Client
    client = Client(hide_inputs=_redact, hide_outputs=_redact)
    return LangChainTracer(project_name=project, client=client)


def cost_usd(tokens_in: int, tokens_out: int, price_in: float | None, price_out: float | None) -> float | None:
    if price_in is None or price_out is None:
        return None
    return round(tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out, 4)
