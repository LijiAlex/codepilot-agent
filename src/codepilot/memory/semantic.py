# src/codepilot/memory/semantic.py
"""Semantic memory in ChromaDB: lessons (keyed by repository + issue type) and the
file-chunk index behind semantic_search. We pass embeddings ourselves
(collections use embedding_function=None, verified), so tests run offline and the
real embedder (Chroma's default ONNX MiniLM) is downloaded once, at setup."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Literal, Sequence

import chromadb
from chromadb.config import Settings

Embedder = Callable[[list[str]], Sequence[Sequence[float]]]
TEXT_SUFFIXES = {".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json"}
CHUNK_CHARS = 800                                   # ≈200 tokens


def default_embedder() -> Embedder:
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    ef = DefaultEmbeddingFunction()
    return lambda texts: [list(map(float, v)) for v in ef(texts)]


def _client(path: Path):
    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def _collection_name(prefix: str, repo: str) -> str:
    return f"{prefix}_{re.sub(r'[^A-Za-z0-9_-]', '_', repo)}"[:60]


class LessonStore:
    def __init__(self, path: Path, embedder: Embedder):
        self.col = _client(path).get_or_create_collection("lessons", embedding_function=None, metadata={"hnsw:space": "cosine"})
        self.embed = embedder
        self._lock = threading.Lock()

    def add(self, repo: str, task_type: str, text: str, source: Literal["merged", "failure", "rejection"], run_id: str) -> str:
        lid = hashlib.sha1(f"{repo}|{task_type}|{text}".encode()).hexdigest()[:16]   # same lesson twice = one row
        with self._lock:
            self.col.upsert(ids=[lid], embeddings=list(self.embed([text])), documents=[text],
                            metadatas=[{"repo": repo, "task_type": task_type, "source": source, "run_id": run_id, "ts": time.time()}])
        return lid

    def top(self, repo: str, task_type: str, query: str, k: int = 3) -> list[str]:
        where = {"$and": [{"repo": repo}, {"task_type": task_type}]}
        if not self.col.get(where=where, limit=1)["ids"]:
            return []
        r = self.col.query(query_embeddings=list(self.embed([query])), n_results=k, where=where)
        return r["documents"][0]

    def all(self, repo: str) -> list[dict]:
        g = self.col.get(where={"repo": repo})
        return [dict(m, text=d, id=i) for i, d, m in zip(g["ids"], g["documents"], g["metadatas"])]


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    lines, out, cur = text.splitlines(keepends=True), [], ""
    for line in lines:
        if len(cur) + len(line) > size and cur:
            out.append(cur); cur = ""
        cur += line[: size * 2]
    if cur.strip():
        out.append(cur)
    return out


class FileIndex:
    """Embeddings of file chunks for one repo, built from the host mirror at the map's SHA;
    on a new SHA only changed files are re-embedded (spec E15)."""

    def __init__(self, path: Path, repo: str, embedder: Embedder):
        self.col = _client(path).get_or_create_collection(_collection_name("files", repo), embedding_function=None,
                                                          metadata={"hnsw:space": "cosine"})
        self.state = Path(path) / f"{_collection_name('files', repo)}.json"
        self.embed = embedder
        self._lock = threading.Lock()

    @property
    def sha(self) -> str | None:
        return json.loads(self.state.read_text())["sha"] if self.state.exists() else None

    def _index_file(self, root: Path, rel: str) -> int:
        self.col.delete(where={"path": rel})
        p = root / rel
        if not p.is_file() or p.suffix not in TEXT_SUFFIXES or p.stat().st_size > 200_000:
            return 0
        chunks = chunk(p.read_text(errors="replace"))
        if not chunks:
            return 0
        self.col.upsert(ids=[f"{rel}#{i}" for i in range(len(chunks))], embeddings=list(self.embed(chunks)),
                        documents=chunks, metadatas=[{"path": rel, "part": i} for i in range(len(chunks))])
        return len(chunks)

    def build(self, root: Path, files: list[str], sha: str) -> int:
        with self._lock:
            n = sum(self._index_file(Path(root), f) for f in files)
            self.state.write_text(json.dumps({"sha": sha}))
            return n

    def refresh(self, root: Path, changed: list[str], sha: str) -> int:
        with self._lock:
            n = sum(self._index_file(Path(root), f) for f in changed)
            self.state.write_text(json.dumps({"sha": sha}))
            return n

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        if self.col.count() == 0:
            return []
        r = self.col.query(query_embeddings=list(self.embed([query])), n_results=min(k * 4, self.col.count()))
        best: dict[str, float] = {}
        for meta, dist in zip(r["metadatas"][0], r["distances"][0]):
            best[meta["path"]] = max(best.get(meta["path"], 0.0), 1.0 - dist)
        return sorted(best.items(), key=lambda kv: -kv[1])[:k]
