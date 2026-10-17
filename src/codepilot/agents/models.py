# src/codepilot/agents/models.py
"""Chat models and the DeepAgents harness profile."""
from __future__ import annotations

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from deepagents._models import get_model_provider


def make_model(name: str, *, max_tokens: int = 8192):
    """name like 'anthropic:claude-sonnet-5-5'. Reads the provider key from the environment (never from a file)."""
    from langchain.chat_models import init_chat_model
    return init_chat_model(name, max_tokens=max_tokens, max_retries=2, timeout=180)   # default temperature: Claude 5 models reject others


def structured(model, schema):
    """Structured output that works on every solver model: Claude 5 models reject forced tool calls, so Anthropic
    models use native JSON-schema output; others (and the test fakes) keep tool calling."""
    if type(model).__name__ == "ChatAnthropic":
        return model.with_structured_output(schema, method="json_schema")
    return model.with_structured_output(schema)


def register_profiles(model) -> str:
    """Disable DeepAgents' general-purpose subagent for this model's provider (verified: the profile is
    keyed by provider, and a mismatch silently re-enables it). Returns the provider key used."""
    provider = get_model_provider(model)
    if not provider:
        raise RuntimeError(f"cannot tell the provider of {model!r}; general-purpose subagent would stay enabled")
    register_harness_profile(provider, HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
    return provider
