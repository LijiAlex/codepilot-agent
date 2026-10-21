# tests/test_connect.py
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

from codepilot.config import load_settings
from codepilot.connect import connect, detect_setup
from codepilot.db import connect as db_connect
from codepilot.improve.repo_skill import RepoSkillStore
from codepilot.repomap.mirror import HostMirror


def mk(tmp_path, files):
    r = tmp_path / "repo"
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True); (r / rel).write_text(text)
    return r


def test_detect_packaging_tests_plugins_and_other_languages(tmp_path):
    a = detect_setup(mk(tmp_path / "a", {"pyproject.toml": "[project]\nname='x'\n[tool.pytest.ini_options]\n", "pkg/x.py": ""}))
    assert (a.install_command, a.test_command, a.notes) == ("pip install -e . pytest", "python -m pytest -q", [])
    b = detect_setup(mk(tmp_path / "b", {"requirements.txt": "rich\n", "requirements-dev.txt": "", "tests/test_x.py": ""}))
    assert b.install_command == "pip install -r requirements.txt -r requirements-dev.txt pytest"
    c = detect_setup(mk(tmp_path / "c", {"x.py": "", ".claude-plugin/plugin.json": "{}", "plugins/p/.claude-plugin/plugin.json": "{}",
                                         "package.json": "{}"}))
    assert c.install_command == "pip install pytest" and c.protected == [".claude-plugin/**", "plugins/p/.claude-plugin/**"]
    assert any("no tests found" in n for n in c.notes) and any("JavaScript" in n for n in c.notes)


class FakeRepo:
    default_branch = "main"
    def __init__(self): self.labels = {"bug"}
    def get_labels(self): return [NS(name=n) for n in self.labels]
    def create_label(self, name, color): self.labels.add(name)


def test_connect_end_to_end_with_local_mirror_and_config_write(tmp_path):
    origin = mk(tmp_path / "o", {"pyproject.toml": "[project]\nname='x'\n", "pkg/slugs.py": '"""Slug helpers."""\ndef slugify(t):\n    return t\n',
                                 "tests/test_slugs.py": "def test_x():\n    assert 1\n"})
    for c in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=origin, check=True)
    home = tmp_path / "home"
    subprocess.run(["git", "clone", "-q", str(origin), str(home / "repos" / "o" / "r" / "mirror")], check=True)
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text("poll_interval_s: 300\nrepos:\n  - owner: other\n    name: thing\n")
    skills = RepoSkillStore(db_connect(tmp_path / "cp.db"))
    fake = FakeRepo()
    rep = connect("o", "r", home, repo_factory=lambda n: fake, mirror=HostMirror(home), skills=skills, config_path=cfg)
    assert set(rep.labels_created) == {"ai-assignable", "codepilot-generated", "needs-review", "codepilot-review-pending", "codepilot-review-completed"} and "bug" in fake.labels
    assert rep.setup.install_command == "pip install -e . pytest" and rep.map_files >= 3 and rep.repo_skill_version == 1
    assert rep.config_written and rep.snapshot is None
    s = load_settings(cfg)
    assert [r.full_name for r in s.repos] == ["o/r", "other/thing"] and s.repos[0].install_command == "pip install -e . pytest"
    again = connect("o", "r", home, repo_factory=lambda n: fake, mirror=HostMirror(home), skills=skills, config_path=cfg)
    assert not again.config_written and again.labels_created == [] and len(load_settings(cfg).repos) == 2
    assert "`pkg/slugs.py`: Slug helpers." in skills.render("o/r")[1]


def test_connect_creates_a_snapshot_when_asked(tmp_path):
    origin = mk(tmp_path / "o", {"requirements.txt": "rich\n", "a.py": "x = 1\n"})
    for c in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=origin, check=True)
    home = tmp_path / "home"
    subprocess.run(["git", "clone", "-q", str(origin), str(home / "repos" / "O" / "R_x" / "mirror")], check=True)
    made = []
    client = NS(snapshot=NS(create=lambda params, on_logs=None: made.append(params)))
    rep = connect("O", "R_x", home, repo_factory=lambda n: FakeRepo(), mirror=HostMirror(home),
                  skills=RepoSkillStore(db_connect(tmp_path / "cp.db")), daytona_client=client)
    assert rep.snapshot == "codepilot-o-r-x" and made[0].name == "codepilot-o-r-x"
    df = made[0].image.dockerfile()
    assert "pip install -r /.requirements.txt" in df and "apt-get install -y git" in df and df.endswith("pip install pytest\n")


def test_cli_connect_prints_setup_and_block(tmp_path, monkeypatch, capsys):
    import codepilot.cli as cli
    from codepilot.connect import ConnectReport, DetectedSetup
    cfg = tmp_path / "codepilot.yml"
    cfg.write_text(f"repos: []\nhome: {tmp_path / 'home'}\n")
    monkeypatch.setattr("codepilot.gh.auth.TokenProvider.from_env", classmethod(lambda cls, *a: NS(repo=lambda: None)))
    seen = {}

    def fake_connect(owner, name, home, **kw):
        seen.update(owner=owner, name=name, **kw)
        return ConnectReport("o/r", "main", ["ai-assignable"], DetectedSetup("pip install pytest", "python -m pytest -q", [], ["no tests found"]),
                             900, 12, 1, None, None, "- owner: o\n  name: r\n", False)
    monkeypatch.setattr("codepilot.connect.connect", fake_connect)
    assert cli.main(["--config", str(cfg), "connect", "o/r", "--no-index"]) == 0
    out = capsys.readouterr().out
    assert seen["owner"] == "o" and seen["file_index"] is None and seen["daytona_client"] is None and seen["config_path"] is None
    assert "note: no tests found" in out and "repo skill v1" in out and "- owner: o" in out


def test_connect_writes_into_an_emptied_repos_list(tmp_path):
    origin = mk(tmp_path / "o", {"pyproject.toml": "[project]\nname='x'\n", "a.py": "x = 1\n"})
    for c in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", *c], cwd=origin, check=True)
    home = tmp_path / "home"
    subprocess.run(["git", "clone", "-q", str(origin), str(home / "repos" / "o" / "r" / "mirror")], check=True)
    for i, empty in enumerate(["repos:\n", "repos: []\n"]):       # README: delete the example entries, then connect
        cfg = tmp_path / f"c{i}.yml"
        cfg.write_text("poll_interval_s: 300\n" + empty + "sandbox: daytona\n")
        connect("o", "r", home, repo_factory=lambda n: FakeRepo(), mirror=HostMirror(home),
                skills=RepoSkillStore(db_connect(tmp_path / f"cp{i}.db")), config_path=cfg)
        assert [r.full_name for r in load_settings(cfg).repos] == ["o/r"]
