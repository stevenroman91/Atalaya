"""Preferencias por usuario (§6.0): países seguidos, zonas MX, idioma, fuseau,
briefing por e-mail. Onboarding en la primera conexión."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from atalaya.config import SUPPORTED_LANGS, load_countries
from atalaya.web.deps import check_csrf, current_user, get_db, render

router = APIRouter(prefix="/account")

_COMMON_TZS = [
    "America/Mexico_City", "America/Guatemala", "America/Tegucigalpa",
    "America/Managua", "America/El_Salvador", "America/Panama",
    "America/Argentina/Buenos_Aires", "America/Sao_Paulo", "America/Caracas",
    "America/Asuncion", "Europe/Brussels", "Europe/Madrid", "Europe/Paris", "UTC",
]


def _mx_zone_choices():
    mx = load_countries().get("MX")
    return [z for z in (mx.zones if mx else []) if z.id != "mx-nacional"]


@router.get("/onboarding")
def onboarding(request: Request, user_sess=Depends(current_user)):
    user, sess = user_sess
    return render(request, "account.html", user=user, csrf=sess.csrf_token,
                  onboarding=True, langs=SUPPORTED_LANGS, tzs=_COMMON_TZS,
                  mx_zones=_mx_zone_choices(), saved=False)


@router.get("")
def account(request: Request, user_sess=Depends(current_user), saved: int = 0):
    user, sess = user_sess
    return render(request, "account.html", user=user, csrf=sess.csrf_token,
                  onboarding=False, langs=SUPPORTED_LANGS, tzs=_COMMON_TZS,
                  mx_zones=_mx_zone_choices(), saved=bool(saved))


@router.post("")
async def save(request: Request, user_sess=Depends(current_user),
               db: Session = Depends(get_db)):
    user, _ = user_sess
    await check_csrf(request, user_sess)
    form = await request.form()
    countries = [c for c in form.getlist("countries") if c in load_countries()]
    mx_zone_ids = {z.id for z in _mx_zone_choices()}
    mx_zones = [z for z in form.getlist("mx_zones") if z in mx_zone_ids]
    lang = form.get("lang", "es")
    user.countries = countries
    user.mx_zones = mx_zones or None       # None/[] = todas las zonas MX (§6.0)
    user.lang = lang if lang in SUPPORTED_LANGS else "es"
    user.timezone = form.get("timezone", user.timezone)
    user.email_briefing = form.get("email_briefing") == "on"
    user.onboarded = True
    db.commit()
    return RedirectResponse("/dashboard" if form.get("from_onboarding") else "/account?saved=1",
                            status_code=303)
