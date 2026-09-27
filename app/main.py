"""HTTP app: GitHub webhook receiver, HTML dashboard, JSON API, action buttons, and the worker.

Run:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

Security: /webhook/github is authenticated by its HMAC signature. Everything else (dashboard,
JSON, and the buttons that start Devin sessions) requires HTTP Basic auth when DASHBOARD_PASSWORD
is set. Set it whenever you expose the port through a tunnel.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Settings
from .dashboard import metrics_json, render_dashboard, render_task
from .db import Store
from .devin_client import make_client
from .github_client import LABEL_COLORS, make_github
from .guards import check_safety
from .orchestrator import Orchestrator
from .states import HUMAN_NEEDED, State
from .webhook import handle_event, verify_signature

log = logging.getLogger("app")

Handler = Callable[[Request], Awaitable[Response]]


def create_app(
    settings: Settings | None = None, devin: Any = None, store: Store | None = None, github: Any = None
) -> Starlette:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = store or Store(settings.db_path)
    check_safety(store, settings)
    devin = devin or make_client(settings)
    github = github or make_github(settings.github_token, settings.github_api_base)
    orch = Orchestrator(store, devin, settings, github=github)

    def protected(fn: Handler) -> Handler:
        async def wrapper(request: Request) -> Response:
            if settings.dashboard_password and not _authorized(request, settings.dashboard_password):
                return PlainTextResponse(
                    "Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="superset-helper-eng"'}
                )
            return await fn(request)

        return wrapper

    # ---- public ------------------------------------------------------------------
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def webhook(request: Request) -> JSONResponse:
        if not settings.github_webhook_secret:
            return JSONResponse({"error": "GITHUB_WEBHOOK_SECRET is not configured"}, status_code=503)
        body = await request.body()
        if not verify_signature(settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        result = await asyncio.to_thread(
            handle_event,
            request.headers.get("x-github-event", ""),
            payload,
            request.headers.get("x-github-delivery"),
            orch,
            settings,
        )
        log.info("webhook %s -> %s", request.headers.get("x-github-event"), result)
        return JSONResponse(result)

    # ---- protected ---------------------------------------------------------------
    async def index(request: Request) -> HTMLResponse:
        page = await asyncio.to_thread(
            render_dashboard, orch, request.query_params.get("window", "7d"), request.query_params.get("msg", "")
        )
        return HTMLResponse(page)

    async def task_page(request: Request) -> Response:
        page = await asyncio.to_thread(render_task, orch, int(request.path_params["issue"]))
        return HTMLResponse(page) if page else PlainTextResponse("No such task", status_code=404)

    async def api_status(request: Request) -> JSONResponse:
        summary = await asyncio.to_thread(orch.status_summary)
        tasks = await asyncio.to_thread(store.list_tasks)
        summary["needs_human"] = [_brief(t) for t in tasks if State(t["state"]) in HUMAN_NEEDED]
        return JSONResponse(summary)

    async def api_tasks(request: Request) -> JSONResponse:
        state = request.query_params.get("state")
        try:
            tasks = await asyncio.to_thread(store.list_tasks, State(state) if state else None)
        except ValueError:
            return JSONResponse({"error": f"unknown state {state!r}"}, status_code=400)
        return JSONResponse({"tasks": [_brief(t) for t in tasks]})

    async def api_metrics(request: Request) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(metrics_json, orch, request.query_params.get("window", "all")))

    async def api_jobs(request: Request) -> JSONResponse:
        jobs = await asyncio.to_thread(store.list_jobs, request.query_params.get("kind"), 20)
        return JSONResponse({"jobs": [{k: v for k, v in j.items() if k != "result_json"} for j in jobs]})

    def _same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return not origin or urlparse(origin).netloc == request.headers.get("host")

    def _job_action(kind: str) -> Handler:
        async def run(request: Request) -> Response:
            if not _same_origin(request):
                return PlainTextResponse("cross-origin request refused", status_code=403)
            fn = {"scan": orch.start_scan, "learn": orch.start_learn, "sync": orch.import_from_github}[kind]
            result = await asyncio.to_thread(fn)
            if request.url.path.startswith("/api/"):
                return JSONResponse(result)
            return RedirectResponse(f"/?msg={quote(_flash(kind, result))}", status_code=303)

        return run

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        tasks = []
        if settings.run_worker:
            tasks.append(asyncio.create_task(_worker_loop(orch, settings.poll_interval_seconds)))
        if not github.dry_run:
            tasks.append(asyncio.create_task(asyncio.to_thread(_startup, github, orch, settings.target_repo)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/webhook/github", webhook, methods=["POST"]),
            Route("/", protected(index)),
            Route("/tasks/{issue:int}", protected(task_page)),
            Route("/api/status", protected(api_status)),
            Route("/api/tasks", protected(api_tasks)),
            Route("/api/metrics", protected(api_metrics)),
            Route("/api/jobs", protected(api_jobs)),
            Route("/actions/scan", protected(_job_action("scan")), methods=["POST"]),
            Route("/actions/learn", protected(_job_action("learn")), methods=["POST"]),
            Route("/actions/sync", protected(_job_action("sync")), methods=["POST"]),
            Route("/api/scan", protected(_job_action("scan")), methods=["POST"]),
            Route("/api/learn", protected(_job_action("learn")), methods=["POST"]),
            Route("/api/sync", protected(_job_action("sync")), methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.orchestrator = orch
    app.state.store = store
    return app


def _authorized(request: Request, password: str) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:]).decode().partition(":")[2]
    except Exception:
        return False
    return hmac.compare_digest(supplied, password)


def _flash(kind: str, result: dict[str, Any]) -> str:
    status = result.get("status")
    if kind == "sync":
        if status == "dry_run":
            return "GitHub sync is in dry-run mode (no GITHUB_TOKEN), so there is nothing to import."
        if status == "error":
            return f"Could not read issues from GitHub: {'; '.join(result.get('errors', []))}"
        return (
            f"Imported {result.get('proposed', 0)} proposed and {result.get('ready', 0)} ready issue(s); "
            f"recovered {result.get('recovered', 0)} merged fix(es) from history "
            f"({result.get('seen', 0)} labeled issue(s) checked)."
        )
    if status == "started":
        return f"{kind} started (session {result.get('url', '')}). Results appear here when Devin finishes."
    if status == "already_running":
        return f"A {kind} is already running."
    if status == "limit_reached":
        return f"Daily session limit ({result.get('limit')}) reached; try again later."
    if status == "nothing_new":
        return "Nothing new to learn from since the last run."
    return f"{kind} could not start: {result.get('detail', status)}"


def _brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue": t["issue_number"],
        "title": t["title"],
        "state": t["state"],
        "attempt": t["attempt"],
        "session": t["current_session_id"],
        "pr_urls": t["pr_urls"],
        "verify_status": t["verify_status"] or None,
        "question": t["blocked_question"] or None,
        "summary": t["last_summary"] or None,
    }


def _startup(github: Any, orch: Orchestrator, repo: str) -> None:
    """Once, at startup with a real token: make sure the labels exist, then pick up any labeled
    issues the bot missed while it was down."""
    _ensure_labels(github, repo)
    try:
        log.info("startup import from GitHub: %s", orch.import_from_github())
    except Exception:
        log.exception("startup import failed")


def _ensure_labels(github: Any, repo: str) -> None:
    for name, (color, desc) in LABEL_COLORS.items():
        try:
            github.ensure_label(repo, name, color, desc)
        except Exception as e:  # best effort: never block startup
            log.warning("could not ensure label %s: %s", name, e)
            return


async def _worker_loop(orch: Orchestrator, interval: int) -> None:
    log.info("worker started (poll every %ss)", interval)
    while True:
        try:
            await asyncio.to_thread(orch.tick)
        except Exception:  # keep the loop alive no matter what
            log.exception("worker tick failed")
        await asyncio.sleep(interval)
