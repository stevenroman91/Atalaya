"""Dependencias FastAPI: sesión DB, usuario actual, CSRF, plantillas."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from atalaya.config import load_countries, zone_by_id
from atalaya.db import get_session
from atalaya.db.models import User, WebSession
from atalaya.web import auth as auth_layer
from atalaya.web.i18n import translator

SESSION_COOKIE = "atalaya_session"

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def get_db(db: Session = Depends(get_session)) -> Session:
    return db


def optional_user(request: Request, db: Session = Depends(get_session)) -> tuple[User, WebSession] | None:
    return auth_layer.get_session_user(db, request.cookies.get(SESSION_COOKIE))


def current_user(request: Request, db: Session = Depends(get_session)) -> tuple[User, WebSession]:
    result = auth_layer.get_session_user(db, request.cookies.get(SESSION_COOKIE))
    if not result:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER,
                            headers={"Location": "/auth/login"})
    return result


def require_admin(user_sess: tuple[User, WebSession] = Depends(current_user)) -> tuple[User, WebSession]:
    if user_sess[0].role != "admin":
        raise HTTPException(status_code=403, detail="solo admin")
    return user_sess


async def check_csrf(request: Request, user_sess: tuple[User, WebSession]) -> None:
    """Valida el token CSRF en POST (campo de formulario o cabecera)."""
    form = await request.form()
    token = form.get("csrf_token") or request.headers.get("x-csrf-token")
    if not token or token != user_sess[1].csrf_token:
        raise HTTPException(status_code=403, detail="CSRF token inválido")


def render(request: Request, template: str, user: User | None = None,
           csrf: str | None = None, **context):
    lang = user.lang if user else "es"
    t = translator(lang)
    return templates.TemplateResponse(request, template, {
        "t": t, "lang": lang, "user": user, "csrf_token": csrf or "",
        "countries": load_countries(), "zones": zone_by_id(), **context,
    })
