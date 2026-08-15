"""Jobs de Atalaya (§8): diario, semanal, mensual.

Cada job crea un CollectRun con estadísticas completas (recolectado, rechazado
por motivo). Idempotencia: artículos dedupe por URL, eventos por dedup_key,
weekly_items por (article, theme), síntesis por (país, mes) — relanzar un job
no crea duplicados.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from atalaya.collect.collector import Collector, RunCancelled
from atalaya.collect.fetcher import PoliteFetcher
from atalaya.db.models import CollectRun, utcnow
from atalaya.process.pipeline import process_daily
from atalaya.process.translate import translate_pending

log = logging.getLogger(__name__)


def _session_factory():
    """Factory de sesiones para la colecta paralela por país."""
    from atalaya.db import SessionLocal
    return SessionLocal


def _finish(db: Session, run: CollectRun, stats: dict, ok: bool = True) -> None:
    run.stats = stats
    run.finished_at = utcnow()
    run.ok = ok
    db.commit()


def run_daily(db: Session, countries: list[str] | None = None,
              fetcher: PoliteFetcher | None = None, origin: str = "cron") -> CollectRun:
    run = CollectRun(kind="daily", stats={"origin": origin})
    db.add(run)
    db.commit()
    stats: dict = {"origin": origin}
    collector = Collector(db, fetcher, session_factory=_session_factory())
    try:
        stats["collect"] = collector.collect_daily(run, countries)
        stats["process"] = process_daily(db, run, countries)
        stats["translate"] = translate_pending(db)
        _finish(db, run, stats)
    except RunCancelled:
        log.info("job diario anulado por el administrador")
        stats["collect"] = collector.stats
        stats["cancelled"] = True
        _finish(db, run, stats, ok=False)
    except Exception:
        log.exception("job diario falló")
        _finish(db, run, stats, ok=False)
        raise
    return run


def run_weekly(db: Session, countries: list[str] | None = None,
               fetcher: PoliteFetcher | None = None, origin: str = "cron") -> CollectRun:
    from atalaya.jobs.weekly import process_weekly
    run = CollectRun(kind="weekly", stats={"origin": origin})
    db.add(run)
    db.commit()
    stats: dict = {"origin": origin}
    collector = Collector(db, fetcher, session_factory=_session_factory())
    try:
        stats["collect"] = collector.collect_weekly(run, countries)
        stats["process"] = process_weekly(db, run, countries)
        _finish(db, run, stats)
    except RunCancelled:
        log.info("job semanal anulado por el administrador")
        stats["collect"] = collector.stats
        stats["cancelled"] = True
        _finish(db, run, stats, ok=False)
    except Exception:
        log.exception("job semanal falló")
        _finish(db, run, stats, ok=False)
        raise
    return run


def run_monthly(db: Session, month: str | None = None,
                countries: list[str] | None = None) -> CollectRun:
    from atalaya.jobs.monthly import generate_monthly
    run = CollectRun(kind="monthly")
    db.add(run)
    db.commit()
    try:
        stats = generate_monthly(db, month=month, countries=countries)
        _finish(db, run, stats)
    except Exception:
        log.exception("job mensual falló")
        _finish(db, run, {}, ok=False)
        raise
    return run
