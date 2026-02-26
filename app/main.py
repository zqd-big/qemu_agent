from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router as api_router
from .settings import DATA_DIR, PROJECTS_DIR, WEB_DIR
from .utils import ensure_dir


def create_app() -> FastAPI:
    app = FastAPI(title="QEMU Device Model Generator MVP", version="0.1.0")
    ensure_dir(DATA_DIR)
    ensure_dir(PROJECTS_DIR)
    db.init_db()

    @app.on_event("startup")
    def _startup() -> None:
        ensure_dir(DATA_DIR)
        ensure_dir(PROJECTS_DIR)
        db.init_db()

    app.include_router(api_router)

    if WEB_DIR.exists():
        app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")
    else:
        @app.get("/", include_in_schema=False)
        def root_missing_ui() -> dict[str, str]:
            return {"message": "UI directory not found"}

    return app


app = create_app()

