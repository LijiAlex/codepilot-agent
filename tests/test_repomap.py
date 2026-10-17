# tests/test_repomap.py
import subprocess
from pathlib import Path
from codepilot.repomap.build import build_repo_map, estimate_tokens

def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "pkg" / "slugs.py").write_text('"""Make URL slugs."""\ndef slugify(title):\n    return title.lower()\ndef _helper():\n    pass\n')
    (r / "pkg" / "build.py").write_text('"""Build summaries."""\nfrom pkg.slugs import slugify\nclass Builder:\n    pass\n')
    (r / "tests" / "test_slugs.py").write_text("from pkg.slugs import slugify\ndef test_x():\n    assert slugify('A')=='a'\n")
    (r / "README.md").write_text("# Demo\n")
    for i in range(300):
        (r / "pkg" / f"mod_{i}.py").write_text(f'"""Module number {i} with a long description to use up budget."""\ndef f{i}():\n    pass\n')
    return r

def test_map_fits_budget_and_has_tree_language_summary(tmp_path):
    m = build_repo_map(_repo(tmp_path), sha="abc", budget_tokens=4000)
    assert m.tokens <= 4000 and estimate_tokens(m.text) <= 4000
    assert "pkg/" in m.text                       # directory tree
    e = next(f for f in m.files if f.path == "pkg/slugs.py")
    assert e.language == "python" and e.summary == "Make URL slugs." and "slugify" in e.symbols
    assert all(f.summary for f in m.files)        # every file has a one-line description

def test_focus_terms_rank_relevant_files_first(tmp_path):
    m = build_repo_map(_repo(tmp_path), sha="abc", budget_tokens=600, focus_terms=["slugify"])
    assert "pkg/slugs.py" in m.text               # survives a tight budget because it is focused

from codepilot.repomap.cache import RepoMapCache
from codepilot.repomap.build import scan

def test_cache_refresh_reparses_only_changed(tmp_path):
    r = _repo(tmp_path)
    cache = RepoMapCache(tmp_path / "cache")
    cache.put("s1", scan(r))
    (r / "pkg" / "slugs.py").write_text('"""Slug helpers v2."""\ndef slugify(t):\n    return t\n')
    entries = cache.refresh(r, "s1", "s2", ["pkg/slugs.py", "pkg/gone.py"])
    assert next(e for e in entries if e.path == "pkg/slugs.py").summary == "Slug helpers v2."
    assert cache.get("s2") is not None


def test_src_layout_and_package_imports_become_edges(tmp_path):
    from codepilot.repomap.build import _pagerank, scan
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("from pkg.core import run\n")
    (tmp_path / "src" / "pkg" / "core.py").write_text("def run():\n    pass\n")
    for i in range(3):
        (tmp_path / "src" / "pkg" / f"user{i}.py").write_text("import pkg\nfrom pkg.core import run\n")
    entries = scan(tmp_path)
    rank = _pagerank(entries, {})
    assert rank["src/pkg/core.py"] > rank["src/pkg/user0.py"] and rank["src/pkg/__init__.py"] > rank["src/pkg/user0.py"]


def test_pagerank_is_normalised_and_linear_enough_for_big_repos():
    import time
    from codepilot.repomap.build import FileEntry, _pagerank
    entries = [FileEntry(f"pkg/m{i}.py", "python", "", [], [f"pkg.m{i - 1}"] if i else []) for i in range(3000)]
    t0 = time.perf_counter()
    rank = _pagerank(entries, {"pkg/m5.py": 10.0})            # partial weights: others default to 1
    assert time.perf_counter() - t0 < 2.0                     # was quadratic (67 s on Django's 7,084 files)
    assert abs(sum(rank.values()) - 1.0) < 1e-9 and rank["pkg/m0.py"] > rank["pkg/m2999.py"]

from codepilot.repomap.cache import RepoMapCache
from codepilot.repomap.build import scan
