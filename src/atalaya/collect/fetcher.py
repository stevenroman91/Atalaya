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
        # excepciones por host: p. ej. news.google.com soporta un ritmo mucho
        # mayor que un pequeño diario local — la cortesía se calibra por host
        self.host_delays = {str(h): float(d)
                            for h, d in (cfg.get("per_host_delay_seconds") or {}).items()}
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
        # causa del último fallo: (clave, texto legible) o None
        self.last_failure: tuple[str, str] | None = None
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
        delay = self.host_delays.get(host, self.delay)
        with self._rate_lock:
            now = time.monotonic()
            slot = max(now, self._last_request.get(host, 0.0) + delay)
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
    # Fallos que merecen otro intento: el servidor cortó o tardó.
    #
    # El 429 NO está aquí, aunque lo estuvo. Un 429 es una petición explícita
    # de bajar el ritmo: reintentar dentro de la misma ventana la prolonga en
    # vez de resolverla — GDELT nos lo repitió cuatro veces seguidas. Se
    # acata y se vuelve más tarde. Un 403 y un robots.txt tampoco: no son
    # incidentes, son respuestas, e insistir sería justamente lo que no
    # hacemos.
    _REINTENTABLES = ("transitoria", "timeout")

    def get(self, url: str, check_robots: bool = True,
            retries: int = 0) -> httpx.Response | None:
        """Devuelve la respuesta, o None. La causa del fallo queda en
        `last_failure` — «no se pudo» no es un diagnóstico.

        Un 403 (el sitio nos bloquea), un robots.txt que nos prohíbe, un
        dominio que no resuelve y un certificado caducado exigen tres
        acciones distintas, y una de ellas no exige ninguna. Contarlos
        juntos como «inalcanzable» llenaba la lista de «revisar a mano» de
        cosas que no se pueden arreglar.

        `retries` solo actúa sobre fallos transitorios. La espera entre
        intentos la impone el delay de cortesía del host, que ya está.
        """
        self.last_failure = None
        if not url.startswith(("http://", "https://")):
            log.warning("URL no absoluta rechazada: %r", url)
            self.last_failure = ("invalida", "URL no absoluta")
            return None
        if check_robots and not self.allowed(url):
            log.info("robots.txt prohíbe %s", url)
            self.last_failure = ("robots", "robots.txt del sitio nos lo prohíbe")
            return None
        for intento in range(retries + 1):
            self._wait_politely(url)
            try:
                resp = self.client.get(self._rewrite(url))
                resp.raise_for_status()
                self.last_failure = None
                return resp
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 429:
                    self.last_failure = ("sobrecarga", "el sitio nos pide bajar el ritmo (429)")
                elif code in (401, 403):
                    self.last_failure = ("bloqueada", f"el sitio nos responde {code}")
                elif code >= 500:
                    self.last_failure = ("transitoria", f"error del servidor ({code})")
                else:
                    self.last_failure = ("error_http", f"respuesta {code}")
                log.warning("fetch falló %s: %s", url, exc)
            except httpx.HTTPError as exc:
                self.last_failure = _classify_transport(exc)
                log.warning("fetch falló %s: %s", url, exc)
            if self.last_failure[0] not in self._REINTENTABLES:
                return None
            if intento < retries:
                log.info("reintento %d/%d en %s", intento + 1, retries, url)
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


def _classify_transport(exc: Exception) -> tuple[str, str]:
    """Causa legible de un fallo de red. Los mensajes vienen de httpx/ssl."""
    msg = str(exc)
    if "Name or service not known" in msg or "nodename nor servname" in msg:
        return ("dns", "el dominio no resuelve — ¿cambió de nombre?")
    if "CERTIFICATE_VERIFY_FAILED" in msg or "SSLError" in type(exc).__name__:
        return ("tls", "certificado del sitio inválido (no lo saltamos)")
    if "timed out" in msg.lower() or "Timeout" in type(exc).__name__:
        return ("timeout", "el sitio no respondió a tiempo")
    if "disconnected" in msg.lower() or "RemoteProtocol" in type(exc).__name__:
        # GDELT lo hace de vez en cuando: corta sin responder. Un reintento
        # basta — no es un bloqueo, es una conexión que se cae.
        return ("transitoria", "el servidor cortó la conexión sin responder")
    return ("red", msg[:120])


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
