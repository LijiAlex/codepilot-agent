# src/codepilot/sandbox/prefixed.py
"""Present a folder inside a remote sandbox as the root of a CompositeBackend route.

CompositeBackend strips the route prefix ("/sandbox/a.py" -> "/a.py"); remote
sandboxes such as Daytona use absolute box paths and have no root option. This
adapter adds the box folder on the way in and removes it from results on the way out.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from deepagents.backends.sandbox import BaseSandbox


class PrefixedSandbox(BaseSandbox):
    def __init__(self, inner: BaseSandbox, prefix: str):
        self.inner = inner
        self.prefix = prefix.rstrip("/")
        self._internal = 0      # >0 while a BaseSandbox method we already prefixed is running

    def _inside(self, fn, *args, **kwargs):
        self._internal += 1
        try:
            return fn(*args, **kwargs)
        finally:
            self._internal -= 1

    @property
    def id(self) -> str:
        return f"{self.inner.id}:{self.prefix}"

    def _in(self, path: str | None) -> str:
        p = path or "/"
        return self.prefix + (p if p.startswith("/") else "/" + p)

    def _out(self, value: Any) -> Any:
        if isinstance(value, str):
            return value[len(self.prefix):] or "/" if value.startswith(self.prefix) else value
        if isinstance(value, list):
            return [self._out(v) for v in value]
        if isinstance(value, dict):
            return {k: self._out(v) for k, v in value.items()}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.replace(value, **{f.name: self._out(getattr(value, f.name)) for f in dataclasses.fields(value)})
        return value

    def execute(self, command: str, *, timeout: int | None = None):
        return self.inner.execute(command, timeout=timeout)

    # write/edit call upload_files/download_files with paths we already prefixed,
    # so only outside callers (the CompositeBackend) get the prefix added here.
    def upload_files(self, files):
        if self._internal:
            return self.inner.upload_files(files)
        return self._out(self.inner.upload_files([(self._in(p), data) for p, data in files]))

    def download_files(self, paths):
        if self._internal:
            return self.inner.download_files(paths)
        return self._out(self.inner.download_files([self._in(p) for p in paths]))

    def ls(self, path):
        return self._out(self._inside(super().ls, self._in(path)))

    def read(self, file_path, offset=0, limit=2000):
        return self._out(self._inside(super().read, self._in(file_path), offset, limit))

    def write(self, file_path, content):
        return self._out(self._inside(super().write, self._in(file_path), content))

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return self._out(self._inside(super().edit, self._in(file_path), old_string, new_string, replace_all))

    def glob(self, pattern, path=None):
        return self._out(self._inside(super().glob, pattern, self._in(path)))

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return self._out(self._inside(super().grep, pattern, self._in(path), glob, max_count=max_count))

    def delete(self, file_path):
        return self._out(self._inside(super().delete, self._in(file_path)))


class CwdSandbox(BaseSandbox):
    """Default backend for a remote box: shell commands run inside the run folder."""
    def __init__(self, inner: BaseSandbox, cwd: str, preamble: str = ""):
        self.inner, self.cwd, self.preamble = inner, cwd.rstrip("/"), preamble

    @property
    def id(self) -> str:
        return self.inner.id

    def execute(self, command: str, *, timeout: int | None = None):
        import shlex
        pre = f"{self.preamble} && " if self.preamble else ""
        return self.inner.execute(f"cd {shlex.quote(self.cwd)} && {pre}{command}", timeout=timeout)

    def upload_files(self, files):
        from deepagents.backends.protocol import FileUploadResponse
        return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]     # default route is read-only

    def write(self, file_path, content):
        from deepagents.backends.protocol import WriteResult
        return WriteResult(error=f"permission denied: {file_path} is outside /sandbox/ and /shared/")

    def edit(self, file_path, old_string, new_string, replace_all=False):
        from deepagents.backends.protocol import EditResult
        return EditResult(error=f"permission denied: {file_path} is outside /sandbox/ and /shared/")

    def download_files(self, paths):
        return self.inner.download_files([f"{self.cwd}{p}" for p in paths])
