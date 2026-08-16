"""Parseo de las API abiertas (GDELT, USGS, GDACS).

Estas pruebas trabajan sobre cargas útiles fijas, no sobre la red: el
entorno de desarrollo no tiene salida a internet, así que la única prueba
del punto de entrada real ocurre desde producción con el botón «Probar las
API». Aquí se comprueba lo que sí se puede comprobar sin red: que el
parseo, los umbrales y la atribución de país hacen lo que dicen.
"""
import json

from atalaya.collect.apis import (
    country_from_place, gdelt_query, parse_gdacs, parse_gdelt, parse_usgs,
)

GDELT = json.dumps({"articles": [
    {"url": "https://www.elsoldemexico.com.mx/nota-1", "title": "Balacera en Zacatecas deja dos heridos",
     "seendate": "20260816T103000Z", "domain": "elsoldemexico.com.mx"},
    {"url": "", "title": "sin url", "seendate": "20260816T103000Z"},
]})

USGS = json.dumps({"features": [
    {"properties": {"mag": 5.4, "place": "20 km SSE of Ciudad Hidalgo, Mexico",
                    "time": 1786000000000, "url": "https://earthquake.usgs.gov/ev/1",
                    "title": "M 5.4 - 20 km SSE of Ciudad Hidalgo, Mexico"},
     "geometry": {"coordinates": [-92.14, 14.68, 35.0]}},
    {"properties": {"mag": 2.9, "place": "10 km N of Managua, Nicaragua",
                    "time": 1786000000000, "url": "https://earthquake.usgs.gov/ev/2",
                    "title": "M 2.9"},
     "geometry": {"coordinates": [-86.2, 12.1, 10.0]}},
    {"properties": {"mag": 6.1, "place": "80 km W of Tokyo, Japan",
                    "time": 1786000000000, "url": "https://earthquake.usgs.gov/ev/3",
                    "title": "M 6.1"},
     "geometry": {"coordinates": [139.0, 35.0, 30.0]}},
]})

GDACS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:gdacs="http://www.gdacs.org"
     xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#">
<channel>
  <item>
    <title>Tropical Cyclone alert for Mexico</title>
    <link>https://www.gdacs.org/report.aspx?eventid=1</link>
    <description>Orange alert for tropical cyclone in Mexico.</description>
    <pubDate>Sat, 15 Aug 2026 12:00:00 GMT</pubDate>
    <gdacs:alertlevel>Orange</gdacs:alertlevel>
    <gdacs:country>Mexico</gdacs:country>
    <geo:lat>19.4</geo:lat><geo:long>-99.1</geo:long>
  </item>
  <item>
    <title>Flood alert for Guatemala</title>
    <link>https://www.gdacs.org/report.aspx?eventid=2</link>
    <description>Green alert.</description>
    <pubDate>Sat, 15 Aug 2026 12:00:00 GMT</pubDate>
    <gdacs:alertlevel>Green</gdacs:alertlevel>
    <gdacs:country>Guatemala</gdacs:country>
  </item>
  <item>
    <title>Earthquake alert for Japan</title>
    <link>https://www.gdacs.org/report.aspx?eventid=3</link>
    <pubDate>Sat, 15 Aug 2026 12:00:00 GMT</pubDate>
    <gdacs:alertlevel>Red</gdacs:alertlevel>
    <gdacs:country>Japan</gdacs:country>
  </item>
