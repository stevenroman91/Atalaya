"""Espace admin (§6.1): invitations, comptes, santé de la collecte."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from atalaya.config import load_schedule
from atalaya.db.models import CollectRun, Invitation, SourceRecord, User
from atalaya.web import auth as auth_layer
from atalaya.web.deps import check_csrf, get_db, render, require_admin

router = APIRouter(prefix="/admin")


log = logging.getLogger(__name__)


@router.get("")
def admin_home(request: Request, invite_link: str | None = None, error: str | None = None,
               notice: str | None = None,
               user_sess=Depends(require_admin), db: Session = Depends(get_db)):
    user, sess = user_sess
    invitations = list(db.scalars(select(Invitation).order_by(desc(Invitation.created_at)).limit(50)))
    users = list(db.scalars(select(User).order_by(User.email)))
    alert_days = int(load_schedule().get("collector", {}).get("source_failure_alert_days", 3))
    sources = list(db.scalars(select(SourceRecord).order_by(
        desc(SourceRecord.consecutive_failures), SourceRecord.domain)))
    runs = list(db.scalars(select(CollectRun).order_by(desc(CollectRun.started_at)).limit(20)))
    collect_running = db.scalar(_active_run_query()) is not None
    return render(request, "admin.html", user=user, csrf=sess.csrf_token,
                  invitations=invitations, users=users, sources=sources,
                  runs=runs, invite_link=invite_link, error=error, notice=notice,
                  collect_running=collect_running, failing_threshold=alert_days)


def _active_run_query():
    """Runs sin terminar y recientes. Un run cuyo proceso murió (redeploy,
    restart) queda con finished_at NULL para siempre: pasadas 2 h se ignora
    para no bloquear el botón de colecta manual."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    return (select(CollectRun)
            .where(CollectRun.finished_at.is_(None), CollectRun.started_at > cutoff)
            .order_by(desc(CollectRun.started_at)))


def _collect_in_background(kind: str) -> None:
    """Ejecuta un job de colecta en un hilo con su propia sesión DB."""
    from atalaya.db import SessionLocal
    from atalaya.jobs.runner import run_daily, run_weekly

    def worker():
        try:
            with SessionLocal() as job_db:
                (run_daily if kind == "daily" else run_weekly)(job_db)
        except Exception:
            log.exception("colecta %s lanzada desde admin falló", kind)

    threading.Thread(target=worker, daemon=True, name=f"collect-{kind}").start()


@router.post("/collect-now")
async def collect_now(request: Request, user_sess=Depends(require_admin),
                      db: Session = Depends(get_db)):
    await check_csrf(request, user_sess)
    in_progress = db.scalar(_active_run_query())
    if in_progress is not None:
        return RedirectResponse("/admin?notice=running", status_code=303)
    _collect_in_background("daily")
    return RedirectResponse("/admin?notice=started", status_code=303)


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
