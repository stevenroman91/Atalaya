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


# ── el barrido debe alcanzar los eventos fuera de la ventana ─────────────
# Fallo real en producción: los eventos del 14/08 seguían en el panel el
# 16/08. Sus artículos habían salido de la ventana de frescura, así que no
# se re-examinaban, así que nunca quedaban marcados como rechazados — y un
# barrido que buscaba «eventos con todos los artículos rechazados» no veía
# ninguno. El juicio debe recaer sobre el evento, no sobre sus artículos.

def _viejo(db, run, title, country="MX", urls=("https://x.com/nota/algo",),
           status=EventStatus.published.value) -> Event:
    ev = Event(run_id=run.id, dedup_key=f"k-{title[:20]}", country=country,
               title_es=title, summary_version="v", event_type="ALERTA",
               category="crimen_alto_impacto", level="advertencia", status=status,
               occurred_at=datetime.now(timezone.utc) - timedelta(days=2),
               recurrence=len(urls), independent_sources=1, has_state_media=False)
    db.add(ev)
    db.flush()
    for i, u in enumerate(urls):
        a = _article(db, run, url=u, title=title, country=country,
                     published_at=datetime.now(timezone.utc) - timedelta(days=2))
        db.add(EventArticle(event_id=ev.id, article_id=a.id))
    db.flush()
    return ev


def test_evento_viejo_fuera_del_perimetro_se_retira(db):
    run = _run(db)
    ev = _viejo(db, run,
                "Terremoto frente a la costa de Indonesia deja al menos 47 muertos")

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["retired"] == 1
    db.refresh(ev)
    assert ev.status == EventStatus.discarded.value
    assert "Indonesia" in ev.score_detail["retirado"]


def test_evento_viejo_de_opinion_se_retira_por_mayoria(db):
    run = _run(db)
    ev = _viejo(db, run, "¡Ay Andy!", urls=(
        "https://www.elimparcial.com/columnas/2026/08/15/ay-andy/",
        "https://www.elimparcial.com/columnas/2026/08/15/la-visa-de-andy/",
        "https://www.eluniversal.com.mx/video/podcast/andy-lopez-beltran/",
        "https://www.reforma.com/defiende-csp-a-andy/ar3258342",
    ))

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["retired"] == 1
    db.refresh(ev)
    assert ev.status == EventStatus.discarded.value


def test_evento_viejo_del_perimetro_se_reatribuye_no_se_retira(db):
    run = _run(db)
    ev = _viejo(db, run, "Balacera deja tres muertos en Caracas, Venezuela",
                country="NI")

    stats = process_daily(db, run, countries_filter=["NI"])

    assert stats["retired"] == 0
    db.refresh(ev)
    assert ev.country == "VE"
    assert ev.status == EventStatus.published.value


def test_un_evento_local_viejo_sigue_en_el_panel(db):
    run = _run(db)
    ev = _viejo(db, run, "Balacera deja dos heridos en el centro de Culiacán",
                urls=("https://www.eluniversal.com.mx/estados/balacera-culiacan/",))

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["retired"] == 0
    db.refresh(ev)
    assert ev.status == EventStatus.published.value


# ── el mapa: coordenadas de los eventos que nunca se reprocesan ──────────
def test_evento_viejo_sin_coordenadas_las_recibe(db):
    """El mapa salía vacío: las coordenadas solo se rellenaban al actualizar
    un evento, y un evento fuera de la ventana de frescura nunca se
    actualiza."""
    run = _run(db)
    ev = _viejo(db, run, "Balacera deja dos heridos en el centro de Culiacán",
                urls=("https://www.eluniversal.com.mx/estados/balacera-culiacan/",))
    assert ev.lat is None

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["geocoded"] == 1
    db.refresh(ev)
    assert ev.lat is not None and ev.lon is not None


