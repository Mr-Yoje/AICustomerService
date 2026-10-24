from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from cagent import settings
from cagent.trace import accept_trace_id, pop_trace_id, push_trace_id

from . import routes_chat, routes_config, routes_data
from .service import Service

log = logging.getLogger("cagent")


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from cagent.trace import current_trace_id
        record.trace_id = current_trace_id() or "-"
        return True


def install_trace_logging() -> None:
    logger = logging.getLogger("cagent")
    logger.setLevel(logging.INFO)
    if not any(isinstance(item, _TraceFilter) for item in logger.filters):
        logger.addFilter(_TraceFilter())
    if any(getattr(handler, "_cagent_trace", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s"))
    handler.addFilter(_TraceFilter())
    handler._cagent_trace = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.propagate = False


class TraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = dict(scope.get("headers") or []).get(b"x-trace-id", b"").decode("latin-1")
        trace_id = accept_trace_id(raw)
        token = push_trace_id(trace_id)
        start = time.perf_counter()
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-trace-id", trace_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            path = scope.get("path") or ""
            if path.startswith("/api"):
                ms = int((time.perf_counter() - start) * 1000)
                log.info("%s %s %s %dms", scope.get("method"), path, status, ms)
            pop_trace_id(token)

DEFAULT_WEB_DIST = settings.REPO_DIR.parent / "AICustomerService-web" / "dist"


def _mount_web(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            return FileResponse(target)
        return FileResponse(root / "index.html")


def create_app(service: Service | None = None, web_dist: Path | None = None) -> FastAPI:
    install_trace_logging()
    app = FastAPI(title="金融催收对话引擎", version="1.0.0")
    app.add_middleware(TraceMiddleware)
    app.state.service = service or Service.from_settings()
    app.include_router(routes_chat.router)
    app.include_router(routes_data.router)
    app.include_router(routes_config.router)
    dist = Path(web_dist or os.getenv("WEB_DIST") or DEFAULT_WEB_DIST)
    if (dist / "index.html").is_file():
        _mount_web(app, dist)
    return app
