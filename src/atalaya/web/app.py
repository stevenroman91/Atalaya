"""Aplicación web de Atalaya (FastAPI)."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from atalaya.web.routes import (
    account, admin_routes, auth_routes, coverage, dashboard, weekly_monthly,
)

log = logging.getLogger(__name__)


def _mark_interrupted_manual_runs() -> None:
    """Las colectas manuales corren en un hilo de ESTE proceso: si el proceso
    murió (redeploy, crash), su run queda huérfano con finished_at NULL y el
    botón de admin aparecería bloqueado. Al arrancar se marcan como
    interrumpidas. Los runs de cron viven en otros contenedores y no se tocan.
    """
    from sqlalchemy import select

    from atalaya.db import SessionLocal
    from atalaya.db.models import CollectRun, utcnow
    try:
        with SessionLocal() as db:
            orphans = db.scalars(
                select(CollectRun).where(CollectRun.finished_at.is_(None))).all()
            for run in orphans:
                origin = (run.stats or {}).get("origin")
                # "manual": lanzado por un hilo de este proceso → huérfano seguro.
                # None: run heredado de una versión sin marcado de origen →
                # se limpia también (transición puntual; el código actual
                # siempre marca el origen).
                if origin == "manual" or origin is None:
                    run.finished_at = utcnow()
                    run.ok = False
                    run.stats = {**(run.stats or {}), "interrupted": True}
            db.commit()
    except Exception:  # p. ej. base aún sin migrar — no impedir el arranque
        log.warning("no se pudieron limpiar runs manuales huérfanos", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _mark_interrupted_manual_runs()
    yield


app = FastAPI(title="Atalaya", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)

_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com; "
    "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if os.environ.get("ATALAYA_HSTS", "1") == "1":
            resp.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
        return resp


app.add_middleware(SecurityHeaders)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(weekly_monthly.router)
app.include_router(account.router)
app.include_router(admin_routes.router)
app.include_router(coverage.router)


@app.get("/")
def index():
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
