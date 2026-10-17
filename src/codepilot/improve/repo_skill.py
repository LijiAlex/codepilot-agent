# src/codepilot/improve/repo_skill.py
"""Per-repository skill (Plus): ordered steps a person would tell a new teammate — setup, where things live, how to run
the tests, what never to touch, gotchas. Always loaded for that repo (≤1500 tokens). Changes are deltas only
(add / correct / retire a step, never a full rewrite); every version is kept, so any version can be restored."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from codepilot.config import DEFAULT_PROTECTED
from codepilot.repomap.build import estimate_tokens

SECTIONS = ("setup", "locations", "tests", "never_touch", "gotchas")
MAX_TOKENS = 1500
SCHEMA = """
create table if not exists repo_skill_versions(
  repo text not null, version integer not null, steps_json text not null, change text not null,
  created_at text default (datetime('now')), primary key(repo, version));
"""


@dataclass
class Step:
    id: str
    section: str
    text: str
    provenance: str                     # "config", "repo map", or the run id whose evidence led to it
    retired: bool = False


class RepoSkillStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(SCHEMA)

    def current(self, repo: str) -> tuple[int, list[Step]]:
        row = self.conn.execute("select version, steps_json from repo_skill_versions where repo=? order by version desc limit 1",
                                (repo,)).fetchone()
        return (row[0], [Step(**s) for s in json.loads(row[1])]) if row else (0, [])

    def _save(self, repo: str, version: int, steps: list[Step], change: str) -> int:
        self.conn.execute("insert into repo_skill_versions(repo, version, steps_json, change) values(?,?,?,?)",
                          (repo, version, json.dumps([asdict(s) for s in steps]), change))
        return version

    def ensure_v1(self, repo_cfg, map_files) -> int:
        """Version 1 from what is known without any model: config and the repo map (spec §4.1, no `connect`)."""
        v, _ = self.current(repo_cfg.full_name)
        if v:
            return v
        steps = []
        if repo_cfg.install_command:
            steps.append(Step("s1", "setup", f"Dependencies are installed by CodePilot+ with `{repo_cfg.install_command}`.", "config"))
        steps.append(Step(f"s{len(steps) + 1}", "tests", f"Run the tests with `{repo_cfg.test_command}`.", "config"))
        for f in [f for f in map_files if f.language == "python"][:5]:
            steps.append(Step(f"s{len(steps) + 1}", "locations", f"`{f.path}`: {f.summary}", "repo map"))
        extra = [p for p in repo_cfg.protected_paths if p not in DEFAULT_PROTECTED]
        steps.append(Step(f"s{len(steps) + 1}", "never_touch", "Never edit " + ", ".join([".github/", "lockfiles", "secrets", *extra]) + ".", "config"))
        return self._save(repo_cfg.full_name, 1, steps, "v1 from config and repo map")

    def render(self, repo: str) -> tuple[int, str]:
        v, steps = self.current(repo)
        if not v:
            return 0, ""
        lines = [f"Repo skill v{v} for {repo} (follow these; they come from earlier work in this repo):"]
        for sec in SECTIONS:
            active = [s for s in steps if s.section == sec and not s.retired]
            if active:
                lines.append(f"{sec.replace('_', ' ')}:")
                lines += [f"- [{s.id}] {s.text}" for s in active]
        text = "\n".join(lines)
        while estimate_tokens(text) > MAX_TOKENS and len(lines) > 2:     # keep within budget: drop the last lines
            lines.pop()
            text = "\n".join(lines)
        return v, text

    def apply(self, repo: str, op: str, section: str, text: str, step_id: str | None, provenance: str) -> int:
        v, steps = self.current(repo)
        if not v:
            raise ValueError(f"{repo} has no repo skill yet")
        if section not in SECTIONS:
            raise ValueError(f"unknown section {section}")
        if op == "add":
            steps.append(Step(f"s{len(steps) + 1}", section, text, provenance))
            change = f"add {section}: {text[:80]}"
        elif op in ("correct", "retire"):
            target = next((s for s in steps if s.id == step_id and not s.retired), None)
            if target is None:
                raise ValueError(f"no active step {step_id}")
            if op == "correct":
                target.retired = True
                steps.append(Step(f"s{len(steps) + 1}", target.section, text, provenance))
                change = f"correct {step_id} → s{len(steps)}: {text[:80]}"
            else:
                target.retired = True
                change = f"retire {step_id}"
        else:
            raise ValueError(f"unknown op {op}")
        return self._save(repo, v + 1, steps, change)

    def rollback(self, repo: str, version: int) -> int:
        row = self.conn.execute("select steps_json from repo_skill_versions where repo=? and version=?", (repo, version)).fetchone()
        if row is None:
            raise ValueError(f"{repo} has no version {version}")
        v, _ = self.current(repo)
        return self._save(repo, v + 1, [Step(**s) for s in json.loads(row[0])], f"rollback to v{version}")

    def history(self, repo: str) -> list[dict]:
        return [{"version": r[0], "created_at": r[1], "change": r[2]} for r in
                self.conn.execute("select version, created_at, change from repo_skill_versions where repo=? order by version", (repo,))]
