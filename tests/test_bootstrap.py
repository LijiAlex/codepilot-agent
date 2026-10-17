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