def test_el_evento_retirado_no_se_geocodifica(db):
    run = _run(db)
    _viejo(db, run, "Terremoto frente a la costa de Indonesia deja 47 muertos")

    stats = process_daily(db, run, countries_filter=["MX"])

    assert stats["retired"] == 1
    assert stats["geocoded"] == 0     # no se gasta en lo que sale del panel


def test_evento_reatribuido_recibe_la_geo_de_su_pais_real(db):
    run = _run(db)
    ev = _viejo(db, run, "Balacera deja tres muertos en Caracas, Venezuela",
                country="NI")

    process_daily(db, run, countries_filter=["NI"])

    db.refresh(ev)
    assert ev.country == "VE"
    assert ev.lat is not None          # y no se queda huérfano del mapa


# ── el barrido debe poder lanzarse solo, sin colecta ─────────────────────
# Estaba enterrado en el tratamiento diario: corregir un filtro obligaba a
# esperar una colecta entera (media hora) para ver el efecto.

def test_el_barrido_funciona_sin_colectar(db):
    from atalaya.process.pipeline import sweep_events

    run = _run(db)
    malo = _viejo(db, run, "Terremoto en Indonesia deja al menos 47 muertos")
    bueno = _viejo(db, run, "Balacera deja dos heridos en el centro de Culiacán",
                   urls=("https://www.eluniversal.com.mx/estados/balacera-culiacan/",))

    stats = sweep_events(db)                 # ni una petición de red

    assert stats["retired"] == 1
    assert stats["geocoded"] == 1
    db.refresh(malo); db.refresh(bueno)
    assert malo.status == EventStatus.discarded.value
    assert bueno.status == EventStatus.published.value
    assert bueno.lat is not None


def test_el_barrido_es_idempotente(db):
    """Relanzarlo no retira dos veces ni deshace nada."""
    from atalaya.process.pipeline import sweep_events

    run = _run(db)
    _viejo(db, run, "Terremoto en Indonesia deja al menos 47 muertos")
    _viejo(db, run, "Balacera deja dos heridos en el centro de Culiacán",
           urls=("https://www.eluniversal.com.mx/estados/balacera-culiacan/",))

    sweep_events(db)
    segunda = sweep_events(db)

    assert segunda == {"retired": 0, "reattributed": 0, "geocoded": 0}


# ── el mapa debe seguir los filtros de la lista ──────────────────────────
# Fallo real: el mapa salía vacío en cuanto se filtraba por otro país. Solo
# leía «scope» e ignoraba el resto, así que consultaba siempre los países
# por defecto del usuario — con una cuenta seguida solo de México, filtrar
# por Brasil dejaba el mapa buscando eventos mexicanos.

def test_el_mapa_respeta_el_filtro_de_pais(db):
    import os

    from fastapi.testclient import TestClient

    from atalaya.web import auth

    run = _run(db)
    br = _viejo(db, run, "Tiroteio deixa dois feridos no centro", country="BR",
                urls=("https://g1.globo.com/rj/noticia/tiroteio-centro/",))
    br.lat, br.lon = -15.7939, -47.8828
    db.commit()

    os.environ["ATALAYA_ADMIN_EMAIL"] = "admin@example.org"
    os.environ["ATALAYA_ADMIN_PASSWORD"] = "admin-password-123"
    auth.create_admin_from_env(db)
    from atalaya.db.models import User
    admin = db.scalar(select(User).where(User.email == "admin@example.org"))
    admin.countries = ["MX"]          # el admin solo sigue México
    db.commit()

    from atalaya.web.app import app
    client = TestClient(app)
    cookie = client.post("/auth/login",
                         data={"email": "admin@example.org",
                               "password": "admin-password-123"},
                         follow_redirects=False).cookies

    sin_filtro = client.get("/dashboard/map.json", cookies=cookie).json()
    assert sin_filtro["features"] == []        # BR no está entre sus países

    con_filtro = client.get("/dashboard/map.json?country=BR", cookies=cookie).json()
    assert len(con_filtro["features"]) == 1    # el filtro manda
    assert con_filtro["features"][0]["geometry"]["coordinates"] == [-47.8828, -15.7939]
