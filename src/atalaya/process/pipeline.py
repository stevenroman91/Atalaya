"""Orquestación del tratamiento diario: cluster → score → clasifica → redacta
→ persiste eventos. Idempotente: la clave de dédup del cluster es estable, un
re-run actualiza el evento existente en lugar de duplicarlo (§8).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from atalaya.collect.whitelist import (
    event_abroad, off_topic_section, perimeter_country_for,
)
from atalaya.config import load_countries, load_schedule, zone_by_id
from atalaya.db.models import (
    Article, ArticleStatus, CollectRun, Event, EventArticle, EventStatus,
)
from atalaya.process.cluster import Cluster, cluster_articles
from atalaya.process.scoring import (
    classify_category, classify_level, classify_type, independent_source_count,
    score_cluster,
)
from atalaya.process.summarize import build_recommendations, build_summary, summary_version

log = logging.getLogger(__name__)


def _cluster_zone(cluster: Cluster) -> str | None:
    zones = [a.zone_id for a in cluster.articles if a.zone_id]
    return max(set(zones), key=zones.count) if zones else None


def _cluster_occurred_at(cluster: Cluster) -> datetime | None:
    dates = [a.published_at for a in cluster.articles if a.published_at]
    return min(dates) if dates else None


def _screen_stored(db: Session, code: str, articles: list[Article], stats: dict) -> list[Article]:
    """Aplica los filtros de pertinencia a los artículos YA guardados.

    La selección va por ventana de frescura, no por run: cada noche se
    vuelven a leer artículos ingeridos por versiones anteriores del
    recolector, anteriores a estos filtros. Sin este paso, un artículo
    pernicioso guardado ayer sigue generando su evento hasta salir de la
    ventana — filtrar solo en la ingesta no basta.

    No se borra nada: el artículo queda en base con estado «rechazado» y el
    motivo, recuperable por el analista.
    """
    kept: list[Article] = []
    for a in articles:
        section = off_topic_section(a.url or "")
        if section:
            a.status = ArticleStatus.rejected.value
            a.reject_reason = f"sección ajena a la vigilancia: {section}"
            stats["screened"] += 1
            log.info("descartado en tratamiento [%s] %s", a.reject_reason, a.title[:100])
            continue

        abroad = event_abroad(code, a.title)
        if abroad:
            other = perimeter_country_for(abroad)
            if other and other != code:
                # el hecho está en otro país vigilado: se reatribuye, no se
                # pierde. Lo tratará el bucle de ese país (este run o el
                # siguiente, según el orden de configuración).
                a.country = other
                a.zone_id = None
                stats["reattributed"] += 1
            else:
                a.status = ArticleStatus.rejected.value
                a.reject_reason = f"hecho localizado fuera del perímetro: {abroad}"
                stats["screened"] += 1
                log.info("descartado en tratamiento [%s] %s", a.reject_reason, a.title[:100])
            continue

        kept.append(a)
    return kept


def screen_event(ev: Event) -> tuple[str | None, str | None]:
    """Juzga un evento ya creado: (país al que reatribuir, motivo de retirada).

    Aplica las reglas al EVENTO, no a sus artículos. Es la diferencia que
    hacía inútil la primera versión de este barrido: solo miraba artículos
    marcados como rechazados, y un artículo únicamente se re-examina si
    sigue dentro de la ventana de frescura. Los eventos del día anterior
    quedaban congelados — demasiado viejos para volver a tratarse, pero
    aún visibles en el panel.
    """
    abroad = event_abroad(ev.country, ev.title_es or "")
    if abroad:
        other = perimeter_country_for(abroad)
        if other and other != ev.country:
            return other, None
        return None, f"hecho localizado fuera del perímetro: {abroad}"

    # La sección se lee en las URL de respaldo. Basta con que la mayoría sean
    # ajenas: el titular del evento sale del artículo más completo, y un
    # cluster mayoritariamente de opinión es una columna, no un suceso.
    urls = [ea.article.url or "" for ea in ev.articles if ea.article]
    off = [off_topic_section(u) for u in urls]
    ajenos = [s for s in off if s]
    if urls and len(ajenos) * 2 > len(urls):
        return None, f"sección ajena a la vigilancia: {ajenos[0]}"
    return None, None


def _retire_screened_events(db: Session, stats: dict) -> None:
    """Pasa por el filtro los eventos ya publicados, sin límite de antigüedad.

    Se retiran del panel —estado «descartado», nunca borrados— o se
    reatribuyen al país donde ocurre el hecho. El analista conserva la
    posibilidad de contradecir el juicio.
    """
    live = (EventStatus.published.value, EventStatus.pending_confirm.value)
    for ev in db.scalars(select(Event).where(Event.status.in_(live))):
        other, reason = screen_event(ev)
        if other:
            ev.country = other
            ev.zone_id = None
            ev.lat = ev.lon = None      # la geo de la zona anterior ya no aplica
            stats["reattributed"] += 1
            log.info("evento reatribuido a %s: %s", other, (ev.title_es or "")[:100])
        elif reason:
            ev.status = EventStatus.discarded.value
            ev.score_detail = {**(ev.score_detail or {}), "retirado": reason}
            stats["retired"] += 1
            log.info("evento retirado del panel [%s] %s", reason, (ev.title_es or "")[:100])


def process_daily(db: Session, run: CollectRun, countries_filter: list[str] | None = None) -> dict:
    stats = {"clusters": 0, "published": 0, "pending_confirm": 0, "discarded": 0,
             "updated": 0, "screened": 0, "reattributed": 0, "retired": 0}
    countries = load_countries()
    zones = zone_by_id()

    for code, country in countries.items():
        if not country.daily or (countries_filter and code not in countries_filter):
            continue
        # Selección por ventana de frescura, NO por run_id: los artículos de
        # un run interrumpido (o deduplicados en el run actual) siguen siendo
        # procesables — el upsert por dedup_key garantiza la idempotencia.
        sched = load_schedule()["daily"]
        window = float(sched.get("window_hours", 24)) + float(sched.get("overlap_hours", 2))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window)
        articles = list(db.scalars(
            select(Article).where(
                Article.country == code,
                Article.published_at >= cutoff,
                Article.status.in_([ArticleStatus.extracted.value, ArticleStatus.title_only.value]),
                Article.theme.is_(None),   # los artículos temáticos son del flujo semanal
            )
        ))
        articles = _screen_stored(db, code, articles, stats)
        if not articles:
            continue

        for cluster in cluster_articles(articles):
            stats["clusters"] += 1
            result = score_cluster(cluster, country.lang)
            total, independent, has_state = independent_source_count(cluster.articles)

            if result.publishable:
                status = EventStatus.published.value
            elif result.pending_confirm:
                status = EventStatus.pending_confirm.value
            else:
                stats["discarded"] += 1
                continue    # no persistimos el ruido; queda trazado en logs/stats

            category = classify_category(cluster, country.lang)
            level = classify_level(cluster, country.lang)
            etype = classify_type(category, result.reasons["severity"])

            zone_id = _cluster_zone(cluster)
            zone = zones.get(zone_id) if zone_id else None
            # {lugar} de las recomendaciones: sintagma completo. Sin zona
            # conocida, genérico — nunca «la zona de México» (país entero).
            place = f"la zona de {zone.name}" if zone else "la zona afectada"
            geo = zone.geo if zone and zone.geo else None
            if geo is None:
                # sin zona precisa: marcador a nivel país (primera zona con
                # geo — p. ej. mx-nacional / la capital) para que el evento
                # aparezca igualmente en el mapa
                geo = next((z.geo for z in country.zones if z.geo), None)

            rep = cluster.representative
            summary = build_summary(cluster)
            recommendations = (
                build_recommendations(category, place,
                                      f"{rep.title}\n{summary or ''}")
                if etype == "ALERTA" and status == EventStatus.published.value else None
            )
            version = summary_version(rep.title, summary, recommendations)
            key = cluster.dedup_key()

            existing = db.scalar(select(Event).where(Event.dedup_key == key))
            if existing:
                # re-run o cluster que ganó fuentes: actualizar, no duplicar
                existing.recurrence = total
                existing.independent_sources = independent
                existing.has_state_media = has_state
                existing.status = status
                existing.event_type = etype
                existing.category = category
                existing.level = level
                existing.summary_es = summary
                existing.recommendations_es = recommendations
                existing.summary_version = version
                existing.score_detail = result.reasons
                if existing.lat is None and geo:   # eventos antiguos sin coordenadas
                    existing.lat, existing.lon = geo
                    existing.zone_id = existing.zone_id or zone_id
                event = existing
                stats["updated"] += 1
            else:
                event = Event(
                    run_id=run.id, dedup_key=key, country=code, zone_id=zone_id,
                    lat=geo[0] if geo else None, lon=geo[1] if geo else None,
                    title_es=rep.title, summary_es=summary,
                    recommendations_es=recommendations, summary_version=version,
                    event_type=etype, category=category, level=level, status=status,
                    occurred_at=_cluster_occurred_at(cluster),
                    recurrence=total, independent_sources=independent,
                    has_state_media=has_state, score_detail=result.reasons,
                )
                db.add(event)
                db.flush()
                if status == EventStatus.published.value:
                    stats["published"] += 1
                else:
                    stats["pending_confirm"] += 1

            linked = {ea.article_id for ea in event.articles}
            for a in cluster.articles:
                if a.id not in linked:
                    db.add(EventArticle(event_id=event.id, article_id=a.id))

    db.flush()          # los rechazos deben ser visibles para el barrido
    _retire_screened_events(db, stats)
    db.commit()
    return stats
