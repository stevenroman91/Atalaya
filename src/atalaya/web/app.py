"""Aplicación web de Atalaya (FastAPI)."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from atalaya.web.routes import account, admin_routes, auth_routes, dashboard, weekly_monthly

app = FastAPI(title="Atalaya", docs_url=None, redoc_url=None, openapi_url=None)

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


@app.get("/")
def index():
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
