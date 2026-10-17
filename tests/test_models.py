# tests/test_models.py
from langchain_core.messages import HumanMessage

from codepilot.agents.models import make_model


def test_solver_model_builds_a_request_claude_5_accepts(monkeypatch):
    # Sonnet 5.5 / Fable reject non-default temperature/top_p/top_k (client-side check, before any request)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    m = make_model("anthropic:claude-sonnet-5-5")
    payload = m._get_request_payload([HumanMessage("hi")])
    assert payload["model"] == "claude-sonnet-5-5" and payload.get("temperature") in (None, 1)
