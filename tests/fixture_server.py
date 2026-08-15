"""Servidor de fixtures para tests E2E.

Reproduce en local la topología real que ve el colector:
  /{host}/{path}  →  contenido que serviría https://{host}/{path}

Sirve: flujos RSS de Google News (con enlaces codificados al estilo GN),
robots.txt y páginas de artículo HTML reales (extraíbles por trafilatura).
El PoliteFetcher se instancia con base_url_override apuntando aquí, así que
TODO el pipeline (resolución de redirecciones GN incluida) se ejecuta por
HTTP de verdad — lo único simulado es la red externa, bloqueada en sandbox.
"""
from __future__ import annotations

import base64
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def gn_link(article_url: str) -> str:
    """Enlace estilo Google News: la URL real va en un payload base64 (formato
    antiguo, el que resuelve _decode_gn_payload)."""
    payload = b'\x08\x13"' + bytes([len(article_url)]) + article_url.encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"https://news.google.com/rss/articles/{token}?oc=5"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def article_html(title: str, paragraphs: list[str], published: datetime) -> str:
    body = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    iso = published.isoformat()
    return f"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><title>{title}</title>
<meta property="og:title" content="{title}">
<meta property="article:published_time" content="{iso}">
<script type="application/ld+json">{{"@type":"NewsArticle","headline":"{title}","datePublished":"{iso}"}}</script>
</head><body>
<article><h1>{title}</h1><time datetime="{iso}">{iso}</time>
{body}
</article></body></html>"""


def rss_feed(items: list[dict]) -> str:
    entries = "".join(
        f"<item><title>{it['title']}</title><link>{it['link']}</link>"
        f"<pubDate>{format_datetime(it['published'])}</pubDate>"
        f"<source url='https://{urlparse(it.get('source_url', it['link'])).netloc}'>{it.get('source', '')}</source></item>"
        for it in items
    )
    return ("<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel>"
            f"<title>Fixture</title>{entries}</channel></rss>")


def build_default_world() -> dict[str, dict]:
    """Escenario de prueba (Guatemala):

    1. Balacera en Zona Viva — 2 fuentes independientes (Prensa Libre, Soy502)
       + 1 estatal (teleSUR), con divergencia de cifras (3 vs 5 heridos)
       → debe publicarse como ALERTA crimen_alto_impacto.
    2. Atentado con explosivos — 1 sola fuente pero gravedad extrema
       → cola «pendiente de confirmar».
    3. Congreso de seguridad vial — sin gravedad → descartado.
    4. Artículo viejo (2019) reciclado por el flujo → rechazado (§7.4).
    """
    now = _now()
    world: dict[str, dict] = {}

    art1a = ("www.prensalibre.com", "/guatemala/balacera-zona-viva")
    art1b = ("www.soy502.com", "/articulo/balacera-zona-10")
    art1c = ("www.telesurtv.net", "/news/guatemala-tiroteo")
    art2 = ("www.prensalibre.com", "/guatemala/atentado-explosivo-antigua")
    art3 = ("www.soy502.com", "/articulo/congreso-seguridad-vial")
    art4 = ("www.prensalibre.com", "/hemeroteca/robo-2019")

    world[f"/{art1a[0]}{art1a[1]}"] = article_html(
        "Balacera en la Zona Viva deja al menos 3 heridos en Ciudad de Guatemala",
        ["Una balacera registrada la noche del jueves en la Zona Viva, zona 10 de "
         "Ciudad de Guatemala, dejó al menos 3 heridos, informaron los Bomberos "
         "Voluntarios.",
         "Testigos indicaron que hombres armados dispararon contra un restaurante "
         "de la 13 calle, en un ataque que la Policía Nacional Civil investiga "
         "como un posible caso de extorsión.",
         "Los heridos fueron trasladados a hospitales privados de la capital y se "
         "reporta la captura de un sospechoso.",
         "La zona 10 concentra hoteles, restaurantes y sedes diplomáticas, por lo "
         "que las autoridades reforzaron la seguridad en el sector."],
        now - timedelta(hours=5))
    world[f"/{art1b[0]}{art1b[1]}"] = article_html(
        "Ataque armado en zona 10: balacera en la Zona Viva de la capital deja 5 heridos",
        ["Un ataque armado ocurrido en la Zona Viva, zona 10 capitalina, dejó 5 "
         "heridos según el conteo preliminar de los socorristas.",
         "La balacera ocurrió frente a un restaurante de la 13 calle; los "
         "responsables huyeron en motocicleta, señalaron testigos.",
         "La Policía Nacional Civil acordonó el área y analiza las cámaras del "
         "sector para identificar a los atacantes.",
         "Comercios de la zona cerraron temporalmente mientras se realizaban las "
         "diligencias."],
        now - timedelta(hours=4))
    world[f"/{art1c[0]}{art1c[1]}"] = article_html(
        "Tiroteo en zona exclusiva de Ciudad de Guatemala deja varios heridos",
        ["Un tiroteo en la denominada Zona Viva de Ciudad de Guatemala dejó varias "
         "personas heridas este jueves, según medios locales.",
         "Las autoridades guatemaltecas investigan el móvil del ataque armado "
         "ocurrido en la zona 10 de la capital."],
        now - timedelta(hours=3))
    world[f"/{art2[0]}{art2[1]}"] = article_html(
        "Atentado con explosivos daña sede municipal en Antigua Guatemala",
        ["Un atentado con un artefacto explosivo dañó la madrugada de este viernes "
         "la fachada de un edificio municipal en Antigua Guatemala, sin víctimas.",
         "El Ministerio Público procesa la escena y no se descarta ninguna "
         "hipótesis, indicó la vocería institucional.",
         "Es el primer ataque de este tipo en la ciudad colonial en más de una "
         "década."],
        now - timedelta(hours=6))
    world[f"/{art3[0]}{art3[1]}"] = article_html(
        "Congreso internacional de seguridad vial se celebra en Ciudad de Guatemala",
        ["El congreso reúne a especialistas de la región para discutir políticas "
         "de seguridad vial y reducción de siniestros de tránsito.",
         "Las ponencias abordan la infraestructura urbana y la educación de "
         "conductores en la Ciudad de Guatemala."],
        now - timedelta(hours=8))
    world[f"/{art4[0]}{art4[1]}"] = article_html(
        "Robo a mano armada en zona 1 deja un herido",
        ["Un asalto a mano armada ocurrido en la zona 1 dejó un herido, informó "
         "la Policía Nacional Civil.",
         "El hecho ocurrió frente al Mercado Central de la capital."],
        datetime(2019, 5, 4, 12, 0, tzinfo=timezone.utc))

    def full(host_path: tuple[str, str]) -> str:
        return f"https://{host_path[0]}{host_path[1]}"

    items = [
        {"title": "Balacera en la Zona Viva deja al menos 3 heridos en Ciudad de Guatemala",
         "link": gn_link(full(art1a)), "published": now - timedelta(hours=5),
         "source": "Prensa Libre", "source_url": full(art1a)},
        {"title": "Ataque armado en zona 10: balacera en la Zona Viva de la capital deja 5 heridos",
         "link": gn_link(full(art1b)), "published": now - timedelta(hours=4),
         "source": "Soy502", "source_url": full(art1b)},
        {"title": "Tiroteo en zona exclusiva de Ciudad de Guatemala deja varios heridos",
         "link": gn_link(full(art1c)), "published": now - timedelta(hours=3),
         "source": "teleSUR", "source_url": full(art1c)},
        {"title": "Atentado con explosivos daña sede municipal en Antigua Guatemala",
         "link": gn_link(full(art2)), "published": now - timedelta(hours=6),
         "source": "Prensa Libre", "source_url": full(art2)},
        {"title": "Congreso internacional de seguridad vial se celebra en Ciudad de Guatemala",
         "link": gn_link(full(art3)), "published": now - timedelta(hours=8),
         "source": "Soy502", "source_url": full(art3)},
        {"title": "Robo a mano armada en zona 1 deja un herido",
         "link": gn_link(full(art4)), "published": now - timedelta(hours=2),  # el flujo miente: recicla un artículo de 2019
         "source": "Prensa Libre", "source_url": full(art4)},
    ]
    world["__gn_rss__"] = rss_feed(items)
    return world


class FixtureHandler(BaseHTTPRequestHandler):
    world: dict[str, dict] = {}

    def log_message(self, *args):  # silencio
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        # path = /{host}/{resto}
        parts = path.lstrip("/").split("/", 1)
        host = parts[0] if parts else ""
        rest = "/" + (parts[1] if len(parts) > 1 else "")

        if rest == "/robots.txt":
            self._send(200, "User-agent: *\nAllow: /\n", "text/plain")
        elif host == "news.google.com" and rest.startswith("/rss/search"):
            self._send(200, self.world["__gn_rss__"], "application/rss+xml")
        elif f"/{host}{rest}" in self.world:
            self._send(200, self.world[f"/{host}{rest}"], "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def _send(self, code: int, body: str, ctype: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_fixture_server(world: dict | None = None) -> tuple[ThreadingHTTPServer, str]:
    FixtureHandler.world = world or build_default_world()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    server, base = start_fixture_server()
    print(f"Fixture server en {base} — Ctrl+C para parar")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
