# tests/test_observability.py
from langchain_core.messages import AIMessage

from codepilot.events import EventTranslator
from codepilot.observability import _redact, cost_usd, make_tracer


def test_redaction_is_recursive():
    out = _redact({"messages": [{"content": "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"}], "n": 3})
    assert "abcdefghijklmnop" not in str(out) and out["n"] == 3


def test_tracer_only_when_configured(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert make_tracer() is None
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-not-real")
    t = make_tracer("p")
    assert t is not None and t.project_name == "p"


def test_tokens_counted_and_cost():
    tr = EventTranslator("r1")
    m = AIMessage(content="hi", usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200})
    tr.translate((), "updates", {"model": {"messages": [m]}})
    assert (tr.tokens_in, tr.tokens_out) == (1000, 200)
    assert cost_usd(1_000_000, 100_000, 3.0, 15.0) == 4.5 and cost_usd(1, 1, None, 15.0) is None
