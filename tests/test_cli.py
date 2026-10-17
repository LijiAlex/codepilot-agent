# tests/test_cli.py
from codepilot.cli import main
from codepilot.events import EventRecorder, RunEvent


def test_selfcheck_command(capsys):
    assert main(["selfcheck"]) == 0 and "tool sets OK" in capsys.readouterr().out


def test_replay_by_run_id_is_labelled(tmp_path, capsys):
    rec = EventRecorder(tmp_path / "runs" / "r1" / "events.jsonl")
    rec.write(RunEvent("r1", "tool_call", "coder", "edit_file(pkg/a.py)"))
    assert main(["replay", "r1", "--home", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "REPLAY of a recorded run (r1) — not live" in out and "[coder] tool_call: edit_file(pkg/a.py)" in out


def test_missing_config_is_explained(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codepilot.cli.DEFAULT_CONFIGS", [tmp_path / "codepilot.yml"])
    try:
        main(["once"])
    except SystemExit as e:
        assert "codepilot.example.yml" in str(e)
    else:
        raise AssertionError("expected SystemExit")
