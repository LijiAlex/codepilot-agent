# src/codepilot/repomap/build.py
"""Repository map: file tree + per-file summary + symbols, ranked to fit a token budget.

Ranking follows Aider's approach (reference graph + PageRank personalised to the
issue), using the stdlib `ast` module for Python; JavaScript, TypeScript and Go use
tree-sitter when the optional `maps` extra is installed (repomap/treesitter.py).
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

LANG = {".py": "python", ".md": "markdown", ".toml": "toml", ".yml": "yaml", ".yaml": "yaml",
        ".json": "json", ".ini": "ini", ".cfg": "ini", ".txt": "text", ".sh": "shell", ".js": "javascript", ".jsx": "javascript",
        ".mjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


@dataclass
class FileEntry:
    path: str
    language: str
    summary: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


@dataclass
class RepoMap:
    text: str
    files: list[FileEntry]
    tokens: int
    sha: str


def estimate_tokens(text: str) -> int:
    return len(text) // 4          # ~4 characters per token for English and code


def _first_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:100]
    return ""


def parse_file(root: Path, path: Path) -> FileEntry:
    rel = path.relative_to(root).as_posix()
    lang = LANG.get(path.suffix, "other")
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return FileEntry(rel, lang, "unreadable")
    if lang in ("javascript", "typescript", "go"):
        try:
            from codepilot.repomap import treesitter      # added in Task 32; optional `maps` extra
        except ImportError:
            treesitter = None
        parsed = treesitter.parse(root, path) if treesitter else None
        if parsed is not None:
            return FileEntry(rel, lang, _first_line(text) or f"{lang} module", parsed[0], parsed[1])
    if lang != "python":
        return FileEntry(rel, lang, _first_line(text) or f"{lang} file")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return FileEntry(rel, lang, _first_line(text) or "python file (does not parse)")
    doc = ast.get_docstring(tree)
    symbols = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    imports: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            imports.append(n.module)
        elif isinstance(n, ast.Import):
            imports.extend(a.name for a in n.names)
    summary = (doc.splitlines()[0][:100] if doc else "") or _first_line(text) or "python module"
    return FileEntry(rel, lang, summary, symbols, imports)


def scan(root: Path) -> list[FileEntry]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            out.append(parse_file(root, p))
    return out


def _pagerank(entries: list[FileEntry], personal: dict[str, float], iters: int = 30, d: float = 0.85) -> dict[str, float]:
    by_module: dict[str, str] = {}
    for e in entries:
        if e.path.endswith(".py"):
            mod = e.path[:-3].replace("/", ".").removesuffix(".__init__")
            by_module[mod] = e.path
            if mod.startswith("src."):                  # src/ layout: imports omit the src. prefix
                by_module[mod[4:]] = e.path
    paths = {e.path for e in entries}
    edges: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        for imp in e.imports:
            target = by_module.get(imp) or (imp if imp in paths else None)     # JS/TS imports are resolved paths
            if target and target != e.path:
                edges[e.path].add(target)
    nodes = [e.path for e in entries]
    total = sum(personal.get(n, 1.0) for n in nodes) or 1.0      # over every node: a partial dict must still sum to 1
    p = {n: personal.get(n, 1.0) / total for n in nodes}
    rank = dict(p)
    for _ in range(iters):
        new = {n: (1 - d) * p[n] for n in nodes}
        dangling = 0.0                                  # files with no outgoing edges spread their rank by p, once per round
        for src in nodes:
            outs = edges.get(src)
            if outs:
                share = d * rank[src] / len(outs)
                for t in outs:
                    new[t] += share
            else:
                dangling += rank[src]
        for n in nodes:
            new[n] += d * dangling * p[n]
        rank = new
    return rank


def _tree(paths: list[str], depth: int = 2) -> str:
    dirs = sorted({"/".join(p.split("/")[:i]) + "/" for p in paths for i in range(1, min(depth, p.count("/")) + 1)})
    return "\n".join(dirs)


def _line(e: FileEntry) -> str:
    syms = [s for s in e.symbols if not s.startswith("_")][:8]
    return f"{e.path} [{e.language}]: {e.summary}" + (f" — {', '.join(syms)}" if syms else "")


def build_repo_map(root: Path, sha: str, budget_tokens: int = 4000, focus_terms=(), focus_files=(),
                   describe: Callable[[str, str], str] | None = None, entries: list[FileEntry] | None = None) -> RepoMap:
    root = Path(root)
    entries = entries if entries is not None else scan(root)
    terms = [t.lower() for t in focus_terms]
    personal: dict[str, float] = {}
    for e in entries:
        w = 1.0
        if e.path in focus_files:
            w *= 10
        if terms and any(t in e.path.lower() or any(t in s.lower() for s in e.symbols) for t in terms):
            w *= 10
        if e.symbols and all(s.startswith("_") for s in e.symbols):
            w *= 0.1
        personal[e.path] = w
    rank = _pagerank(entries, personal)
    ordered = sorted(entries, key=lambda e: rank[e.path], reverse=True)
    if describe:
        for e in ordered[:20]:
            if e.summary in ("python module", "") or e.summary.endswith(" file"):
                e.summary = describe(e.path, (root / e.path).read_text(errors="replace")[:4000])[:100]
    header = f"# Repo map @ {sha[:8]}\n## Directories\n{_tree([e.path for e in entries])}\n## Files (most relevant first)\n"
    lo, hi = 0, len(ordered)
    while lo < hi:                       # largest prefix of ranked files that fits the budget
        mid = (lo + hi + 1) // 2
        if estimate_tokens(header + "\n".join(_line(e) for e in ordered[:mid])) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    shown = ordered[:lo]
    omitted = len(ordered) - lo
    text = header + "\n".join(_line(e) for e in shown) + (f"\n… {omitted} more files not shown" if omitted else "")
    if estimate_tokens(text) > budget_tokens and shown:
        shown = shown[:-1]
        text = header + "\n".join(_line(e) for e in shown) + f"\n… {len(ordered) - len(shown)} more files not shown"
    return RepoMap(text=text, files=entries, tokens=estimate_tokens(text), sha=sha)
