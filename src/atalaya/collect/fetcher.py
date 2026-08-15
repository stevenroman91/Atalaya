"""Cliente HTTP «educado»: respeta robots.txt, aplica un retraso por host y se
identifica con un User-Agent propio (§9). Resuelve las redirecciones de
Google News hacia la URL canónica del artículo — nunca se adivina una URL.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from atalaya.config import load_schedule

log = logging.getLogger(__name__)


class PoliteFetcher:
    def __init__(self, base_url_override: str | None = None):
        cfg = load_schedule().get("collector", {})
        self.delay = float(cfg.get("request_delay_seconds", 1.5))
        self.timeout = float(cfg.get("timeout_seconds", 20))
        self.user_agent = cfg.get("user_agent", "AtalayaBot/1.0")
        # Uso concurrente (colecta paralela): httpx.Client es thread-safe;
        # los diccionarios de estado se protegen con locks.
        self._last_request: dict[str, float] = {}
        self._rate_lock = threading.Lock()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        # En tests, todas las peticiones se reescriben hacia el servidor de fixtures.
        self.base_url_override = base_url_override
        self.client = httpx.Client(
            headers={
                "User-Agent": self.user_agent,  # identificable (§9), sin disfraz
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-419,es;q=0.9,pt-BR;q=0.8,en;q=0.5",
            },
            timeout=self.timeout,
            follow_redirects=True,
        )

    # ── robots.txt ───────────────────────────────────────────────────────
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        host = urlparse(url).netloc
        with self._robots_lock:
            if host in self._robots:
                return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        try:
            robots_url = self._rewrite(f"https://{host}/robots.txt")
            resp = self.client.get(robots_url)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # sin robots.txt → permitido
        except Exception:
            rp = None
        with self._robots_lock:
            self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # ── rate limiting por host ───────────────────────────────────────────
    def _wait_politely(self, url: str) -> None:
        """Reserva un turno para el host y espera hasta que llegue.

        La reserva (lectura + actualización de _last_request) es atómica, y
        la espera ocurre fuera del lock: N hilos que golpean el mismo host
        quedan espaciados self.delay segundos entre sí, mientras que hosts
        distintos avanzan en paralelo sin bloquearse.
        """
        host = urlparse(url).netloc
        with self._rate_lock:
            now = time.monotonic()
            slot = max(now, self._last_request.get(host, 0.0) + self.delay)
            self._last_request[host] = slot
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _rewrite(self, url: str) -> str:
        if self.base_url_override:
            p = urlparse(url)
            return f"{self.base_url_override}/{p.netloc}{p.path}" + (f"?{p.query}" if p.query else "")
        return url

    # ── API ──────────────────────────────────────────────────────────────
    def get(self, url: str, check_robots: bool = True) -> httpx.Response | None:
        if not url.startswith(("http://", "https://")):
            log.warning("URL no absoluta rechazada: %r", url)
            return None
        if check_robots and not self.allowed(url):
            log.info("robots.txt prohíbe %s", url)
            return None
        self._wait_politely(url)
        try:
            resp = self.client.get(self._rewrite(url))
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            log.warning("fetch falló %s: %s", url, exc)
            return None

    def resolve_google_news_url(self, gn_url: str) -> str | None:
        """Devuelve la URL real del artículo detrás de un enlace de Google News.

        1) Formato antiguo: la URL viene codificada en base64 dentro del path.
        2) Si no, se siguen las redirecciones HTTP.
        Si nada funciona, devuelve None — el artículo se descarta antes que
        inventar una URL (§7.3).
        """
        decoded = _decode_gn_payload(gn_url)
        if decoded:
            return decoded
        self._wait_politely(gn_url)
        try:
            resp = self.client.get(self._rewrite(gn_url))
            final = str(resp.url)
            if "news.google.com" not in urlparse(final).netloc:
                return final
        except httpx.HTTPError:
            pass
        return None

    def close(self) -> None:
        self.client.close()


_GN_ARTICLE_RE = re.compile(r"news\.google\.com/(?:rss/)?articles/([^?/]+)")


def _decode_gn_payload(gn_url: str) -> str | None:
    """Los enlaces antiguos de Google News (`CBMi…`) llevan la URL del artículo
    en un protobuf base64. Extraemos la primera URL http(s) legible; si el
    payload es del formato nuevo (opaco), devolvemos None."""
    m = _GN_ARTICLE_RE.search(gn_url)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    urls = re.findall(rb"https?://[^\x00-\x1f\x7f-\xff\"']+", raw)
    for u in urls:
        try:
            text = u.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "google.com" not in text:
            return text.rstrip("\\")
    return None
