# tests/test_release_check.py
import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("check_release", ROOT / "scripts" / "check_release.py")
cr = importlib.util.module_from_spec(spec); spec.loader.exec_module(cr)


def repo_with(tmp_path, files: dict[str, str], email=cr.NOREPLY) -> Path:
    r = tmp_path / "r"; r.mkdir()
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True); (r / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", f"user.email={email}", "-c", "user.name=x", "commit", "-qm", "i"], cwd=r, check=True)
    return r


GOOD_README = "\n".join(cr.README_SECTIONS) + "\nBrief / Bonus / Plus. Built on langchain-core.\n"


def test_clean_repo_passes(tmp_path):
    assert cr.check(repo_with(tmp_path, {"README.md": GOOD_README, "src/a.py": "x = 1\n"}), ["zebracorp"]) == []


def test_finds_words_secrets_env_tiers_and_emails(tmp_path):
    r = repo_with(tmp_path, {"README.md": "## Demo\nThis is a stretch goal\n", "src/a.py": "# ZebraCorp internal\n",
                             ".env": "K=1", "notes.txt": "token ghp_" + "a" * 30}, email="me@zebracorp.example")
    probs = "\n".join(cr.check(r, ["zebracorp", "# comment lines are ignored"]))
    assert "forbidden word 'zebracorp' in src/a.py" in probs and "tracked: .env" in probs
    assert "possible secret" in probs and "README section missing: ## Quick start" in probs
    assert "build tier ('stretch goal')" in probs and "me@zebracorp.example" in probs


def test_main_needs_the_outside_word_list(tmp_path, capsys):
    assert cr.main(["--repo", str(tmp_path), "--forbidden", str(tmp_path / "missing.txt")]) == 2


def test_readme_has_every_section_and_no_fill_markers_left(tmp_path):
    tpl = (ROOT / "README.md").read_text()
    r = repo_with(tmp_path, {"README.md": tpl})
    probs = cr.check(r, [])
    assert not [p for p in probs if "section missing" in p or "build tier" in p or "FILL: marker" in p]
    (tmp_path / "t").mkdir()
    assert cr.check(repo_with(tmp_path / "t", {"README.md": tpl + "\nFILL: demo link\n"}), [])  # a marker still blocks
