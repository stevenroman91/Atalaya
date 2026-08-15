"""Tratamiento semanal (§6.2): clasifica los artículos temáticos de la semana
por país × tema con un mini-resumen extractivo (primera frase del texto real —
nunca generado), para pre-estructurar la síntesis mensual."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from atalaya.config import load_countries
from atalaya.db.models import Article, ArticleStatus, CollectRun, WeeklyItem
from atalaya.process.summarize import split_sentences


def iso_week_of(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def mini_summary(article: Article) -> str | None:
    """§7.1: sin texto íntegro no hay resumen («título solamente»)."""
    if not article.text:
        return None
    sentences = split_sentences(article.text)
    return " ".join(sentences[:2]) if sentences else None


def process_weekly(db: Session, run: CollectRun, countries_filter: list[str] | None = None) -> dict:
    stats = {"items": 0, "title_only": 0, "duplicates": 0}
    week = iso_week_of(date.today())
    countries = load_countries()

    # ventana semanal por fecha de publicación (no por run: un artículo puede
    # haber entrado antes por el job diario y clasificarse ahora por tema)
    from datetime import datetime, timedelta, timezone
    from atalaya.config import load_schedule
    sched = load_schedule()["weekly"]
    window_start = datetime.now(timezone.utc) - timedelta(
        days=float(sched.get("window_days", 7)), hours=float(sched.get("overlap_hours", 6)))
    articles = db.scalars(select(Article).where(
        Article.theme.is_not(None),
        Article.published_at >= window_start,
        Article.status.in_([ArticleStatus.extracted.value, ArticleStatus.title_only.value]),
    ))
    for art in articles:
        c = countries.get(art.country or "")
        if not c or not c.weekly:
            continue
        if countries_filter and art.country not in countries_filter:
            continue
        exists = db.scalar(select(WeeklyItem).where(
            WeeklyItem.article_id == art.id, WeeklyItem.theme == art.theme))
        if exists:
            stats["duplicates"] += 1
            continue
        summary = mini_summary(art)
        if summary is None:
            stats["title_only"] += 1
        db.add(WeeklyItem(
            run_id=run.id, country=art.country, theme=art.theme,
            iso_week=week, article_id=art.id, mini_summary_es=summary,
        ))
        stats["items"] += 1
    db.commit()
    return stats
