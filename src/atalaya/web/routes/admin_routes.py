"""Espace admin (§6.1): invitations, comptes, santé de la collecte."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from atalaya.config import load_schedule
from atalaya.db.models import CollectRun, Invitation, SourceRecord, User
from atalaya.web import auth as auth_layer
from atalaya.web.deps import check_csrf, get_db, render, require_admin

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(request: Request, invite_link: str | None = None, error: str | None = None,
               user_sess=Depends(require_admin), db: Session = Depends(get_db)):
    user, sess = user_sess
    invitations = list(db.scalars(select(Invitation).order_by(desc(Invitation.created_at)).limit(50)))
    users = list(db.scalars(select(User).order_by(User.email)))
    alert_days = int(load_schedule().get("collector", {}).get("source_failure_alert_days", 3))
    sources = list(db.scalars(select(SourceRecord).order_by(
        desc(SourceRecord.consecutive_failures), SourceRecord.domain)))
    runs = list(db.scalars(select(CollectRun).order_by(desc(CollectRun.started_at)).limit(20)))
    return render(request, "admin.html", user=user, csrf=sess.csrf_token,
                  invitations=invitations, users=users, sources=sources,
                  runs=runs, invite_link=invite_link, error=error,
                  failing_threshold=alert_days)


@router.post("/invite")
async def invite(request: Request, user_sess=Depends(require_admin),
                 db: Session = Depends(get_db)):
    user, _ = user_sess
    await check_csrf(request, user_sess)
    form = await request.form()
    try:
        token = auth_layer.create_invitation(
            db, email=str(form.get("email", "")),
            role=str(form.get("role", "analista")), created_by=user.id)
    except auth_layer.AuthError as exc:
        return RedirectResponse(f"/admin?error={exc.code}", status_code=303)
    base = os.environ.get("ATALAYA_BASE_URL", str(request.base_url).rstrip("/"))
    link = f"{base}/auth/invite/{token}"
    return RedirectResponse(f"/admin?invite_link={link}", status_code=303)


@router.post("/users/{user_id}/toggle")
async def toggle_user(request: Request, user_id: int,
                      user_sess=Depends(require_admin), db: Session = Depends(get_db)):
    admin, _ = user_sess
    await check_csrf(request, user_sess)
    target = db.get(User, user_id)
    if target and target.id != admin.id:
        target.active = not target.active
        db.commit()
    return RedirectResponse("/admin", status_code=303)
