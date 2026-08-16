"""Los filtros de pertinencia también se aplican a lo ya guardado.

La selección del tratamiento diario va por ventana de frescura, no por run:
cada noche vuelve a leer artículos ingeridos por versiones anteriores del
recolector. Filtrar solo en la ingesta dejaba intactos los eventos ya
creados —el panel seguía mostrándolos— y volvía a producirlos mientras el
artículo siguiera dentro de la ventana.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from atalaya.db.models import (
    Article, ArticleStatus, CollectRun, Event, EventArticle, EventStatus,
)
from atalaya.process.pipeline import process_daily


def _run(db) -> CollectRun:
    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    return run


def _article(db, run, **kw) -> Article:
    defaults = dict(
        run_id=run.id,
        url="https://www.laprensani.com/2026/08/15/sucesos/1-nota",
        domain="laprensani.com",
        source_name="La Prensa",
        source_type="independiente",
        title="Balacera deja dos heridos en el mercado oriental de Managua",
        text="Una balacera en el mercado oriental dejó 2 heridos este viernes. "
             "Sujetos armados dispararon contra un puesto de comerciantes. "
             "La Policía Nacional acordonó la zona tras el ataque armado.",
        published_at=datetime.now(timezone.utc) - timedelta(hours=3),
        country="NI",
        status=ArticleStatus.extracted.value,
    )
    defaults.update(kw)
    a = Article(**defaults)
    db.add(a)
    db.flush()
    return a


# ── sección ajena: la columna «¡Ay Andy!» de la primera prueba real ──────
def test_columna_de_opinion_guardada_no_produce_evento(db):
    run = _run(db)
    _article(db, run,
             url="https://www.elimparcial.com/columnas/2026/08/15/ay-andy/",
             title="¡Ay Andy!",
             country="MX")

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["screened"] == 1
    assert not list(db.scalars(select(Event)))
    art = db.scalar(select(Article))
    # marcado con su motivo, NO borrado: el analista puede contradecirlo
    assert art.status == ArticleStatus.rejected.value
    assert "sección ajena" in art.reject_reason


# ── hecho fuera del perímetro: los sismos de Colombia e Indonesia ────────
def test_hecho_en_el_extranjero_guardado_no_produce_evento(db):
    run = _run(db)
    _article(db, run,
             url="https://www.eluniversal.com.mx/mundo/2026/08/15/sismo/",
             title="Fuerte sismo de magnitud 6.1 sacude la costa de Indonesia",
             country="MX")

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["screened"] == 1
    assert not list(db.scalars(select(Event)))
    assert "fuera del perímetro: Indonesia" in db.scalar(select(Article)).reject_reason


# ── reatribución: Venezuela contado como Nicaragua ───────────────────────
def test_pais_del_perimetro_se_reatribuye_en_vez_de_descartarse(db):
    run = _run(db)
    _article(db, run,
             url="https://www.laprensani.com/2026/08/15/internacionales/1-nota",
             title="Balacera deja tres muertos en Caracas, Venezuela",
             country="NI")

    stats = process_daily(db, run, countries_filter=["NI"])

    assert stats["reattributed"] == 1
    assert stats["screened"] == 0
    art = db.scalar(select(Article))
    assert art.country == "VE"                       # la información se conserva
    assert art.status == ArticleStatus.extracted.value


# ── el panel se limpia: los eventos de ayer se retiran ───────────────────
def test_evento_cuyos_articulos_se_rechazan_sale_del_panel(db):
    run = _run(db)
    art = _article(db, run,
                   url="https://www.elimparcial.com/columnas/2026/08/15/ay-andy/",
                   title="¡Ay Andy!",
                   country="MX")
    # evento creado ayer, antes de que existieran los filtros
    ev = Event(run_id=run.id, dedup_key="viejo-1", country="MX",
               title_es="¡Ay Andy!", summary_version="x",
               event_type="ALERTA", category="crimen_alto_impacto", level="medio",
               status=EventStatus.published.value,
               recurrence=1, independent_sources=1, has_state_media=False)
    db.add(ev)
    db.flush()
    db.add(EventArticle(event_id=ev.id, article_id=art.id))
    db.flush()

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["retired"] == 1
    db.refresh(ev)
    assert ev.status == EventStatus.discarded.value  # retirado, no borrado


def test_un_evento_legitimo_sobrevive_al_barrido(db):
    run = _run(db)
    _article(db, run)                                # asalto en Managua: pertinente
    _article(db, run,                                # segunda fuente independiente
             url="https://www.articulo66.com/2026/08/15/sucesos/2-nota",
             domain="articulo66.com", source_name="Artículo 66")

    stats = process_daily(db, run, countries_filter=["NI"])

    assert stats["screened"] == 0
    assert stats["retired"] == 0
    events = list(db.scalars(select(Event)))
    assert len(events) == 1
    assert events[0].status != EventStatus.discarded.value
