"""Contrôle de la collecte : annulation coopérative et parallélisation."""
import pytest

from atalaya.collect.collector import Collector, RunCancelled
from atalaya.collect.fetcher import PoliteFetcher
from atalaya.db.base import SessionLocal
from atalaya.db.models import CollectRun


def _fetcher(base: str) -> PoliteFetcher:
    f = PoliteFetcher(base_url_override=base)
    f.delay = 0.0
    return f


def test_cancel_flag_stops_collection(db, fixture_base):
    run = CollectRun(kind="daily", cancel_requested=True)
    db.add(run)
    db.commit()
    collector = Collector(db, _fetcher(fixture_base))
    with pytest.raises(RunCancelled):
        collector.collect_daily(run, countries=["GT"])


def test_runner_marks_cancelled_run(db, fixture_base, monkeypatch):
    from atalaya.jobs import runner as runner_mod

    def cancelled(self, run, countries=None):
        raise RunCancelled()

    monkeypatch.setattr(Collector, "collect_daily", cancelled)
    run = runner_mod.run_daily(db, countries=["GT"], fetcher=_fetcher(fixture_base))
    assert run.ok is False
    assert run.finished_at is not None
    assert run.stats.get("cancelled") is True


def test_parallel_run_parallel_merges_stats(db, fixture_base):
    from atalaya.config import load_countries

    run = CollectRun(kind="daily")
    db.add(run)
    db.commit()
    collector = Collector(db, _fetcher(fixture_base), session_factory=SessionLocal)
    todo = list(load_countries().values())[:2]
    assert len(todo) == 2

    def work(col, wrun, country):
        # chaque worker a sa propre session et son propre Collector
        assert col is not collector
        assert wrun.id == run.id
        col.stats["stored"] += 1
        col.stats["reject_reasons"]["test"] = 1

    collector._run_parallel(run, todo, work)
    assert collector.stats["stored"] == 2
    assert collector.stats["reject_reasons"]["test"] == 2


def test_sequential_fallback_without_factory(db, fixture_base):
    from atalaya.config import load_countries

    run = CollectRun(kind="daily")
    db.add(run)
    db.commit()
    collector = Collector(db, _fetcher(fixture_base))  # sans session_factory
    seen = []
    collector._run_parallel(run, list(load_countries().values())[:2],
                             lambda col, r, c: seen.append((col is collector, c.code)))
    assert all(same for same, _ in seen) and len(seen) == 2


def test_startup_marks_orphan_manual_runs(db):
    from atalaya.web.app import _mark_interrupted_manual_runs

    manual = CollectRun(kind="daily", stats={"origin": "manual"})
    cron = CollectRun(kind="daily", stats={"origin": "cron"})
    db.add_all([manual, cron])
    db.commit()
    manual_id, cron_id = manual.id, cron.id
    db.expunge_all()

    _mark_interrupted_manual_runs()

    manual = db.get(CollectRun, manual_id)
    cron = db.get(CollectRun, cron_id)
    assert manual.finished_at is not None and manual.ok is False
    assert manual.stats.get("interrupted") is True
    assert cron.finished_at is None and cron.ok is None
