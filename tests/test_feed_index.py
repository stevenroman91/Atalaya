"""Índices HTML de flujos: extracción de candidatos e ingesta derivada.

Varios diarios (La Jornada, Milenio…) publican en /rss una página HTML que
enlaza sus flujos por sección en vez de un feed. El colector debe seguir
esos enlaces — extraídos de la propia página, nunca inventados — y no darse
por consultado si no llega a parsear ningún flujo real.
"""
from atalaya.collect.collector import Collector

INDEX_HTML = """
<html><head>
  <link rel="alternate" type="application/rss+xml" href="/rss/edicion.xml">
</head><body>
  <a href="/rss/politica.xml">Política</a>
  <a href="rss/estados.xml">Estados</a>
  <a href="https://www.jornada.com.mx/rss/capital.xml">Capital</a>
  <a href="/rss/politica.xml">Política (repetido)</a>
  <a href="/category/mundo">Mundo</a>
  <a href="//cdn.jornada.com.mx/feed/deportes.xml">Deportes</a>
</body></html>
"""


def test_feed_links_from_html_absolutiza_y_deduplica():
    links = Collector._feed_links_from_html(
        "https://www.jornada.com.mx/rss/", INDEX_HTML)

    assert links == [
        "https://www.jornada.com.mx/rss/edicion.xml",
        "https://www.jornada.com.mx/rss/politica.xml",
        "https://www.jornada.com.mx/rss/rss/estados.xml",
        "https://www.jornada.com.mx/rss/capital.xml",
        "https://cdn.jornada.com.mx/feed/deportes.xml",
    ]


def test_feed_links_ignora_paginas_normales():
    html = '<a href="/politica/nota-123">Nota</a><a href="/contacto">Contacto</a>'
    assert Collector._feed_links_from_html("https://x.com/rss", html) == []


def test_indice_sin_candidatos_no_ingiere(db, fixture_base, monkeypatch):
    from atalaya.collect.fetcher import PoliteFetcher

    f = PoliteFetcher(base_url_override=fixture_base)
    f.delay = 0.0
    f.host_delays = {}
    col = Collector(db, f)
    called: list[str] = []
    monkeypatch.setattr(Collector, "ingest_feed",
                        lambda self, url, **kw: called.append(url) or 0)

    stored = col._ingest_feed_index(
        "https://x.com/rss", "<html><a href='/nota'>x</a></html>",
        run=None, country=None, zone=None, keyword=None, theme=None,
        window_hours=26, max_entries=None)

    assert stored == 0
    assert called == []


def test_indice_respeta_el_tope_de_flujos(db, fixture_base, monkeypatch):
    from atalaya.collect.fetcher import PoliteFetcher

    f = PoliteFetcher(base_url_override=fixture_base)
    f.delay = 0.0
    f.host_delays = {}
    col = Collector(db, f)
    html = "".join(f'<a href="/rss/s{i}.xml">s</a>' for i in range(20))
    called: list[str] = []
    monkeypatch.setattr(Collector, "ingest_feed",
                        lambda self, url, **kw: called.append(url) or 1)

    stored = col._ingest_feed_index(
        "https://x.com/rss", html, run=None, country=None, zone=None,
        keyword=None, theme=None, window_hours=26, max_entries=None)

    assert len(called) == Collector._MAX_INDEX_FEEDS
    assert stored == Collector._MAX_INDEX_FEEDS


def test_texto_de_content_encoded_cuando_el_sitio_bloquea():
    """Si la página del artículo no se puede leer, vale el texto del flujo."""
    from atalaya.collect.extract import text_from_feed_html

    entry = {"content": [{"value": (
        "<p>Los senadores estadounidenses Ted Cruz y Timothy Michael Kaine "
        "propusieron incluir sanciones al oro nicaragüense y al Instituto de "
        "Previsión Social Militar del Ejército.</p>"
        "<p>Gracias a las alzas históricas del precio del oro, se consolidó "
        "como el principal producto de exportación de Nicaragua.</p>")}]}

    html = Collector._entry_html(entry)
    text = text_from_feed_html(html)

    assert text and "Ted Cruz" in text
    assert "Previsión Social Militar" in text
    assert "<p>" not in text


def test_entrada_sin_content_no_inventa_texto():
    # description es un teaser, no el artículo: no debe pasar por texto íntegro
    assert Collector._entry_html({"summary": "Un resumen corto"}) == ""
    assert Collector._entry_html({}) == ""
