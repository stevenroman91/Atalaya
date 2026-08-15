"""§10(4): la vista semanal y la síntesis mensual funcionan sobre datos
recogidos realmente (fixture server)."""
from datetime import date

from sqlalchemy import select

from atalaya.collect.fetcher import PoliteFetcher
from atalaya.db.models import MonthlySynthesis, WeeklyItem
from atalaya.jobs.runner import run_daily, run_monthly, run_weekly


def _fetcher(base):
    f = PoliteFetcher(base_url_override=base)
    f.delay = 0.0
    return f


def test_weekly_and_monthly(db, fixture_base):
    run_daily(db, countries=["GT"], fetcher=_fetcher(fixture_base))   # alimenta la tabla de incidentes
    run = run_weekly(db, countries=["GT"], fetcher=_fetcher(fixture_base))
    assert run.stats["process"]["items"] > 0

    items = list(db.scalars(select(WeeklyItem)))
    assert all(i.country == "GT" for i in items)
    themes = {i.theme for i in items}
    assert themes  # clasificados por tema
    # mini-resumen solo cuando hay texto íntegro (§7.1)
    assert all(i.mini_summary_es for i in items if i.article.text)

    # idempotencia semanal
    n = len(items)
    run_weekly(db, countries=["GT"], fetcher=_fetcher(fixture_base))
    assert len(list(db.scalars(select(WeeklyItem)))) == n

    # síntesis del mes en curso (los datos de test son de hoy)
    month = date.today().strftime("%Y-%m")
    run_monthly(db, month=month, countries=["GT"])
    synth = db.scalar(select(MonthlySynthesis).where(
        MonthlySynthesis.country == "GT", MonthlySynthesis.month == month))
    assert synth is not None
    assert synth.overview_es
    assert set(synth.sections) == {"politica", "economia", "sanitario", "natural", "seguridad"}
    # tabla de incidentes alimentada por el evento publicado del pipeline diario
    assert len(synth.incidents) == 1
    inc = synth.incidents[0]
    assert inc["categoria"] == "crimen de alto impacto"
    assert inc["fuentes"] and all(f["url"].startswith("https://") for f in inc["fuentes"])

    # regenerar no duplica (§8)
    run_monthly(db, month=month, countries=["GT"])
    assert len(list(db.scalars(select(MonthlySynthesis)))) == 1
