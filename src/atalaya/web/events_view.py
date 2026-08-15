"""Construcción de las vistas de eventos: filtrado por preferencias del
usuario, localización (traducciones en cache) y serialización para plantillas,
mapa y exportes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from atalaya.config import load_countries, zone_by_id
from atalaya.db.models import Event, EventStatus, User


@dataclass
class EventFilters:
    countries: list[str] = field(default_factory=list)
    zone: str | None = None
    category: str | None = None
    level: str | None = None
    event_type: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    q: str | None = None
    statuses: list[str] = field(default_factory=lambda: [EventStatus.published.value])


def default_filters_for(user: User) -> EventFilters:
    """§6.0: por defecto solo los países seguidos (se puede ampliar con filtros)."""
    return EventFilters(countries=list(user.countries or []))


def _dim_conditions(f: EventFilters) -> list:
    """Condiciones de dimensión (país/zona/categoría/nivel/tipo/texto),
    compartidas entre la lista de eventos y los contadores de cabecera."""
    conds = []
    if f.countries:
        conds.append(Event.country.in_(f.countries))
    if f.zone:
        conds.append(Event.zone_id == f.zone)
    if f.category:
        conds.append(Event.category == f.category)
    if f.level:
        conds.append(Event.level == f.level)
    if f.event_type:
        conds.append(Event.event_type == f.event_type)
    if f.q:
        needle = f"%{f.q}%"
        conds.append(or_(Event.title_es.ilike(needle), Event.summary_es.ilike(needle)))
    return conds


def query_events(db: Session, f: EventFilters, limit: int = 200) -> list[Event]:
    stmt = select(Event).options(
        selectinload(Event.articles), selectinload(Event.translations)
    ).where(Event.status.in_(f.statuses), *_dim_conditions(f))
    if f.date_from:
        stmt = stmt.where(Event.occurred_at >= f.date_from)
    if f.date_to:
        stmt = stmt.where(Event.occurred_at < f.date_to + timedelta(days=1))
    # gravedad primero (ALERTA antes que NOTA, advertencia antes que informativo), luego récence
    stmt = stmt.order_by(
        Event.event_type.asc(),      # ALERTA < NOTA alfabéticamente
        Event.level.asc(),           # advertencia < informativo
        Event.occurred_at.desc().nullslast(),
    ).limit(limit)
    return list(db.scalars(stmt).unique())


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def localize_event(event: Event, lang: str, user_tz: str) -> dict:
    """Devuelve el evento en la lengua pedida usando SOLO traducciones en
    cache válidas (misma versión que el canónico). Si no hay, canónico español
    con la marca `es_only`. URL y títulos de artículos: nunca traducidos (§6.0)."""
    title, summary, recs, es_only = event.title_es, event.summary_es, event.recommendations_es, False
    if lang != "es":
        tr = next((t for t in event.translations
                   if t.lang == lang and t.version == event.summary_version), None)
        if tr:
            title, summary, recs = tr.title or title, tr.summary or summary, tr.recommendations or recs
        else:
            es_only = True

    try:
        tz = ZoneInfo(user_tz)
    except Exception:
        tz = timezone.utc
    occurred = _aware(event.occurred_at)
    zone = zone_by_id().get(event.zone_id) if event.zone_id else None
    country = load_countries().get(event.country)
    sources = [{"url": ea.article.url, "name": ea.article.source_name or ea.article.domain,
                "state": ea.article.source_type == "estatal"} for ea in event.articles]
    return {
        "id": event.id,
        "title": title, "summary": summary, "recommendations": recs or [],
        "es_only": es_only,
        "event_type": event.event_type, "category": event.category, "level": event.level,
        "status": event.status,
        "country": event.country,
        "country_name": country.name if country else event.country,
        "zone_name": zone.name if zone else None,
        "lat": event.lat, "lon": event.lon,
        "occurred_at": occurred.astimezone(tz).strftime("%Y-%m-%d %H:%M") if occurred else None,
        "occurred_date": occurred.date().isoformat() if occurred else None,
        "created_at": _aware(event.created_at),
        "sources": sources,
        "n_sources": event.recurrence,
        "has_state_media": event.has_state_media,
    }


def counters(db: Session, f: EventFilters) -> dict:
    """Compteurs de tête: alertes/notes du jour + à confirmer, sur le MÊME
    périmètre que les filtres actifs (« hoy » = créés dans les 24 h)."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    events = db.scalars(select(Event).where(Event.created_at >= since,
                                            *_dim_conditions(f)))
    out = {"alerts": 0, "notes": 0, "pending": 0, "by_country": {}}
    for ev in events:
        if ev.status == EventStatus.pending_confirm.value:
            out["pending"] += 1
            continue
        if ev.status != EventStatus.published.value:
            continue
        key = "alerts" if ev.event_type == "ALERTA" else "notes"
        out[key] += 1
        bc = out["by_country"].setdefault(ev.country, {"alerts": 0, "notes": 0})
        bc["alerts" if ev.event_type == "ALERTA" else "notes"] += 1
    return out


def timeline(db: Session, f: EventFilters, days: int = 7) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(Event).where(Event.status == EventStatus.published.value,
                               Event.occurred_at >= since,
                               *_dim_conditions(f))
    buckets: dict[str, dict] = {}
    for i in range(days, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
        buckets[day] = {"date": day, "alerts": 0, "notes": 0}
    for ev in db.scalars(stmt):
        day = _aware(ev.occurred_at).date().isoformat()
        if day in buckets:
            buckets[day]["alerts" if ev.event_type == "ALERTA" else "notes"] += 1
    return list(buckets.values())
