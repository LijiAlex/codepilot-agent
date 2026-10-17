# src/codepilot/repomap/treesitter.py
"""Symbols and imports for JavaScript, TypeScript and Go via tree-sitter (Plus, optional extra `maps`).
Grammars come from the per-language wheels, so nothing is downloaded at run time. Without the extra installed,
`parse` returns None and the map falls back to a one-line summary for these files."""
from __future__ import annotations

import posixpath
from functools import cache
from pathlib import Path

QUERIES = {
    "javascript": ("(function_declaration name: (identifier) @def) (class_declaration name: (identifier) @def) "
                   "(variable_declarator name: (identifier) @def value: [(arrow_function) (function_expression)]) "
                   "(import_statement source: (string) @import) (call_expression function: (identifier) @ref)"),
    "go": ("(function_declaration name: (identifier) @def) (method_declaration name: (field_identifier) @def) "
           "(type_spec name: (type_identifier) @def) (import_spec path: (interpreted_string_literal) @import)"),
}
QUERIES["typescript"] = QUERIES["javascript"].replace("(class_declaration name: (identifier) @def)",
                                                      "(class_declaration name: (type_identifier) @def) "
                                                      "(interface_declaration name: (type_identifier) @def)")
SUFFIX = {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".go": "go"}
RESOLVE_EXT = ["", ".js", ".ts", ".tsx", ".jsx", "/index.js", "/index.ts"]


@cache
def _language(kind: str):
    try:
        import tree_sitter as ts
        if kind == "javascript":
            import tree_sitter_javascript as mod
            return ts.Language(mod.language())
        if kind in ("typescript", "tsx"):
            import tree_sitter_typescript as mod
            return ts.Language(mod.language_tsx() if kind == "tsx" else mod.language_typescript())
        if kind == "go":
            import tree_sitter_go as mod
            return ts.Language(mod.language())
    except ImportError:
        return None
    return None


def available() -> bool:
    return _language("javascript") is not None


def parse(root: Path, path: Path) -> tuple[list[str], list[str]] | None:
    """(symbols, imports) or None when this file type or tree-sitter is not available.
    Relative JS/TS imports are resolved to repository paths so they become graph edges."""
    kind = SUFFIX.get(path.suffix)
    lang = _language(kind) if kind else None
    if lang is None:
        return None
    import tree_sitter as ts
    tree = ts.Parser(lang).parse(path.read_bytes())
    caps = ts.QueryCursor(ts.Query(lang, QUERIES["typescript" if kind == "tsx" else kind])).captures(tree.root_node)
    in_order = lambda name: sorted(caps.get(name, []), key=lambda n: n.start_byte)    # captures come grouped by pattern  # noqa: E731
    symbols = list(dict.fromkeys(n.text.decode(errors="replace") for n in in_order("def")))
    imports = []
    rel_dir = posixpath.dirname(path.relative_to(root).as_posix())
    for n in in_order("import"):
        raw = n.text.decode(errors="replace").strip("\"'`")
        if raw.startswith("."):
            base = posixpath.normpath(posixpath.join(rel_dir, raw))
            hit = next((base + ext for ext in RESOLVE_EXT if (root / (base + ext)).is_file()), None)
            imports.append(hit or base)
        else:
            imports.append(raw)
    return symbols, imports
