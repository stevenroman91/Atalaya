"""Construcción de las vistas de eventos: filtrado por preferencias del
usuario, localización (traducciones en cache) y serialización para plantillas,
mapa y exportes."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
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
    # Ocultar lo que el clasificador declaró ajeno a la seguridad. Es una
    # elección del analista, nunca el valor por defecto: esconder de oficio
    # el veredicto de un modelo sería descartar en silencio.
    hide_nonsec: bool = False
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
    if f.hide_nonsec:
        conds.append(Event.category != "no_securitario")
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
        # El veredicto del clasificador, a la vista: el analista debe poder
        # contradecirlo, y para eso tiene que leer en qué se basó.
        "classifier": (event.score_detail or {}).get("clasificador"),
    }


def counters(db: Session, f: EventFilters) -> dict:
    """Compteurs de tête: alertes/notes du jour + à confirmer, sur le MÊME
    périmètre que les filtres actifs (« hoy » = créés dans les 24 h)."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    events = db.scalars(select(Event).where(Event.created_at >= since,
                                            *_dim_conditions(f)))
    out = {"alerts": 0, "notes": 0, "pending": 0, "nonsec": 0, "by_country": {}}
    for ev in events:
        if ev.status == EventStatus.pending_confirm.value:
            out["pending"] += 1
            continue
        if ev.status != EventStatus.published.value:
            continue
        # Lo que el clasificador declaró ajeno a la seguridad tiene su propio
        # contador. Contarlo entre las notas daba «13 notas hoy» cuando las
        # trece eran justamente lo que no había que leer.
        if ev.category == "no_securitario":
            out["nonsec"] += 1
            continue
        key = "alerts" if ev.event_type == "ALERTA" else "notes"
        out[key] += 1
        bc = out["by_country"].setdefault(ev.country, {"alerts": 0, "notes": 0})
        bc["alerts" if ev.event_type == "ALERTA" else "notes"] += 1
    return out


def doubtful_events(db: Session, f: EventFilters, lang: str = "es",
                    tz: str = "UTC") -> list[dict]:
    """Eventos cuya clasificación el modelo no sostiene con seguridad.

    Su veredicto NO se ha aplicado: está aquí para que el analista lo lea y
    decida. Es la doctrina del proyecto en una pantalla — no trancher, pero
    exposer l'incertitude: se ve lo que el modelo proponía, con qué certeza
    y por qué, junto a la etiqueta que el evento conserva mientras tanto.

    Un veredicto sin confianza declarada —guardado antes de que existiera el
    umbral— también entra aquí: su etiqueta se aplicó sin que nadie sepa con
    qué certeza. Callarlo sería hacerlo pasar por seguro.
    """
    out = []
    for ev in query_events(db, replace(f, hide_nonsec=False), limit=200):
        v = (ev.score_detail or {}).get("clasificador") or {}
        if not v:
            continue                  # sin veredicto: nada que exponer aquí
        if not v.get("dudoso") and v.get("confianza") is not None:
            continue
        loc = localize_event(ev, lang, tz)
        out.append({
            "event": loc,
            "propuesta": v.get("categoria"),
            "es_seguridad": v.get("es_seguridad"),
            "confianza": v.get("confianza"),
            "motivo": v.get("motivo"),
            "actual": ev.category,
        })
    # Primero lo que exige una decisión del analista (confianza más baja
    # arriba); al final los veredictos sin confianza declarada, que no se
    # arreglan decidiendo sino volviendo a pasar el clasificador.
    out.sort(key=lambda d: (d["confianza"] is None, d["confianza"] or 0))
    return out


def count_nonsec(db: Session, f: EventFilters) -> int:
    """Cuántos eventos visibles declaró el clasificador ajenos a la seguridad."""
    from sqlalchemy import func

    visible = replace(f, hide_nonsec=False)
    return db.scalar(
        select(func.count(Event.id))
        .where(Event.status.in_(f.statuses), Event.category == "no_securitario",
               *_dim_conditions(replace(visible, category=None)))) or 0


def counts_by_country(db: Session, f: EventFilters) -> dict[str, int]:
    """Nº de eventos por país con TODOS los filtros vigentes menos el país.

    Alimenta las pestañas: el analista debe ver de un vistazo dónde hay algo
    antes de hacer clic, y no descubrir la pestaña vacía después.
    """
    from sqlalchemy import func

    sin_pais = replace(f, countries=[])
    stmt = (select(Event.country, func.count(Event.id))
            .where(Event.status.in_(f.statuses), *_dim_conditions(sin_pais))
            .group_by(Event.country))
    if f.date_from:
        stmt = stmt.where(Event.occurred_at >= f.date_from)
    if f.date_to:
        stmt = stmt.where(Event.occurred_at < f.date_to + timedelta(days=1))
    return {code: n for code, n in db.execute(stmt)}


def country_tabs(db: Session, f: EventFilters, query: dict,
                 followed: list[str] | None = None) -> list[dict]:
    """Pestañas por país. Los países seguidos por la cuenta primero, luego el
    resto del perímetro — con su recuento, aunque estén fuera del perfil: ver
    que Venezuela tiene 12 eventos es la única forma de pensar en mirarlos."""
    counts = counts_by_country(db, f)
    seguidos = list(followed if followed is not None else f.countries)

    def href(code: str | None) -> str:
        params = {k: v for k, v in query.items() if k not in ("country", "scope")}
        if code:
            params["country"] = code
        else:
            params["scope"] = "all"
        return "/dashboard" + ("?" + urlencode(params) if params else "")

    tabs = [{"code": None, "name": None, "count": sum(counts.values()),
             "href": href(None), "active": not query.get("country")}]
    ordenados = sorted(load_countries().items(),
                       key=lambda kv: (kv[0] not in seguidos, kv[1].name))
    for code, c in ordenados:
        tabs.append({"code": code, "name": c.name, "count": counts.get(code, 0),
                     "href": href(code), "followed": code in seguidos,
                     "active": query.get("country") == code})
    return tabs


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
