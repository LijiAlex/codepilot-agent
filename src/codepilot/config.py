# src/codepilot/config.py
"""Settings for one CodePilot+ installation, read from a YAML file.

Secrets never live in this file: it names the environment variables that hold
them, and only CodePilot's own code reads those variables.
"""
from __future__ import annotations

from typing import Literal

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_PROTECTED = [
    ".github/**", "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
    "**/*.secret", "**/*credentials*", "uv.lock", "poetry.lock", "package-lock.json",
]


class RepoConfig(BaseModel):
    owner: str
    name: str
    default_branch: str = "main"
    test_command: str = "pytest -q"
    install_command: str | None = None
    protected_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED))
    author_allowlist: list[str] = Field(default_factory=list)
    poll_unassigned_below_threshold: bool = False

    @field_validator("protected_paths")
    @classmethod
    def _keep_defaults(cls, v: list[str]) -> list[str]:
        """Paths in codepilot.yml add to the defaults; they can never remove .github/** or the secret patterns."""
        return list(dict.fromkeys([*DEFAULT_PROTECTED, *v]))

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class Settings(BaseModel):
    repos: list[RepoConfig]
    poll_interval_s: int = 300
    complexity_threshold: int = 6
    solver_model: str = "anthropic:claude-sonnet-5-5"
    reviewer_model: str = "gpt-6.1-sol"
    sandbox: str = "daytona"            # "daytona" | "local"
    daytona_snapshot: str | None = None  # None = Daytona default image; per-repo snapshot is Plus (connect)
    daytona_auto_stop_minutes: int = 30
    max_model_calls_per_run: int = 150
    max_tool_calls_per_run: int = 400
    max_active_minutes_per_run: int = 30
    repo_map_budget_tokens: int = 4000      # brief: configurable, default 4000
    top_k: int = 10                         # brief: top-K relevant files, default 10
    price_in_per_mtok: float | None = None   # solver model prices (USD per million tokens); None = cost not shown
    price_out_per_mtok: float | None = None
    langsmith_project: str = "codepilot-plus"
    reviewer_login: str | None = None       # "<reviewer app slug>[bot]" (Plus: independent reviewer)
    github_app_id_env: str = "CODEPILOT_GH_APP_ID"
    github_app_key_path_env: str = "CODEPILOT_GH_APP_KEY_PATH"
    guardrail_policy_path: Path = Path("guardrails/codepilot.yaml")
    injection_checker: Literal["nemo", "bedrock"] = "nemo"   # issue screen: NeMo Guardrails (brief) or the A3 Bedrock guardrail
    nemo_config_path: Path = Path("guardrails/nemo")
    guardrail_model: str = "anthropic:claude-haiku-5-5"      # the NeMo self-check rail's model (one short yes/no call per issue)
    home: Path = Field(default_factory=lambda: Path.home() / ".codepilot")


def load_settings(path: Path) -> Settings:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Settings.model_validate(data)
