# tests/fakes.py
"""Scripted chat models for offline tests.

FakeClaude reports provider "anthropic" so DeepAgents resolves the same harness
profile as the real model (verified: deepagents/_models.get_model_provider uses
_get_ls_params; identifier comes from model_name).
"""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

BOUND_TOOLS: dict[str, dict[str, str]] = {}


class FakeClaude(FakeMessagesListChatModel):
    model_name: str = "fake-sonnet"
    tag: str = "agent"

    def _get_ls_params(self, stop: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "anthropic", "ls_model_name": self.model_name, "ls_model_type": "chat"}

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeClaude":
        BOUND_TOOLS[self.tag] = {_name(t): _description(t) for t in tools}
        return self


def _name(t: Any) -> str:
    if isinstance(t, type):                       # Pydantic schema from with_structured_output
        return t.__name__
    if isinstance(t, dict):
        return t.get("name") or t.get("function", {}).get("name", "")
    return t.name


def _description(t: Any) -> str:
    if isinstance(t, type):
        return t.__doc__ or ""
    if isinstance(t, dict):
        return t.get("description") or t.get("function", {}).get("description", "")
    return t.description


def tool_call(name: str, args: dict[str, Any], cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])
