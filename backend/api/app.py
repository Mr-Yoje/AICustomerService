from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from cagent import settings

from . import routes_chat, routes_config, routes_data
from .service import Service

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
    app = FastAPI(title="金融催收对话引擎", version="1.0.0")
    app.state.service = service or Service.from_settings()
    app.include_router(routes_chat.router)
    app.include_router(routes_data.router)
    app.include_router(routes_config.router)
    dist = Path(web_dist or os.getenv("WEB_DIST") or DEFAULT_WEB_DIST)
    if (dist / "index.html").is_file():
        _mount_web(app, dist)
    return app
