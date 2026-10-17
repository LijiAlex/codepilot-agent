# src/codepilot/skills/__init__.py
"""Task skills: structured playbooks, one per task type the brief defines."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

TASK_TYPES = ("bug_fix", "feature_addition", "dependency_update", "documentation", "config_change")
SKILLS_DIR = Path(__file__).parent


class Skill(BaseModel):
    name: str
    display_name: str
    task_type: str
    description: str
    instructions: str
    workflow_steps: list[str]
    example_prompts: list[str]
    forbidden_actions: list[str]
    spawns: list[str]
    full_test_suite: bool = False
    notes: str = ""


def _parse(path: Path) -> Skill:
    text = path.read_text()
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    cp = meta["codepilot"]
    return Skill(name=meta["name"], description=meta["description"], notes=body.strip(), **cp)


def load_skills(root: Path | None = None) -> dict[str, Skill]:
    root = root or SKILLS_DIR
    skills = [_parse(p) for p in sorted(root.glob("*/SKILL.md"))]
    return {s.task_type: s for s in skills}


def render_skill_prompt(skill: Skill, part: Literal["all", "testing", "explore"] = "all") -> str:
    if part == "testing":
        return (f"Task skill: {skill.display_name}\nTesting rule: "
                f"{'run the full test suite' if skill.full_test_suite else 'run the tests relevant to the change'}.\n"
                f"Forbidden: {'; '.join(a for a in skill.forbidden_actions if 'test' in a)}")
    if part == "explore":
        return f"Task skill: {skill.display_name}\nFirst steps: {', '.join(skill.workflow_steps[:2])}"
    return (f"Task skill: {skill.display_name}\n{skill.instructions}\n"
            f"Workflow: {' -> '.join(skill.workflow_steps)}\n"
            f"Forbidden: {'; '.join(skill.forbidden_actions)}\n{skill.notes}")
