# tests/test_preflight.py
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

from langchain_core.messages import AIMessage

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)

CFG = "repos:\n  - owner: o\n    name: r\nsandbox: daytona\n"


def test_everything_skips_without_keys(tmp_path, monkeypatch, capsys):
    for k in ("ANTHROPIC_API_KEY", "CODEPILOT_GH_APP_ID", "CODEPILOT_GH_APP_KEY_PATH", "DAYTONA_API_KEY", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "codepilot.yml").write_text(CFG)
    assert pf.main(["--config", str(tmp_path / "codepilot.yml")]) == 0
    out = capsys.readouterr().out
    assert out.count("SKIP") == 4 and "FAIL" not in out


def test_model_pass_and_one_failure_does_not_hide_others(tmp_path, monkeypatch, capsys):
    (tmp_path / "codepilot.yml").write_text(CFG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    monkeypatch.setattr(pf, "_chat_model", lambda name: NS(invoke=lambda q: AIMessage(content="OK")))
    monkeypatch.setenv("DAYTONA_API_KEY", "test-not-real")
    monkeypatch.setattr(pf, "check_daytona", lambda settings: (_ for _ in ()).throw(RuntimeError("401 bad key")))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert pf.main(["--config", str(tmp_path / "codepilot.yml")]) == 1
    out = capsys.readouterr().out
    assert "PASS model" in out and "FAIL daytona  RuntimeError: 401 bad key" in out and "SKIP bedrock" in out
