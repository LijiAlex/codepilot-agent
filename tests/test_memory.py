# tests/test_memory.py
import hashlib
import math
import re
import time

import pytest

from codepilot.agents.tools import make_semantic_search_tool
from codepilot.memory.episodic import EpisodicMemory
from codepilot.memory.semantic import FileIndex, LessonStore, chunk
from codepilot.memory.working import WorkingMemory


def hash_embed(texts):
    """Offline stand-in for MiniLM: hashed bag of words, unit length."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in re.findall(r"[a-z_]+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def test_working_memory_roundtrip_and_unknown_fields(tmp_path):
    wm = WorkingMemory(tmp_path)
    wm.init({"number": 7, "title": "t"}, "bug_fix")
    wm.update(relevant_files=["pkg/a.py"], retry_count=1)
    d = wm.read()
    assert d["issue"]["number"] == 7 and d["relevant_files"] == ["pkg/a.py"] and d["retry_count"] == 1
    with pytest.raises(KeyError):
        wm.update(secret="x")
    wm.clear()
    assert wm.read() == {}


def test_episodic_recently_failed_uses_last_three_sessions_and_ignores_crashes(tmp_path):
    em = EpisodicMemory(tmp_path / "episodic.db")
    em.save_session("s0", "old", {}); time.sleep(0.002)
    em.record_run("o/r", "r0", "o/r#1", "FAILED", "tests", "old failure"); time.sleep(0.002)
    for s in ("s1", "s2", "s3"):
        em.save_session(s, "x", {}); time.sleep(0.002)
    em.record_run("o/r", "r1", "o/r#2", "FAILED", "tests", "")
    em.record_run("o/r", "r2", "o/r#3", "FAILED", "crashed", "")
    em.record_run("o/r", "r3", "o/r#4", "DONE", None, "")
    assert [s["session_id"] for s in em.last_sessions()] == ["s3", "s2", "s1"]
    assert em.recently_failed("o/r") == {"o/r#2"}
    em.save_session("s3", "updated summary", {"done": 1})          # incremental update keeps its place
    assert em.last_sessions()[0]["summary"] == "updated summary" and em.last_sessions()[0]["session_id"] == "s3"
    assert [e["run_id"] for e in em.episodes("o/r")][:2] == ["r3", "r2"]


def test_lessons_filtered_by_repo_and_type_deduplicated_top3(tmp_path):
    ls = LessonStore(tmp_path / "chroma", hash_embed)
    assert ls.top("o/r", "bug_fix", "slugify") == []
    a = ls.add("o/r", "bug_fix", "slugify bugs live in pkg/slugs.py; test with empty strings", "merged", "r1")
    assert ls.add("o/r", "bug_fix", "slugify bugs live in pkg/slugs.py; test with empty strings", "merged", "r2") == a
    ls.add("o/r", "bug_fix", "date parsing uses utils/dates.py", "merged", "r3")
    ls.add("o/r", "feature_addition", "slugify features go in pkg/slugs.py too", "merged", "r4")
    ls.add("x/y", "bug_fix", "slugify in another repo", "merged", "r5")
    top = ls.top("o/r", "bug_fix", "slugify crashes on empty title")
    assert top[0].startswith("slugify bugs") and len(top) == 2 and all("another repo" not in t for t in top)
    assert len(ls.all("o/r")) == 3


def test_file_index_build_search_refresh_and_tool(tmp_path):
    root = tmp_path / "mirror"; (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "slugs.py").write_text("def slugify(title):\n    return title.lower().replace(' ', '-')\n")
    (root / "pkg" / "dates.py").write_text("def parse_date(text):\n    return text\n")
    (root / "logo.png").write_bytes(b"\x89PNG")
    fi = FileIndex(tmp_path / "chroma", "o/r", hash_embed)
    assert fi.search("anything") == [] and fi.sha is None
    assert fi.build(root, ["pkg/slugs.py", "pkg/dates.py", "logo.png"], "sha1") == 2 and fi.sha == "sha1"
    assert fi.search("slugify title lower")[0][0] == "pkg/slugs.py"
    (root / "pkg" / "dates.py").unlink()
    fi.refresh(root, ["pkg/dates.py"], "sha2")
    assert [p for p, _ in fi.search("parse date text")] == ["pkg/slugs.py"] and fi.sha == "sha2"
    out = make_semantic_search_tool(fi).invoke({"query": "slugify"})
    assert out.startswith("/sandbox/pkg/slugs.py")


def test_chunks_stay_small():
    parts = chunk("x = 1\n" * 1000)
    assert len(parts) > 1 and all(len(p) <= 800 for p in parts)


def test_episodes_are_structured_and_session_rows_filtered(tmp_path):
    em = EpisodicMemory(tmp_path / "e.db")
    t0 = time.time_ns()
    em.record_run("o/r", "r1", "o/r#1", "DONE", "merged", "t", task_type="bug_fix", files=["pkg/a.py"], duration_s=42.0)
    em.record_run("x/y", "r2", "x/y#2", "FAILED", "tests", "t", task_type="feature_addition")
    rows = em.session_episodes(["o/r", "x/y"], t0)
    assert [(r["run_id"], r["task_type"], r["files"], r["duration_s"]) for r in rows] == [
        ("r1", "bug_fix", ["pkg/a.py"], 42.0), ("r2", "feature_addition", [], None)]
    assert em.session_episodes(["o/r"], time.time_ns()) == []


def test_later_record_keeps_earlier_fields(tmp_path):
    em = EpisodicMemory(tmp_path / "e.db")
    em.record_run("o/r", "r1", "o/r#1", "AWAITING_APPROVAL", "publish", "t", task_type="bug_fix", files=["a.py"], duration_s=61.5)
    em.record_run("o/r", "r1", "o/r#1", "DONE", "merged", "t")
    e = em.episodes("o/r")[0]
    assert (e["outcome"], e["task_type"], e["files"], e["duration_s"]) == ("DONE", "bug_fix", ["a.py"], 61.5)


def test_infrastructure_failures_do_not_block_a_retry(tmp_path):
    em = EpisodicMemory(tmp_path / "episodic.db")
    em.save_session("s1", "x", {}); time.sleep(0.002)
    em.record_run("o/r", "r1", "o/r#1", "FAILED", "setup: box command failed (1): cd … requires a different Python", "")
    em.record_run("o/r", "r2", "o/r#2", "FAILED", "sandbox_lost", "")
    em.record_run("o/r", "r3", "o/r#3", "FAILED", "tests", "")
    em.record_run("o/r", "r4", "o/r#4", "FAILED", "provider: credit balance too low", "")
    assert em.recently_failed("o/r") == {"o/r#3"}


def test_empty_sessions_do_not_push_a_failure_out_of_the_window(tmp_path):
    # live 2026-10-18: TUI restarts ("no runs") made #7 eligible again after one failure
    em = EpisodicMemory(tmp_path / "episodic.db")
    em.save_session("s1", "FAILED(tests): 1", {"counts": {"FAILED(tests)": 1}}); time.sleep(0.002)
    em.record_run("o/r", "r1", "o/r#7", "FAILED", "tests", ""); time.sleep(0.002)
    for s in ("s2", "s3", "s4"):
        em.save_session(s, "no runs", {"counts": {}}); time.sleep(0.002)
    assert em.recently_failed("o/r") == {"o/r#7"}
