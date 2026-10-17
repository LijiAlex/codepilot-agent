# tests/test_bootstrap.py
from codepilot.bootstrap import build_engine
from codepilot.config import RepoConfig, Settings
from codepilot.engine import UiCallbacks
from codepilot.sandbox.daytona import DaytonaSandboxProvider
from codepilot.sandbox.local import LocalSandboxProvider


def test_build_engine_offline_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    s = Settings(repos=[RepoConfig(owner="o", name="r")], home=tmp_path / "home")
    e = build_engine(s, UiCallbacks(), config_dir=tmp_path)
    assert isinstance(e.worker.s.providers["local"], LocalSandboxProvider)
    assert isinstance(e.worker.s.providers["daytona"], DaytonaSandboxProvider)
    assert e.worker.s.tracer is None and (tmp_path / "home" / "codepilot.db").exists()
    assert e.cs.repo_config("o/r").name == "r"
    assert e.selfcheck() == []                       # the real startup check passes on the real graph


def test_issue_screen_is_nemo_by_default_and_bedrock_on_request(tmp_path, monkeypatch):
    from codepilot.safety.injection import BedrockInjectionChecker, NemoInjectionChecker
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    s = Settings(repos=[RepoConfig(owner="o", name="r")], home=tmp_path / "h1")
    nemo = build_engine(s, UiCallbacks(), config_dir=tmp_path).poller.ps.checker
    assert isinstance(nemo, NemoInjectionChecker) and nemo.config_dir == tmp_path / "guardrails" / "nemo"
    assert nemo.model_name == "anthropic:claude-haiku-5-5"
    s2 = Settings(repos=[RepoConfig(owner="o", name="r")], home=tmp_path / "h2", injection_checker="bedrock")
    assert isinstance(build_engine(s2, UiCallbacks(), config_dir=tmp_path).poller.ps.checker, BedrockInjectionChecker)
