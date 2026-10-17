# tests/test_treesitter_map.py
import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_javascript")

from codepilot.repomap import treesitter  # noqa: E402
from codepilot.repomap.build import _pagerank, build_repo_map, scan  # noqa: E402


def test_js_ts_go_symbols_and_resolved_imports(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.js").write_text("export function helper(t) { return t }\n")
    (tmp_path / "src" / "app.js").write_text('import { helper } from "./util.js";\nimport React from "react";\n'
                                             "export function slugify(t) { return helper(t) }\nclass Box {}\nconst go = () => 1;\n")
    (tmp_path / "src" / "types.ts").write_text("export interface Thing { id: string }\nexport class Store {}\nimport { helper } from './util';\n")
    (tmp_path / "main.go").write_text('package main\nimport "fmt"\nfunc Hello() { fmt.Println(1) }\ntype T struct{}\nfunc (t T) Run() {}\n')
    assert treesitter.available()
    syms, imps = treesitter.parse(tmp_path, tmp_path / "src" / "app.js")
    assert syms == ["slugify", "Box", "go"] and imps == ["src/util.js", "react"]
    tsyms, timps = treesitter.parse(tmp_path, tmp_path / "src" / "types.ts")
    assert tsyms == ["Thing", "Store"] and timps == ["src/util.js"]
    gsyms, gimps = treesitter.parse(tmp_path, tmp_path / "main.go")
    assert gsyms == ["Hello", "T", "Run"] and gimps == ["fmt"]
    entries = scan(tmp_path)
    rank = _pagerank(entries, {})
    assert rank["src/util.js"] > rank["src/app.js"]                      # imported twice → ranked higher
    m = build_repo_map(tmp_path, "abc12345", entries=entries)
    assert "src/app.js [javascript]" in m.text and "slugify" in m.text and "Hello" in m.text
