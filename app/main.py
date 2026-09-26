"""HTTP app: GitHub webhook receiver, a small JSON API, and the background worker.

Run:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
(The M4 milestone adds the HTML dashboard on top of /api/status and /api/tasks.)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .config import Settings
from .db import Store
from .devin_client import make_client
from .orchestrator import Orchestrator
from .states import HUMAN_NEEDED, State
from .webhook import handle_event, verify_signature

log = logging.getLogger("app")


def create_app(settings: Settings | None = None, devin: Any = None, store: Store | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = store or Store(settings.db_path)
    devin = devin or make_client(settings)
    orch = Orchestrator(store, devin, settings)

    async def index(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "superset-helper-eng is running.\n"
            f"target repo: {settings.target_repo}  mode: {settings.devin_mode}\n"
            "GET /api/status   GET /api/tasks   POST /webhook/github   GET /healthz\n"
        )

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

    async def api_status(request: Request) -> JSONResponse:
        summary = await asyncio.to_thread(orch.status_summary)
        tasks = await asyncio.to_thread(store.list_tasks)
        summary["needs_human"] = [
            _brief(t) for t in tasks if State(t["state"]) in HUMAN_NEEDED
        ]
        return JSONResponse(summary)

    async def api_tasks(request: Request) -> JSONResponse:
        state = request.query_params.get("state")
        try:
            tasks = await asyncio.to_thread(store.list_tasks, State(state) if state else None)
        except ValueError:
            return JSONResponse({"error": f"unknown state {state!r}"}, status_code=400)
        return JSONResponse({"tasks": [_brief(t) for t in tasks]})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        worker = asyncio.create_task(_worker_loop(orch, settings.poll_interval_seconds)) if settings.run_worker else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Route("/webhook/github", webhook, methods=["POST"]),
            Route("/api/status", api_status),
            Route("/api/tasks", api_tasks),
        ],
        lifespan=lifespan,
    )
    app.state.orchestrator = orch
    app.state.store = store
    return app


def _brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue": t["issue_number"],
        "title": t["title"],
        "state": t["state"],
        "attempt": t["attempt"],
        "session": t["current_session_id"],
        "pr_urls": t["pr_urls"],
        "question": t["blocked_question"] or None,
        "summary": t["last_summary"] or None,
    }


async def _worker_loop(orch: Orchestrator, interval: int) -> None:
    log.info("worker started (poll every %ss)", interval)
    while True:
        try:
            await asyncio.to_thread(orch.tick)
        except Exception:  # keep the loop alive no matter what
            log.exception("worker tick failed")
        await asyncio.sleep(interval)