</channel></rss>"""


def test_gdelt_devuelve_entradas_con_forma_de_flujo():
    entries = parse_gdelt(json.loads(GDELT))
    assert len(entries) == 1                       # la que no tiene URL se cae
    e = entries[0]
    assert e["link"].startswith("https://")
    assert e["published"].startswith("2026-08-16T10:30:00")


def test_gdelt_no_filtra_por_pais_del_medio():
    """`sourcecountry:` filtra por el país del MEDIO — justo el error que
    llevamos toda la semana corrigiendo."""
    q = gdelt_query("México", ["homicidio", "balacera"])
    assert "sourcecountry" not in q
    assert '"México"' in q


def test_gdelt_se_limita_a_la_lengua_del_pais():
    """Primera prueba real sin este filtro: «Head to Head Analysis : FIBRA
    Macquarie México & Its Rivals» — boletines bursátiles en inglés que
    nombran México de pasada. Ni prensa local, ni resumible en español."""
    assert "sourcelang:spanish" in gdelt_query("México", ["homicidio"], "es")
    assert "sourcelang:portuguese" in gdelt_query("Brasil", ["homicídio"], "pt")


def test_usgs_respeta_el_umbral_y_el_perimetro():
    items = parse_usgs(json.loads(USGS), min_magnitude=4.0)
    assert len(items) == 1                         # el 2.9 y el de Japón fuera
    it = items[0]
    assert it.country == "MX"
    assert (it.lat, it.lon) == (14.68, -92.14)     # coordenadas exactas
    assert "magnitud 5.4" in it.text
    assert "35.0 km" in it.text                    # profundidad del boletín


def test_gdacs_filtra_por_nivel_y_por_perimetro():
    items = parse_gdacs(GDACS, min_level="orange")
    assert [i.country for i in items] == ["MX"]    # verde fuera, Japón fuera
    assert items[0].lat == 19.4


def test_el_pais_sale_del_toponimo_no_del_gentilicio():
    assert country_from_place("20 km SSE of Ciudad Hidalgo, Mexico") == "MX"
    assert country_from_place("80 km W of Tokyo, Japan") is None
    assert country_from_place("") is None


# ── la coordenada oficial debe llegar hasta el mapa ──────────────────────
def test_las_coordenadas_de_la_fuente_ganan_al_centroide(db):
    """Sin esto, USGS no sirve de nada para el mapa: el marcador seguiría
    en la capital del país aunque el sismo esté a 300 km."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from atalaya.db.models import Article, ArticleStatus, CollectRun, Event
    from atalaya.process.pipeline import process_daily

    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    ahora = datetime.now(timezone.utc) - timedelta(hours=2)
    for i, dom in enumerate(("earthquake.usgs.gov", "eluniversal.com.mx")):
        db.add(Article(
            run_id=run.id, url=f"https://{dom}/sismo-{i}", domain=dom,
            source_name=dom, source_type="oficial" if i == 0 else "independiente",
            title="Sismo de magnitud 5.4 sacude Ciudad Hidalgo",
            text="Sismo de magnitud 5.4 registrado en Ciudad Hidalgo. "
                 "Se reportan al menos tres heridos y daños en viviendas. "
                 "Protección Civil evalúa los derrumbes en la zona.",
            published_at=ahora, country="MX", lang="es",
            lat=14.68 if i == 0 else None, lon=-92.14 if i == 0 else None,
            status=ArticleStatus.extracted.value))
    db.flush()

    process_daily(db, run, countries_filter=["MX"])

    ev = db.scalar(select(Event))
    assert ev is not None
    assert (ev.lat, ev.lon) == (14.68, -92.14)


# ── el motivo del fallo, no «no se pudo» ─────────────────────────────────
# Log de producción: latribuna.hn 403, laprensagrafica.com prohibida por
# robots.txt, 100noticias.com.ni no resuelve, diarioelmundo.com con el
# certificado roto. Cuatro causas, cuatro acciones distintas — y una de
# ellas, robots.txt, no pide ninguna acción: se acata.

def test_cada_causa_de_fallo_tiene_su_estado():
    from atalaya.web.routes.coverage import _causa

    assert _causa("sin flujo; robots.txt del sitio nos lo prohíbe") == "robots"
    assert _causa("sin flujo; el sitio nos responde 403") == "bloqueada"
    assert _causa("sin flujo; el dominio no resuelve — ¿cambió de nombre?") == "dns"
    assert _causa("sin flujo; certificado del sitio inválido (no lo saltamos)") == "tls"
    assert _causa("sin flujo; portada sin artículos legibles") == "inalcanzable"
    assert _causa(None) == "inalcanzable"


def test_robots_txt_no_cuenta_como_a_revisar_a_mano():
    """No se arregla: se acata. Contarlo inflaba la lista de tareas con
    fuentes sobre las que no hay nada que decidir."""
    from atalaya.web.routes.coverage import ACCIONABLE

    assert "robots" not in ACCIONABLE
    assert "bloqueada" not in ACCIONABLE      # tampoco: no nos disfrazamos
    assert "dns" in ACCIONABLE                # eso sí: config a corregir


def test_el_fetcher_dice_por_que_falló():
    from atalaya.collect.fetcher import _classify_transport

    assert _classify_transport(Exception("[Errno -2] Name or service not known"))[0] == "dns"
    assert _classify_transport(Exception("CERTIFICATE_VERIFY_FAILED"))[0] == "tls"


