"""Dashboard principal (§6.1): tarjetas de alerta, filtros, mapa, timeline,
marcador «nuevo» por cuenta y export del briefing diario."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from sqlalchemy import select

from atalaya.db.models import EventStatus, SourceRecord, utcnow
from atalaya.web.deps import current_user, get_db, render, templates
from atalaya.web.events_view import (
    EventFilters, counters, default_filters_for, localize_event, query_events, timeline,
)
from atalaya.web.i18n import translator

router = APIRouter()


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _filters_from_query(user, country: list[str] | None, zone: str | None,
                        category: str | None, level: str | None, type_: str | None,
                        date_from: str | None, date_to: str | None, q: str | None,
                        scope: str | None) -> EventFilters:
    f = default_filters_for(user)
    if scope == "all":
        f.countries = []          # ampliar puntualmente a todo el perímetro
    if country:
        f.countries = country
    f.zone = zone or None
    f.category = category or None
    f.level = level or None
    f.event_type = type_ or None
    f.date_from = _parse_date(date_from)
    f.date_to = _parse_date(date_to)
    f.q = (q or "").strip() or None
    return f


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request,
              country: list[str] | None = Query(default=None),
              zone: str | None = None, category: str | None = None,
              level: str | None = None, type: str | None = None,
              date_from: str | None = None, date_to: str | None = None,
              q: str | None = None, scope: str | None = None,
              user_sess=Depends(current_user), db: Session = Depends(get_db)):
    user, sess = user_sess
    f = _filters_from_query(user, country, zone, category, level, type,
                            date_from, date_to, q, scope)
    f.statuses = [EventStatus.published.value, EventStatus.pending_confirm.value]
    events = [localize_event(e, user.lang, user.timezone) for e in query_events(db, f)]

    last_seen = user.last_seen_at
    if last_seen and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    for e in events:
        e["is_new"] = bool(last_seen is None or (e["created_at"] and e["created_at"] > last_seen))

    stats = counters(db, f)
    tl = timeline(db, f)
    # Transparence de couverture (§7): fuentes cuyo flujo directo falló en la
    # última colecta — el analista debe saber qué NO se pudo consultar.
    unreachable = [s.name for s in db.scalars(
        select(SourceRecord).where(SourceRecord.consecutive_failures >= 1)
        .order_by(SourceRecord.consecutive_failures.desc(), SourceRecord.name))]
    user.last_seen_at = utcnow()      # marcador «nuevo» por cuenta
    db.commit()

    return render(request, "dashboard.html", user=user, csrf=sess.csrf_token,
                  events=events, stats=stats, timeline=tl, f=f,
                  unreachable_sources=unreachable,
                  query=dict(request.query_params))


@router.get("/dashboard/map.json")
def map_data(request: Request, scope: str | None = None,
             user_sess=Depends(current_user), db: Session = Depends(get_db)):
    user, _ = user_sess
    f = default_filters_for(user)
    if scope == "all":
        f.countries = []
    features = []
    for ev in query_events(db, f, limit=500):
        if ev.lat is None or ev.lon is None:
            continue
        loc = localize_event(ev, user.lang, user.timezone)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [ev.lon, ev.lat]},
            "properties": {
                "id": ev.id, "title": loc["title"], "type": ev.event_type,
                "category": ev.category, "level": ev.level,
                "place": loc["zone_name"] or loc["country_name"],
                "date": loc["occurred_at"],
            },
        })
    return JSONResponse({"type": "FeatureCollection", "features": features})


@router.get("/dashboard/briefing", response_class=HTMLResponse)
def briefing(request: Request, format: str = "html",
             user_sess=Depends(current_user), db: Session = Depends(get_db)):
    """Briefing diario imprimible (PDF si weasyprint está disponible, si no
    HTML autónomo), en la lengua del usuario y sobre sus países (§6.1)."""
    user, _ = user_sess
    f = default_filters_for(user)
    f.statuses = [EventStatus.published.value]
    events = [localize_event(e, user.lang, user.timezone)
              for e in query_events(db, f, limit=100)]
    t = translator(user.lang)
    html = templates.get_template("briefing.html").render({
        "t": t, "lang": user.lang, "events": events,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "user": user,
    })
    if format == "pdf":
        try:
            from weasyprint import HTML  # requiere libs del sistema (ver README)
            pdf = HTML(string=html).write_pdf()
            return Response(pdf, media_type="application/pdf", headers={
                "Content-Disposition": "attachment; filename=briefing-atalaya.pdf"})
        except Exception:
            pass  # degradación elegante → HTML imprimible
    return HTMLResponse(html)
