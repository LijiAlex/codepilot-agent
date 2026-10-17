# src/codepilot/gh/auth.py
"""Repo-scoped GitHub App installation tokens.

PyGithub's get_access_token sends only `permissions` (verified), so the token
would cover every repo of the installation. We POST ourselves with
`repositories=[name]` + minimum permissions, and refresh 5 minutes before expiry.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from github import Auth, Github, GithubIntegration

API = "https://api.github.com"
PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"}


class TokenProvider:
    def __init__(self, app_id: str, private_key: str, owner: str, name: str, *,
                 permissions: dict | None = None, http=httpx, installation_id: int | None = None):
        self.owner, self.name = owner, name
        self.permissions = permissions or PERMISSIONS
        self._auth = Auth.AppAuth(app_id, private_key)
        self._http = http
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, app_id_env: str, key_path_env: str, owner: str, name: str) -> "TokenProvider":
        # The App key file is CodePilot's own runtime credential; it is never shown to an agent.
        return cls(os.environ[app_id_env], Path(os.environ[key_path_env]).read_text(), owner, name)

    @property
    def installation_id(self) -> int:
        if self._installation_id is None:
            gi = GithubIntegration(auth=self._auth)
            self._installation_id = gi.get_repo_installation(self.owner, self.name).id
        return self._installation_id

    def token(self) -> str:
        with self._lock:
            if self._token and self._expires - datetime.now(timezone.utc) > timedelta(minutes=5):
                return self._token
            r = self._http.post(
                f"{API}/app/installations/{self.installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._auth.create_jwt()}",
                         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                json={"repositories": [self.name], "permissions": self.permissions}, timeout=15)
            if r.status_code != 201:
                # GitHub names the missing permission in this header (pre-mortem E5); never echo the body's token fields.
                raise PermissionError(f"token request failed: HTTP {r.status_code}; "
                                      f"accepted permissions: {r.headers.get('X-Accepted-GitHub-Permissions', '?')}")
            data = r.json()
            self._token = data["token"]
            self._expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            return self._token

    def repo(self):
        """A fresh client per call, so a long-lived caller never holds an expired token."""
        return Github(auth=Auth.Token(self.token())).get_repo(f"{self.owner}/{self.name}", lazy=True)