# ── reintentar lo transitorio, jamás lo que es una respuesta ─────────────
# GDELT corta la conexión sin responder de vez en cuando («Server
# disconnected without sending a response»): dos diagnósticos seguidos, dos
# resultados distintos. Eso se reintenta. Un 403 o un robots.txt no: no son
# incidentes, son respuestas, e insistir sería justamente lo que no hacemos.

def test_la_conexion_cortada_se_considera_transitoria():
    from atalaya.collect.fetcher import _classify_transport

    clave, _ = _classify_transport(
        Exception("Server disconnected without sending a response."))
    assert clave == "transitoria"


def test_solo_se_reintenta_lo_transitorio():
    from atalaya.collect.fetcher import PoliteFetcher

    assert "transitoria" in PoliteFetcher._REINTENTABLES
    assert "timeout" in PoliteFetcher._REINTENTABLES
    assert "bloqueada" not in PoliteFetcher._REINTENTABLES   # un 403 es una respuesta
    assert "robots" not in PoliteFetcher._REINTENTABLES      # y eso, una prohibición
    # El 429 estuvo aquí y salió: es una petición explícita de bajar el
    # ritmo. Reintentar dentro de la misma ventana la prolonga en vez de
    # resolverla — GDELT nos lo repitió cuatro veces seguidas.
    assert "sobrecarga" not in PoliteFetcher._REINTENTABLES


