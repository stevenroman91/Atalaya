"""Orquestación del tratamiento diario: cluster → score → clasifica → redacta
→ persiste eventos. Idempotente: la clave de dédup del cluster es estable, un
re-run actualiza el evento existente en lugar de duplicarlo (§8).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def process_daily(db: Session, run: CollectRun, countries_filter: list[str] | None = None) -> dict:
    stats = {"clusters": 0, "published": 0, "pending_confirm": 0, "discarded": 0,
             "updated": 0}
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
            place = zone.name if zone else country.name
            geo = zone.geo if zone and zone.geo else None

            rep = cluster.representative
            summary = build_summary(cluster)
            recommendations = (
                build_recommendations(category, place)
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
    db.commit()
    return stats
