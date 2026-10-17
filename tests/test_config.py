# tests/test_config.py
from pathlib import Path
from codepilot.config import load_settings

def test_load_settings_defaults(tmp_path: Path):
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text(
        "repos:\n"
        "  - owner: LijiAlex\n"
        "    name: layered-memory\n"
        "    test_command: pytest -q\n"
        "github_app_id_env: CODEPILOT_GH_APP_ID\n"
        "github_app_key_path_env: CODEPILOT_GH_APP_KEY_PATH\n"
    )
    s = load_settings(cfg)
    assert s.poll_interval_s == 300
    assert s.complexity_threshold == 6
    assert s.repo_map_budget_tokens == 4000 and s.top_k == 10
    assert s.repos[0].full_name == "LijiAlex/layered-memory"
    assert ".github/**" in s.repos[0].protected_paths
    assert s.solver_model == "anthropic:claude-sonnet-5-5"
    assert s.home == Path.home() / ".codepilot"


def test_protected_paths_add_to_defaults(tmp_path: Path):
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text("repos:\n  - owner: o\n    name: r\n    protected_paths: ['.claude-plugin/**']\n")
    paths = load_settings(cfg).repos[0].protected_paths
    assert ".claude-plugin/**" in paths and ".github/**" in paths and "**/.env" in paths