def test_el_reintento_acaba_devolviendo_la_respuesta():
    """Un corte seguido de un éxito no debe dejar rastro de fallo."""
    import httpx

    from atalaya.collect.fetcher import PoliteFetcher

    f = PoliteFetcher()
    f.delay = 0.0
    f.allowed = lambda url: True
    intentos = {"n": 0}

    def get(url):
        intentos["n"] += 1
        if intentos["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    f.client.get = get
    resp = f.get("https://api.gdeltproject.org/x", retries=2)

    assert resp is not None and intentos["n"] == 2
    assert f.last_failure is None


# ── un solo diagnóstico a la vez ─────────────────────────────────────────
# Log de producción: dos clics en «Probar las API» con 20 s de diferencia →
# dos hilos, cada uno con su propio fetcher, cada uno respetando el retardo
# de cortesía por su cuenta e ignorando al otro. Juntos martillearon GDELT,
# que respondió 429. El 429 no venía de su API: venía de nosotros dos veces.

def test_dos_diagnosticos_a_la_vez_no_se_lanzan():
    import threading
    import time

    from atalaya.web.routes import admin_routes

    soltar = threading.Event()
    admin_routes._PROBE_LOCK.acquire()

    def liberar():
        soltar.wait(5)
        admin_routes._PROBE_LOCK.release()

    threading.Thread(target=liberar, daemon=True).start()
    try:
        # con el cerrojo tomado, un segundo lanzamiento se niega en vez de
        # duplicar las peticiones a espaldas de quien las recibe
        assert admin_routes._probe_apis_in_background() is False
        assert admin_routes._probe_all_in_background() is False
    finally:
        soltar.set()
        time.sleep(0.05)


def test_el_cerrojo_se_suelta_aunque_el_hilo_reviente():
    """Un hilo que muere con el cerrojo en la mano deja el botón muerto
    hasta el próximo despliegue."""
    from atalaya.web.routes import admin_routes

    admin_routes._PROBE_LOCK.acquire()

    def explota():
        raise RuntimeError("boom")

    admin_routes._releasing(explota)()

    assert admin_routes._PROBE_LOCK.acquire(blocking=False) is True
    admin_routes._PROBE_LOCK.release()


# ── ReliefWeb y NOAA/NHC ─────────────────────────────────────────────────
RELIEFWEB = json.dumps({"data": [
    {"fields": {"name": "Guatemala: Inundaciones - Ago 2026", "status": "alert",
                "url": "https://reliefweb.int/disaster/fl-2026-gtm",
                "date": {"created": "2026-08-15T10:00:00+00:00"},
                "country": [{"name": "Guatemala"}],
                "type": [{"name": "Flood"}]}},
    {"fields": {"name": "Philippines: Typhoon - 2026", "status": "alert",
                "url": "https://reliefweb.int/disaster/tc-2026-phl",
                "date": {"created": "2026-08-15T10:00:00+00:00"},
                "country": [{"name": "Philippines"}]}},
    {"fields": {"name": "Mexico: Sismo - 2019", "status": "past",
                "url": "https://reliefweb.int/disaster/eq-2019-mex",
                "date": {"created": "2019-09-19T10:00:00+00:00"},
                "country": [{"name": "Mexico"}]}},
]})

NHC = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Tropical Storm Warning for the coast of Mexico</title>
    <link>https://www.nhc.noaa.gov/ep1.shtml</link>
    <description>A tropical storm warning is in effect for Mexico.</description>
    <pubDate>Sat, 15 Aug 2026 15:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Hurricane approaching Hawaii</title>
    <link>https://www.nhc.noaa.gov/ep2.shtml</link>
    <description>No land areas of interest.</description>
    <pubDate>Sat, 15 Aug 2026 15:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_reliefweb_descarta_lo_pasado_y_lo_de_fuera():
    from atalaya.collect.apis import parse_reliefweb

    items = parse_reliefweb(json.loads(RELIEFWEB))
    assert [i.country for i in items] == ["GT"]     # Filipinas fuera, 2019 cerrado
    assert "Flood" in items[0].text


def test_nhc_solo_conserva_lo_que_toca_el_perimetro():
    """El Pacífico oriental cubre mucho más que nosotros: un huracán sobre
    Hawái no cambia la conducta de nadie en Guatemala."""
    from atalaya.collect.apis import parse_nhc

    items = parse_nhc(NHC)
    assert [i.country for i in items] == ["MX"]


def test_reliefweb_lleva_su_appname_obligatorio():
    from atalaya.collect.apis import api_url

    url = api_url({"kind": "reliefweb_json", "url": "https://api.reliefweb.int/v1/disasters",
                   "appname": "atalaya-vigilancia", "limit": 40})
    assert "appname=atalaya-vigilancia" in url and "limit=40" in url


def test_el_diagnostico_y_la_colecta_usan_el_mismo_despacho():
    """Probar algo distinto de lo que se usará no prueba nada: ya nos pasó
    con la consulta simplificada de GDELT."""
    from atalaya.collect.apis import _OFICIALES
    from atalaya.config import load_apis

    for key, cfg in load_apis().items():
        assert cfg["kind"] == "gdelt_doc" or cfg["kind"] in _OFICIALES, key


# ── el filtro de lengua de GDELT, editable sin tocar código ──────────────
# Su sintaxis no se puede verificar desde el entorno de desarrollo, y una
# consulta mal formada la rechaza con un texto en claro, no con JSON. Vive
# en apis.yaml para poder corregirla sin un despliegue de código.

def test_el_token_de_lengua_viene_de_config():
    q = gdelt_query("México", ["homicidio"], "es", {"es": "spa"})
    assert "sourcelang:spa" in q


def test_sin_mapa_de_lengua_no_hay_filtro():
    """Vaciar source_langs quita el filtro en vez de romper la consulta."""
    q = gdelt_query("México", ["homicidio"], "es", {})
    assert "sourcelang" not in q
    assert '"México"' in q


def test_reliefweb_esta_apagada_por_robots_txt():
    """No se negocia y no se sortea: la prueba real devolvió «robots.txt del
    sitio nos lo prohíbe». Reactivarla exige una confirmación de OCHA, no un
    cambio de código."""
    from atalaya.config import load_apis

    assert load_apis()["reliefweb"]["enabled"] is False


def test_gdelt_descarta_los_terminos_demasiado_cortos():
    """Prueba real: «The specified phrase is too short.» — GDELT rechaza la
    consulta ENTERA si un solo término baja de cinco caracteres. El culpable
    era «robo». No se toca keywords.yaml: en Google News «robo» funciona."""
    q = gdelt_query("México", ["homicidio", "robo", "balacera"])

    assert '"robo"' not in q
    assert '"homicidio"' in q and '"balacera"' in q


def test_sin_ningun_termino_valido_la_consulta_sigue_siendo_valida():
    q = gdelt_query("México", ["robo", "ola"])

    assert q == '"México"'          # sin paréntesis vacíos que la rompan


def test_ningun_termino_de_gdelt_es_demasiado_corto():
    """Guardia sobre la config real: añadir «robo» a keywords.yaml no debe
    volver a tumbar GDELT sin que nadie lo vea."""
    from atalaya.collect.apis import _GDELT_MIN_TERM
    from atalaya.config import load_keywords

    for lang in ("es", "pt"):
        q = gdelt_query("X", load_keywords()["daily"][lang], lang)
        for termino in q.split('"')[1::2]:
            assert termino == "X" or len(termino) >= _GDELT_MIN_TERM, termino


# ── el encadenamiento completo, no solo el parseo ────────────────────────
# Hasta aquí las pruebas cubrían los parseadores y el diagnóstico probaba
# los puntos de entrada. Faltaba lo del medio: que una colecta real llame a
# las API, ingiera lo que devuelven y lo lleve hasta el evento. Un parser
# correcto conectado a nada no sirve de nada.

class _RespuestaFalsa:
    def __init__(self, texto="", datos=None):
        self.text = texto
        self._datos = datos
        self.url = "https://x.test/"

    def json(self):
        if self._datos is None:
            raise ValueError("no es JSON")
        return self._datos


class _FetcherFalso:
    """Devuelve la carga útil que corresponda a cada URL. Ninguna red."""

    def __init__(self, por_url):
        self.por_url = por_url
        self.last_failure = None
        self.pedidas = []

    def get(self, url, check_robots=True, retries=0):
        self.pedidas.append(url)
        for fragmento, resp in self.por_url.items():
            if fragmento in url:
                return resp
        return None

    def resolve_google_news_url(self, url):
        return None


def _marcar_probada(db, dominio, nombre):
    """El cerrojo: sin prueba superada el colector ignora la API."""
    from datetime import datetime, timezone

    from atalaya.db.models import SourceRecord

    db.add(SourceRecord(domain=dominio, name=nombre,
                        last_ok_at=datetime.now(timezone.utc)))
    db.commit()


def test_una_colecta_ingiere_los_boletines_oficiales(db):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from atalaya.collect.collector import Collector
    from atalaya.db.models import Article, CollectRun

    ahora = int(datetime.now(timezone.utc).timestamp() * 1000)
    usgs = {"features": [{
        "properties": {"mag": 5.4, "place": "20 km SSE of Ciudad Hidalgo, Mexico",
                       "time": ahora, "url": "https://earthquake.usgs.gov/ev/9",
                       "title": "M 5.4 - 20 km SSE of Ciudad Hidalgo, Mexico"},
        "geometry": {"coordinates": [-92.14, 14.68, 35.0]}}]}

    _marcar_probada(db, "earthquake.usgs.gov", "USGS")
    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()

    col = Collector(db, fetcher=_FetcherFalso({
        "earthquake.usgs.gov": _RespuestaFalsa(datos=usgs)}))
    col._daily_official(run, window=26.0)
    db.commit()

    art = db.scalar(select(Article).where(Article.domain == "earthquake.usgs.gov"))
    assert art is not None
    assert art.country == "MX"
    assert (art.lat, art.lon) == (14.68, -92.14)   # coordenadas exactas
    assert art.source_type == "oficial"


def test_sin_prueba_superada_la_api_no_se_toca(db):
    """El cerrojo de §4: nada entra en base porque una URL parezca correcta."""
    from datetime import datetime, timezone

    from atalaya.collect.collector import Collector
    from atalaya.db.models import CollectRun

    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()

    fetcher = _FetcherFalso({})
    Collector(db, fetcher=fetcher)._daily_official(run, window=26.0)

    assert fetcher.pedidas == []      # ni una petición


def test_gdelt_pasa_por_el_mismo_filtro_que_google_news(db):
    """No aporta texto, aporta URLs: deben cruzar el filtro de sección como
    cualquier otra entrada, sin excepción escrita para GDELT."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from atalaya.collect.collector import Collector
    from atalaya.config import load_countries
    from atalaya.db.models import Article, CollectRun, Reject

    payload = {"articles": [
        {"url": "https://www.eluniversal.com.mx/deportes/chivas-gana/",
         "title": "Chivas gana el clásico nacional",
         "seendate": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")},
    ]}

    _marcar_probada(db, "api.gdeltproject.org", "GDELT 2.0")
    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()

    col = Collector(db, fetcher=_FetcherFalso({
        "api.gdeltproject.org": _RespuestaFalsa(datos=payload)}))
    col._daily_gdelt(run, load_countries()["MX"], window=26.0)
    db.commit()

    assert db.scalar(select(Article)) is None            # deportes: fuera
    rej = db.scalar(select(Reject))
    assert rej is not None and "deportes" in rej.reason  # y con su motivo
