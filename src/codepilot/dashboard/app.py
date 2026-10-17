# src/codepilot/dashboard/app.py
"""Read-mostly dashboard (Plus): FastAPI on 127.0.0.1 only. Token in a header (so cross-site requests cannot carry it),
Host and Origin checks, strict CSP with no third-party scripts, and merges only of PRs the independent reviewer marked
merge-ready at the current commit."""
from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from codepilot.dashboard import data

STATIC = Path(__file__).parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@dataclass
class DashboardDeps:
    conn: sqlite3.Connection
    runs_dir: Path
    results_dir: Path
    repos: list[str]
    repo_factory: Callable[[str], Any]
    audit: Any
    reviewer_login: str | None = None
    lessons: Any = None
    episodic: Any = None
    skills: dict | None = None
    improvements: Any = None                    # Task 30: ImprovementStore


class PRAction(BaseModel):
    expected_head: str
    confirm: bool


class Confirm(BaseModel):
    confirm: bool


def create_app(deps: DashboardDeps, token: str, port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in hosts:                       # DNS-rebinding defence
            return JSONResponse({"error": "bad host"}, status_code=400)
        if request.url.path.startswith("/api/"):
            if not hmac.compare_digest(request.headers.get("x-codepilot-token", ""), token):
                return JSONResponse({"error": "missing or wrong token"}, status_code=401)
            if request.method != "GET" and request.headers.get("origin") not in origins:
                return JSONResponse({"error": "bad origin"}, status_code=403)
        resp = await call_next(request)
        resp.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        return resp

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/overview")
    def overview():
        return data.overview(deps.conn)

    @app.get("/api/matrix")
    def matrix():
        return data.matrix(deps.conn, deps.runs_dir)

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        d = data.run_detail(deps.conn, deps.runs_dir, run_id)
        if d is None:
            raise HTTPException(404, "no such run")
        return d

    @app.get("/api/prs")
    def prs():
        out = {}
        for name in deps.repos:
            try:
                out[name] = data.pull_requests(deps.repo_factory(name), deps.reviewer_login)
            except Exception as e:                                         # noqa: BLE001 — show, don't crash the page
                out[name] = {"error": type(e).__name__}
        return out

    def _pr_action(repo: str, number: int, body: PRAction, action: str):
        if repo not in deps.repos:
            raise HTTPException(404, "unknown repository")
        if not body.confirm:
            raise HTTPException(400, "confirmation required")
        gh = deps.repo_factory(repo)
        try:
            res = data.merge_pr(gh, number, body.expected_head, deps.reviewer_login) if action == "merge" \
                else data.close_pr(gh, number, body.expected_head)
        except data.MergeRefused as e:
            deps.audit.log("human:dashboard", f"pr_{action}_refused", target=f"{repo}#{number}", result="refused", detail={"why": str(e)})
            raise HTTPException(409, str(e))
        except Exception as e:                                             # noqa: BLE001 — e.g. GitHub 405 "not mergeable"
            deps.audit.log("human:dashboard", f"pr_{action}_failed", target=f"{repo}#{number}", result="error",
                           detail={"type": type(e).__name__})
            raise HTTPException(409, f"GitHub refused: {type(e).__name__}")
        deps.audit.log("human:dashboard", f"pr_{action}", target=f"{repo}#{number}", detail=res)
        return res

    @app.post("/api/prs/{owner}/{name}/{number}/merge")
    def merge(owner: str, name: str, number: int, body: PRAction):
        return _pr_action(f"{owner}/{name}", number, body, "merge")

    @app.post("/api/prs/{owner}/{name}/{number}/close")
    def close(owner: str, name: str, number: int, body: PRAction):
        return _pr_action(f"{owner}/{name}", number, body, "close")

    @app.get("/api/safety")
    def safety():
        return data.safety(deps.conn, deps.results_dir)

    @app.get("/api/context")
    def context():
        return data.context(deps.conn, deps.runs_dir)

    @app.get("/api/memory")
    def memory():
        return data.memory(deps.lessons, deps.episodic, deps.repos)

    @app.get("/api/skills")
    def skills():
        out = data.skills(deps.skills or {})
        if deps.improvements is not None:
            out["repo_skills"] = {r: deps.improvements.skill_history(r) for r in deps.repos}
        return out

    @app.get("/api/audit")
    def audit():
        return data.audit(deps.conn)

    @app.get("/api/improvements")
    def improvements():
        if deps.improvements is None:
            return {"proposals": [], "reports": [], "trend": []}
        return deps.improvements.view(deps.repos)

    @app.post("/api/improvements/{proposal_id}/{decision}")
    def decide_improvement(proposal_id: str, decision: str, body: Confirm):
        if deps.improvements is None or decision not in ("approve", "reject") or not body.confirm:
            raise HTTPException(400, "not available")
        res = deps.improvements.decide(proposal_id, decision, "dashboard")
        deps.audit.log("human:dashboard", f"improvement_{decision}", target=proposal_id, detail=res)
        return res

    return app
