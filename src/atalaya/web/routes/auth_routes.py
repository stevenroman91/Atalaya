"""Rutas de autenticación: login, logout, invitación, reset."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from atalaya.web import auth as auth_layer
from atalaya.web.deps import SESSION_COOKIE, get_db, optional_user, render

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _set_session_cookie(resp: RedirectResponse, token: str) -> None:
    secure = not bool(__import__("os").environ.get("ATALAYA_INSECURE_COOKIES"))
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure,
                    samesite="lax", path="/")


@router.get("/login")
def login_form(request: Request, user_sess=Depends(optional_user)):
    if user_sess:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    try:
        user = auth_layer.login(db, email, password, _client_ip(request))
    except auth_layer.AuthError as exc:
        return render(request, "login.html", error=exc.code)
    token, _ = auth_layer.create_session(db, user)
    target = "/account/onboarding" if not user.onboarded else "/dashboard"
    resp = RedirectResponse(target, status_code=303)
    _set_session_cookie(resp, token)
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    auth_layer.revoke_session(db, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/auth/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ── Invitación (enlace de uso único) ────────────────────────────────────────

@router.get("/invite/{token}")
def invite_form(request: Request, token: str, db: Session = Depends(get_db)):
    inv = auth_layer.get_valid_invitation(db, token)
    if not inv:
        return render(request, "invite.html", invalid=True, token=None, email=None)
    return render(request, "invite.html", invalid=False, token=token, email=inv.email)


@router.post("/invite/{token}")
def invite_accept(request: Request, token: str, password: str = Form(...),
                  password2: str = Form(...), db: Session = Depends(get_db)):
    if password != password2:
        inv = auth_layer.get_valid_invitation(db, token)
        return render(request, "invite.html", invalid=inv is None, token=token,
                      email=inv.email if inv else None, error="password_mismatch")
    try:
        user = auth_layer.accept_invitation(db, token, password)
    except auth_layer.AuthError:
        return render(request, "invite.html", invalid=True, token=None, email=None)
    tok, _ = auth_layer.create_session(db, user)
    resp = RedirectResponse("/account/onboarding", status_code=303)
    _set_session_cookie(resp, tok)
    return resp


# ── Reset de contraseña ──────────────────────────────────────────────────────

@router.get("/reset")
def reset_request_form(request: Request):
    return render(request, "reset_request.html", sent=False)


@router.post("/reset")
def reset_request(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    token = auth_layer.create_password_reset(db, email)
    if token:
        # Sin SMTP configurado, el enlace se registra en el log del servidor
        # para que el admin lo transmita. (SMTP: ver README.)
        import os
        base = os.environ.get("ATALAYA_BASE_URL", "http://localhost:8000")
        log.info("Enlace de reset para %s: %s/auth/reset/%s", email, base, token)
    return render(request, "reset_request.html", sent=True)


@router.get("/reset/{token}")
def reset_form(request: Request, token: str):
    return render(request, "reset_form.html", token=token, error=None)


@router.post("/reset/{token}")
def reset_apply(request: Request, token: str, password: str = Form(...),
                password2: str = Form(...), db: Session = Depends(get_db)):
    if password != password2:
        return render(request, "reset_form.html", token=token, error="password_mismatch")
    try:
        auth_layer.use_password_reset(db, token, password)
    except auth_layer.AuthError as exc:
        return render(request, "reset_form.html", token=token, error=exc.code)
    return RedirectResponse("/auth/login", status_code=303)
