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


# ── portada como último recurso ──────────────────────────────────────────
# Los tres grandes diarios paraguayos no publican ningún flujo: /feed, /rss
# y /rss.xml devuelven 404 mientras la portada responde 200. Sin este
# recurso, tres fuentes de la lista blanca quedaban mudas.

PORTADA_HTML = """
<html><body>
  <nav><a href="/">Inicio</a><a href="/deportes">Deportes</a></nav>
  <a href="/nacionales/2026/08/16/balacera-deja-dos-heridos-en-asuncion/">
     Balacera deja dos heridos en el centro de Asunción</a>
  <a href="/policiales/2026/08/16/asalto-a-camion-de-caudales-en-luque/">
     <h2>Asalto a camión de caudales en Luque deja un guardia herido</h2></a>
  <a href="/nacionales/2026/08/16/balacera-deja-dos-heridos-en-asuncion/">
     Balacera deja dos heridos en el centro de Asunción</a>
  <a href="/politica/">Sección política</a>
  <a href="/opinion/2026/08/16/la-columna-del-domingo/">
     La columna del domingo sobre el futuro del país</a>
  <a href="https://www.otrodiario.com/nota/2026/algo-que-paso-alla/">
     Una noticia alojada en otro dominio cualquiera</a>
  <a href="/nacionales/2026/08/16/otra-nota/">Leer</a>
</body></html>
"""


def _links():
    return Collector._article_links_from_html(
        "https://www.abc.com.py/", PORTADA_HTML, "abc.com.py")


def test_portada_extrae_articulos_con_su_titular():
    urls = dict(_links())
    assert ("https://www.abc.com.py/nacionales/2026/08/16/"
            "balacera-deja-dos-heridos-en-asuncion/") in urls
    # el titular sale del texto del enlace, con las etiquetas internas fuera
    assert urls["https://www.abc.com.py/policiales/2026/08/16/"
                "asalto-a-camion-de-caudales-en-luque/"] == (
        "Asalto a camión de caudales en Luque deja un guardia herido")


def test_portada_descarta_menus_secciones_y_dominios_ajenos():
    urls = [u for u, _ in _links()]
    assert not any(u.endswith("/politica/") for u in urls)      # índice de sección
    assert not any("otrodiario.com" in u for u in urls)         # enlace saliente
    assert not any(u.rstrip("/").endswith("/deportes") for u in urls)
    assert not any(u.endswith("/otra-nota/") for u in urls)     # «Leer»: sin titular


def test_portada_no_duplica_el_mismo_articulo():
    urls = [u for u, _ in _links()]
    assert len(urls) == len(set(urls))


def test_la_columna_de_opinion_se_recoge_pero_el_filtro_la_descarta():
    """La extracción no juzga: el filtro de sección sigue siendo el juez."""
    from atalaya.collect.whitelist import off_topic_section

    opinion = [u for u, _ in _links() if "/opinion/" in u]
    assert opinion                                   # la portada la enlaza
    assert off_topic_section(opinion[0]) == "opinion"  # y se descarta al ingerir


# ── muestras para reparar el filtro sin adivinar ─────────────────────────
# Tres portadas (ABC Color, Última Hora, Folha) dan cientos de enlaces y
# cero artículos. Sin ver la forma real de sus URL, corregir el filtro es
# adivinar — y adivinar ya costó varios despliegues inútiles.

def test_las_rutas_rechazadas_se_exponen_para_diagnostico():
    html = """
    <a href="/seccion/">Índice de una sección cualquiera del diario</a>
    <a href="/n123456">Nota con identificador numérico sin slug alguno</a>
    <a href="https://otro.com/x/y-z">Enlace saliente que no nos incumbe</a>
    <a href="/politica/2026/08/16/nota-con-slug/">Un artículo bien formado
       que sí pasa el filtro y no debe aparecer</a>
    """
    rutas = Collector._rejected_paths("https://www.abc.com.py/", html,
                                      "abc.com.py")

    assert "/seccion/" in rutas
    assert "/n123456" in rutas
    assert not any("otro.com" in r for r in rutas)       # solo el propio dominio
    assert "/politica/2026/08/16/nota-con-slug/" not in rutas  # ese sí pasó


def test_las_rutas_rechazadas_no_se_repiten():
    html = '<a href="/x/">Un enlace repetido tres veces en la portada</a>' * 3
    assert Collector._rejected_paths("https://x.com/", html, "x.com") == ["/x/"]


# ── el titular no siempre es el texto del enlace ─────────────────────────
# ABC Color y Última Hora daban cero artículos pese a enlazarlos con URL
# perfectamente válidas: sus portadas envuelven el titular en una imagen,
# así que el enlace no tiene texto propio. El título vive en `title`,
# `aria-label` o el `alt` de la imagen.

NOTA = "/deportes/2026/08/16/cerro-porteno-vs-san-lorenzo-en-la-olla/"


def _uno(html: str):
    return Collector._article_links_from_html("https://www.abc.com.py/", html,
                                              "abc.com.py")


def test_el_titular_se_recupera_del_alt_de_la_imagen():
    r = _uno(f'<a href="{NOTA}"><img src="x.jpg" '
             f'alt="Cerro Porteño vs San Lorenzo: propuesta matinal"></a>')
    assert r and r[0][1] == "Cerro Porteño vs San Lorenzo: propuesta matinal"


def test_el_titular_se_recupera_del_atributo_title():
    r = _uno(f'<a href="{NOTA}" title="Cerro Porteño vs San Lorenzo en la Olla">'
             f'<img src="x.jpg"></a>')
    assert r and "Cerro Porteño" in r[0][1]


def test_el_titular_se_recupera_de_aria_label():
    r = _uno(f'<a class="card" aria-label="Cerro Porteño vs San Lorenzo hoy en '
             f'la Olla" href="{NOTA}"><span></span></a>')
    assert r and "Cerro Porteño" in r[0][1]


def test_el_texto_del_enlace_sigue_teniendo_prioridad():
    r = _uno(f'<a href="{NOTA}" title="Etiqueta genérica del sitio web ABC">'
             f'Cerro Porteño vs San Lorenzo: propuesta matinal en la Olla</a>')
    assert r and r[0][1].startswith("Cerro Porteño vs San Lorenzo: propuesta")


def test_un_enlace_de_menu_sigue_descartado_aunque_tenga_title():
    assert _uno('<a href="/deportes" title="Deportes">Deportes</a>') == []


# ── etiquetas de accesibilidad que anteponen su función ─────────────────
# ABC Color etiqueta sus enlaces de comentarios «Enlace a comentarios para
# el artículo X». Sin recortar ese prefijo, el evento aparecía en el panel
# titulado así — ilegible para el analista.

def test_el_prefijo_de_la_etiqueta_se_recorta():
    r = _uno(f'<a href="{NOTA}" aria-label="Enlace a comentarios para el '
             f'artículo Llamativa megainversión de una firma paraguaya">'
             f'<span></span></a>')
    assert r and r[0][1] == "Llamativa megainversión de una firma paraguaya"


def test_un_titular_que_empieza_por_ver_no_se_mutila():
    """El recorte exige la palabra «artículo»: un titular normal no la trae."""
    r = _uno(f'<a href="{NOTA}">Ver crecer la violencia en Asunción preocupa '
             f'a los comerciantes</a>')
    assert r and r[0][1].startswith("Ver crecer la violencia")
