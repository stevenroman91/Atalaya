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
