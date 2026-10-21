# src/codepilot/connect.py
"""`codepilot connect owner/name` (Plus): one command to make a repository ready — access check with a repo-scoped token,
labels, setup detection, repo map, repo skill v1, file index, optional Daytona snapshot, and the codepilot.yml entry."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from codepilot.config import RepoConfig
from codepilot.gh.writer import ensure_labels
from codepilot.repomap.build import build_repo_map, scan
from codepilot.repomap.cache import RepoMapCache

LABELS = ["ai-assignable", "codepilot-generated", "needs-review", "codepilot-review-pending", "codepilot-review-completed"]
OTHER_LANGS = {"package.json": "JavaScript", "go.mod": "Go", "Cargo.toml": "Rust", "pom.xml": "Java", "Gemfile": "Ruby"}


@dataclass
class DetectedSetup:
    install_command: str
    test_command: str
    protected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def detect_setup(root: Path) -> DetectedSetup:
    root = Path(root)
    notes: list[str] = []
    py = root / "pyproject.toml"
    project = tomllib.loads(py.read_text()).get("project") if py.exists() else None
    if project or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        install = "pip install -e . pytest"
    elif (root / "requirements.txt").exists():
        extra = " -r requirements-dev.txt" if (root / "requirements-dev.txt").exists() else ""
        install = f"pip install -r requirements.txt{extra} pytest"
    else:
        install = "pip install pytest"
        notes.append("no Python packaging found: treated as plain scripts (pytest only)")
    pytest_cfg = (root / "pytest.ini").exists() or (py.exists() and "pytest" in tomllib.loads(py.read_text()).get("tool", {}))
    has_tests = (root / "tests").is_dir() or any(root.rglob("test_*.py"))
    if not (pytest_cfg or has_tests):
        notes.append("no tests found: the Test Agent will add the first ones (pytest exit 5 is not a failure)")
    protected = sorted({f"{p.parent.relative_to(root).as_posix()}/**".removeprefix("./")
                        for p in root.rglob(".claude-plugin/*.json")})
    for marker, lang in OTHER_LANGS.items():
        if (root / marker).exists():
            notes.append(f"{lang} files found ({marker}): the repo map and test parsing are Python-first; check test_command")
    return DetectedSetup(install, "python -m pytest -q", protected, notes)


@dataclass
class ConnectReport:
    repo: str
    default_branch: str
    labels_created: list[str]
    setup: DetectedSetup
    map_tokens: int
    map_files: int
    repo_skill_version: int
    indexed_chunks: int | None
    snapshot: str | None
    config_block: str
    config_written: bool


def snapshot_image(root: Path, setup: DetectedSetup):
    from daytona import Image
    img = Image.debian_slim("3.12").run_commands("apt-get update && apt-get install -y git")
    req = Path(root) / "requirements.txt"
    if req.exists():
        img = img.pip_install_from_requirements(str(req))
    return img.pip_install("pytest")


def connect(owner: str, name: str, home: Path, *, repo_factory, mirror, skills, file_index=None, daytona_client=None,
            config_path: Path | None = None) -> ConnectReport:
    full = f"{owner}/{name}"
    repo = repo_factory(full)                                    # access check: repo-scoped token or a clear error
    branch = repo.default_branch
    created = ensure_labels(repo, LABELS)
    path = mirror.ensure(owner, name)
    sha = mirror.head_sha(path, f"origin/{branch}")
    mirror.checkout(path, sha)
    setup = detect_setup(path)
    cache = RepoMapCache(Path(home) / "repos" / owner / name / "maps")
    entries = cache.get(sha) or scan(path)
    cache.put(sha, entries)
    rmap = build_repo_map(path, sha, entries=entries)
    cfg = RepoConfig(owner=owner, name=name, default_branch=branch, install_command=setup.install_command,
                     test_command=setup.test_command, protected_paths=setup.protected)
    version = skills.ensure_v1(cfg, rmap.files)
    chunks = file_index.build(path, [e.path for e in entries], sha) if file_index is not None else None
    snap = None
    if daytona_client is not None:
        from daytona import CreateSnapshotParams
        snap = f"codepilot-{owner}-{name}".lower().replace("_", "-")
        daytona_client.snapshot.create(CreateSnapshotParams(name=snap, image=snapshot_image(path, setup)), on_logs=print)
    entry = {"owner": owner, "name": name, "default_branch": branch, "install_command": setup.install_command,
             "test_command": setup.test_command}
    if setup.protected:
        entry["protected_paths"] = setup.protected
    block = yaml.safe_dump([entry], sort_keys=False)
    written = False
    if config_path is not None:
        data = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
        repos = (data or {}).get("repos") or []
        if not any(r.get("owner") == owner and r.get("name") == name for r in repos):
            text = config_path.read_text() if config_path.exists() else ""
            if "repos:" not in text:
                text += ("" if text.endswith("\n") or not text else "\n") + "repos:\n"
                config_path.write_text(text + "".join("  " + line + "\n" for line in block.splitlines()))
            else:
                lines = text.splitlines(keepends=True)
                i = next(n for n, line in enumerate(lines) if line.startswith("repos:"))
                lines[i] = "repos:\n"                     # `repos: []` would make the inserted entry invalid YAML
                lines.insert(i + 1, "".join("  " + line + "\n" for line in block.splitlines()))
                config_path.write_text("".join(lines))
            written = True
    return ConnectReport(full, branch, created, setup, rmap.tokens, len(rmap.files), version, chunks, snap, block, written)
