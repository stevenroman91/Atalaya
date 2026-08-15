"""Colector de Atalaya (§5.1–5.2).

Dos vías de entrada:
  1. Google News RSS por zona × palabra clave (con hl/gl/ceid por país).
  2. Flujos RSS directos de la lista blanca — solo URLs *verificadas*:
     las declaradas en config o las autodescubiertas (<link rel="alternate">)
     y persistidas en la tabla sources. Jamás se construye una URL a mano.

Todo artículo aceptado se almacena con URL canónica, título, texto íntegro,
fecha de publicación real, fuente y timestamp de fetch. Los rechazos se
registran con su motivo (§8 journalisation).
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import feedparser
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from atalaya.collect.extract import extract_article, text_from_feed_html, _parse_dt
from atalaya.collect.fetcher import PoliteFetcher
from atalaya.collect.whitelist import (
    event_abroad, geo_filter_ok, looks_like_content_farm, match_source,
    norm_domain, off_topic_section, perimeter_country_for,
)
from atalaya.config import Country, Zone, load_countries, load_keywords, load_schedule, load_sources
from atalaya.db.models import Article, ArticleStatus, CollectRun, SourceRecord, utcnow

log = logging.getLogger(__name__)

GN_BASE = "https://news.google.com/rss/search"


class RunCancelled(Exception):
    """La colecta fue anulada desde el panel de administración."""


def gn_feed_url(query: str, gn: dict, hours: int = 24) -> str:
    when = f" when:{hours}h" if hours else ""
    q = quote_plus(query + when)
    hl, gl, ceid = gn.get("hl", "es-419"), gn.get("gl", "MX"), gn.get("ceid", "MX:es-419")
    return f"{GN_BASE}?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


class Collector:
    def __init__(self, db: Session, fetcher: PoliteFetcher | None = None,
                 session_factory=None):
        self.db = db
        self.fetcher = fetcher or PoliteFetcher()
        # session_factory habilita la colecta paralela por país: cada hilo
        # trabaja con su propia sesión. Sin factory → modo secuencial.
        self.session_factory = session_factory
        self._last_cancel_check = 0.0
        self.stats = {"feeds": 0, "entries": 0, "stored": 0, "rejected": 0,
                      "title_only": 0, "duplicate_url": 0, "reject_reasons": {}}

    # ── anulación cooperativa ────────────────────────────────────────────
    def _check_cancel(self, run: CollectRun) -> None:
        """Consulta (como mucho cada 3 s) si el admin pidió anular el run.

        Se llama entre feeds, tras un commit: la lectura ve el valor fresco
        escrito por el proceso web.
        """
        now = time.monotonic()
        if now - self._last_cancel_check < 3.0:
            return
        self._last_cancel_check = now
        flag = self.db.scalar(select(CollectRun.cancel_requested)
                              .where(CollectRun.id == run.id))
        if flag:
            raise RunCancelled()

    # ── helpers ──────────────────────────────────────────────────────────
    def _reject(self, reason: str) -> None:
        self.stats["rejected"] += 1
        rr = self.stats["reject_reasons"]
        rr[reason] = rr.get(reason, 0) + 1

    def _source_record(self, domain: str, name: str) -> SourceRecord:
        rec = self.db.scalar(select(SourceRecord).where(SourceRecord.domain == domain))
        if rec:
            return rec
        try:
            # savepoint: dos hilos pueden crear la misma fuente a la vez
            # (p. ej. El Universal marcado por dos zonas de CDMX); el UNIQUE
            # de domain convierte al segundo en un re-select, sin romper el run
            with self.db.begin_nested():
                rec = SourceRecord(domain=domain, name=name)
                self.db.add(rec)
                self.db.flush()
            return rec
        except IntegrityError:
            return self.db.scalar(select(SourceRecord).where(SourceRecord.domain == domain))

    def mark_source(self, domain: str, name: str, ok: bool, error: str | None = None) -> None:
        rec = self._source_record(domain, name)
        if ok:
            rec.last_ok_at = utcnow()
            rec.consecutive_failures = 0
            rec.last_error = None
        else:
            rec.consecutive_failures += 1
            rec.last_error = (error or "")[:500]

    # ── descubrimiento RSS (solo URLs verificadas, nunca inventadas) ─────
    # Rutas convencionales de feeds. Solo se persisten si la respuesta se
    # parsea como RSS/Atom válido con entradas — probar y verificar no es
    # inventar: nada entra en base sin haber servido un feed real.
    _COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml",
                          "/arc/outboundfeeds/rss/")

    def _probe_feed(self, domain: str, path: str) -> str | None:
        url = f"https://{domain}{path}"
        resp = self.fetcher.get(url)
        if not resp:
            return None
        parsed = feedparser.parse(resp.content)
        if parsed.version and parsed.entries:
            return url
        return None

    # Muchos diarios publican en /rss un *índice* HTML de sus flujos por
    # sección en vez de un feed. Se extraen los candidatos de esa misma
    # página (nunca inventados) y solo se ingieren los que se parsean.
    _MAX_INDEX_FEEDS = 8

    @staticmethod
    def _feed_links_from_html(page_url: str, html: str) -> list[str]:
        """Candidatos a feed enlazados desde una página índice."""
        found: list[str] = []
        for m in re.finditer(
                r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*?'
                r'href=["\']([^"\']+)["\']', html, re.I):
            found.append(m.group(1))
        for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
            href = m.group(1)
            low = href.lower().split("?")[0]
            if low.endswith((".xml", ".rss")) or "/rss" in low or "/feed" in low:
                found.append(href)
        out: list[str] = []
        for href in found:
            url = urljoin(page_url, href.strip())
            if url.startswith(("http://", "https://")) and url not in out:
                out.append(url)
        return out

    @staticmethod
    def _absolutize(domain: str, url: str | None) -> str | None:
        """Normaliza una URL de feed a forma absoluta https://…

        Cubre config, descubrimiento y valores heredados en base: una URL
        relativa ('/rss/x.xml', 'rss/x.xml') o protocolo-relativa ('//…')
        se ancla al dominio de la fuente.
        """
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        return f"https://{domain}/{url.lstrip('/')}"

    def discover_rss(self, source) -> str | None:
        if source.rss:
            return self._absolutize(source.domain, source.rss)
        rec = self._source_record(source.domain, source.name)
        if rec.discovered_rss:
            fixed = self._absolutize(source.domain, rec.discovered_rss)
            if fixed != rec.discovered_rss:  # repara valores heredados
                rec.discovered_rss = fixed
            return fixed
        base = source.section_url or f"https://{source.domain}/"
        resp = self.fetcher.get(base)
        if resp:
            m = re.search(
                r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
                resp.text, re.I) or re.search(
                r'<link[^>]+href=["\']([^"\']+)["\'][^>]*type=["\']application/(?:rss|atom)\+xml["\']',
                resp.text, re.I)
            if m:
                href = self._absolutize(source.domain, m.group(1).strip())
                rec.discovered_rss = href
                log.info("RSS autodescubierto para %s: %s", source.domain, href)
                return href
        # home inalcanzable (anti-bot) o sin <link>: sondear rutas comunes
        for path in self._COMMON_FEED_PATHS:
            found = self._probe_feed(source.domain, path)
            if found:
                rec.discovered_rss = found
                self.mark_source(source.domain, source.name, ok=True)
                log.info("RSS verificado por sondeo para %s: %s", source.domain, found)
                return found
        if not resp:
            self.mark_source(source.domain, source.name, ok=False, error="home inalcanzable")
        return None

    # ── ingesta de un feed ───────────────────────────────────────────────
    def ingest_feed(self, feed_url: str, *, run: CollectRun, country: Country,
                    zone: Zone | None, keyword: str | None, theme: str | None,
                    window_hours: float, is_google_news: bool,
                    max_entries: int | None = None,
                    allow_index: bool = False) -> int:
        self._check_cancel(run)
        resp = self.fetcher.get(feed_url, check_robots=not is_google_news)
        if not resp:
            return 0
        parsed = feedparser.parse(resp.content)
        if allow_index and (not parsed.version or not parsed.entries):
            return self._ingest_feed_index(
                feed_url, resp.text, run=run, country=country, zone=zone,
                keyword=keyword, theme=theme, window_hours=window_hours,
                max_entries=max_entries)
        self.stats["feeds"] += 1
        cutoff = utcnow() - timedelta(hours=window_hours)
        stored = 0
        cfg = load_schedule().get("collector", {})
        limit = max_entries or int(cfg.get("max_articles_per_feed", 50))
        for entry in parsed.entries[:limit]:
            # cada entrada implica varias peticiones HTTP (redirección GN,
            # texto íntegro): comprobar la anulación aquí, no solo por feed
            self._check_cancel(run)
            self.stats["entries"] += 1
            if self._ingest_entry(entry, run=run, country=country, zone=zone,
                                  keyword=keyword, theme=theme, cutoff=cutoff,
                                  is_google_news=is_google_news):
                stored += 1
        # commit por feed: resiliencia ante caídas y visibilidad del flag
        # de anulación (transacción corta → la siguiente lectura ve fresco)
        self.db.commit()
        return stored

    def _ingest_feed_index(self, page_url: str, html: str, *, run: CollectRun,
                           country: Country, zone: Zone | None,
                           keyword: str | None, theme: str | None,
                           window_hours: float,
                           max_entries: int | None) -> int:
        """La URL apuntaba a un índice HTML de flujos: ingerir los que lo sean.

        Los candidatos salen de la propia página — no se construye ninguna
        URL — y cada uno se ingiere como feed normal; los que no se parsean
        devuelven 0 sin coste añadido.
        """
        cands = self._feed_links_from_html(page_url, html)
        if not cands:
            log.info("Índice de feeds sin candidatos: %s", page_url)
            return 0
        keep, dropped = cands[:self._MAX_INDEX_FEEDS], cands[self._MAX_INDEX_FEEDS:]
        if dropped:
            log.info("Índice %s: %d flujos ingeridos, %d omitidos (tope %d)",
                     page_url, len(keep), len(dropped), self._MAX_INDEX_FEEDS)
        stored = 0
        for url in keep:
            stored += self.ingest_feed(
                url, run=run, country=country, zone=zone, keyword=keyword,
                theme=theme, window_hours=window_hours, is_google_news=False,
                max_entries=max_entries)
        return stored

    @staticmethod
    def _entry_html(entry) -> str:
        """HTML íntegro que el flujo adjunta a la entrada, si lo trae."""
        for block in (entry.get("content") or []):
            value = (block.get("value") or "").strip()
            if value:
                return value
        return ""

    def _entry_date(self, entry) -> datetime | None:
        for key in ("published", "updated"):
            if entry.get(key):
                dt = _parse_dt(entry[key])
                if dt:
                    return dt
        return None

    def _ingest_entry(self, entry, *, run, country: Country, zone: Zone | None,
                      keyword: str | None, theme: str | None, cutoff: datetime,
                      is_google_news: bool) -> bool:
        link = entry.get("link")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            self._reject("entrada sin enlace o título")
            return False

        # Secciones ajenas a la seguridad (deportes, opinión, espectáculos):
        # la opinión además no es resumible de forma extractiva — una columna
        # comenta un hecho, no lo describe.
        section = off_topic_section(link)
        if section:
            self._reject(f"sección ajena a la vigilancia: {section}")
            return False

        # §4 — el hecho debe ocurrir en el perímetro. La prensa nacional cubre
        # a diario sucesos del extranjero; se descartan antes de gastar una
        # petición en el texto íntegro. Si el lugar es otro país vigilado, el
        # artículo se reatribuye a ese país en vez de perderse.
        abroad = event_abroad(country.code, title)
        if abroad:
            other = perimeter_country_for(abroad)
            if other and other in load_countries():
                country = load_countries()[other]
                zone = None  # la zona del feed de origen ya no aplica
                self.stats["reattributed"] = self.stats.get("reattributed", 0) + 1
            else:
                self._reject(f"hecho localizado fuera del perímetro: {abroad}")
                return False

        # Fecha del flujo: primer filtro de frescura (la fecha real del
        # artículo se re-verifica tras la extracción, §7.4)
        feed_date = self._entry_date(entry)
        if feed_date and feed_date < cutoff:
            self._reject("fuera de ventana (fecha del flujo)")
            return False

        gn_url = None
        if is_google_news:
            gn_url = link
            # dedupe ANTES de resolver: la resolución cuesta una petición a
            # news.google.com (1,5 s por cortesía) — en re-runs es el grueso
            # del tiempo. Un enlace GN ya visto no se vuelve a resolver.
            seen = self.db.scalar(select(Article).where(Article.gn_url == gn_url))
            if seen:
                self.stats["duplicate_url"] += 1
                if theme and seen.theme is None:
                    seen.theme = theme
                    self.stats["theme_backfilled"] = self.stats.get("theme_backfilled", 0) + 1
                return False
            resolved = self.fetcher.resolve_google_news_url(link)
            if not resolved:
                self._reject("redirección Google News no resoluble")
                return False
            link = resolved

        # dominio → lista blanca u off-whitelist
        domain = norm_domain(link)
        source = match_source(link)
        if source and not source.covers_country(country.code):
            self._reject(f"fuente {source.domain} no cubre {country.code}")
            return False

        # dedupe por URL canónica (idempotencia)
        existing = self.db.scalar(select(Article).where(Article.url == link))
        if existing:
            self.stats["duplicate_url"] += 1
            # un artículo ya ingerido por el job diario puede clasificarse
            # además en un tema semanal (no se duplica la fila)
            if theme and existing.theme is None:
                existing.theme = theme
                self.stats["theme_backfilled"] = self.stats.get("theme_backfilled", 0) + 1
            return False

        # texto íntegro
        page = self.fetcher.get(link)
        html = page.text if page else ""
        ext = extract_article(html, link)
        text = ext["text"]
        if not text:
            # el sitio bloquea al robot o no se pudo extraer: muchos flujos
            # traen el texto íntegro del editor en content:encoded
            text = text_from_feed_html(self._entry_html(entry))
            if text:
                self.stats["text_from_feed"] = self.stats.get("text_from_feed", 0) + 1
        published = ext["date"] or feed_date

        # §7.4 — fecha real fuera de ventana → rechazo (los flujos reciclan)
        if published and published < cutoff:
            self._reject("fuera de ventana (fecha real del artículo)")
            return False
        if not published:
            self._reject("sin fecha de publicación verificable")
            return False

        if not source:
            # fuera de lista blanca: filtro granja de contenido (§7.5); la
            # regla «solo corrobora, nunca funda» se aplica en el scoring
            if looks_like_content_farm(domain, title, text or ""):
                self._reject(f"señales de granja de contenido: {domain}")
                return False
            source_type = "off_whitelist"
            source_name = domain
        else:
            source_type = source.type
            source_name = source.name
            if zone and not geo_filter_ok(source, country.code, title, text or "", zone.query_terms):
                self._reject(f"fuente fuera de país sin mención de {country.code}")
                return False
            self.mark_source(source.domain, source.name, ok=True)

        art = Article(
            run_id=run.id, url=link, gn_url=gn_url, domain=domain,
            source_name=source_name, source_type=source_type,
            title=ext["title"] or title, text=text,
            lang=ext["lang"] or country.lang, published_at=published,
            country=country.code, zone_id=zone.id if zone else None,
            keyword=keyword, theme=theme,
            status=ArticleStatus.extracted.value if text else ArticleStatus.title_only.value,
        )
        if not text:
            self.stats["title_only"] += 1
        try:
            # savepoint: dos hilos (zonas distintas) pueden descubrir el mismo
            # artículo a la vez y pasar ambos el dedupe — el UNIQUE de la URL
            # convierte al segundo en duplicado, sin romper la transacción
            with self.db.begin_nested():
                self.db.add(art)
                self.db.flush()
        except IntegrityError:
            self.stats["duplicate_url"] += 1
            return False
        self.stats["stored"] += 1
        return True

    # ── colecta diaria (§5.1) ────────────────────────────────────────────
    def _daily_zone(self, run: CollectRun, country: Country, zone: Zone,
                    window: float, kws: dict) -> None:
        words = kws.get(country.lang, kws["es"])
        for kw in words:
            for term in zone.query_terms:
                url = gn_feed_url(f'{kw} "{term}"', country.gn, hours=int(window))
                self.ingest_feed(url, run=run, country=country, zone=zone,
                                 keyword=kw, theme=None,
                                 window_hours=window, is_google_news=True)

    def _daily_rss(self, run: CollectRun, country: Country, window: float) -> None:
        """Flujos RSS directos de las fuentes que cubren el país."""
        for source in load_sources():
            if not source.covers_country(country.code) or source.origin != country.code:
                continue
            feed = self.discover_rss(source)
            if not feed:
                continue
            before = self.stats["feeds"]
            self.ingest_feed(feed, run=run, country=country, zone=None,
                             keyword=None, theme=None, window_hours=window,
                             is_google_news=False, allow_index=True)
            # «feeds» solo crece cuando algo se parseó como flujo real: si no
            # se movió, la fuente no ha sido consultada de verdad y así debe
            # figurar en el panel de cobertura.
            if self.stats["feeds"] == before:
                self.mark_source(source.domain, source.name, ok=False,
                                 error="el flujo no devolvió entradas")
            else:
                self.mark_source(source.domain, source.name, ok=True)

    def collect_daily(self, run: CollectRun, countries: list[str] | None = None) -> dict:
        sched = load_schedule()["daily"]
        window = float(sched.get("window_hours", 24)) + float(sched.get("overlap_hours", 2))
        kws = load_keywords()["daily"]
        todo = [c for c in load_countries().values()
                if c.daily and not (countries and c.code not in countries)]
        # La unidad de paralelismo es la zona (México tiene ~10), no el país:
        # así un solo país con muchas zonas también aprovecha los workers.
        tasks: list[tuple] = []
        for country in todo:
            for zone in country.zones:
                tasks.append(("zone", country, zone))
            tasks.append(("rss", country, None))

        def work(col: "Collector", wrun: CollectRun, task: tuple) -> None:
            kind, country, zone = task
            if kind == "zone":
                col._daily_zone(wrun, country, zone, window, kws)
            else:
                col._daily_rss(wrun, country, window)

        self._run_parallel(run, tasks, work)
        self.db.commit()
        return self.stats

    # ── colecta semanal (§6.2) ───────────────────────────────────────────
    def _weekly_country(self, run: CollectRun, country: Country, window: float,
                        weekly_kws: dict) -> None:
        for theme, langs in weekly_kws.items():
            for kw in langs.get(country.lang, langs["es"]):
                url = gn_feed_url(f'{kw} {country.name}', country.gn, hours=int(window))
                self.ingest_feed(url, run=run, country=country, zone=None,
                                 keyword=kw, theme=theme,
                                 window_hours=window, is_google_news=True,
                                 max_entries=15)

    def collect_weekly(self, run: CollectRun, countries: list[str] | None = None) -> dict:
        sched = load_schedule()["weekly"]
        window = float(sched.get("window_days", 7)) * 24 + float(sched.get("overlap_hours", 6))
        weekly_kws = load_keywords()["weekly"]
        todo = [c for c in load_countries().values()
                if c.weekly and not (countries and c.code not in countries)]
        self._run_parallel(run, todo,
                           lambda col, r, c: col._weekly_country(r, c, window, weekly_kws))
        self.db.commit()
        return self.stats

    # ── orquestación secuencial / paralela ───────────────────────────────
    def _run_parallel(self, run: CollectRun, items: list, work) -> None:
        """Ejecuta `work(collector, run, item)` para cada elemento.

        Con session_factory y parallel_workers > 1, los elementos (zonas en
        la colecta diaria, países en la semanal) se reparten entre hilos:
        cada hilo usa su propia sesión DB y comparte el PoliteFetcher
        (thread-safe, cortesía por host preservada). El límite de 1,5 s por
        host sigue aplicando globalmente, de modo que ningún medio recibe
        más carga que en modo secuencial.
        """
        workers = int(load_schedule().get("collector", {}).get("parallel_workers", 4))

        # progreso visible en admin: nº total de tareas + incremento atómico
        run.progress_total = len(items)
        run.progress_done = 0
        self.db.commit()

        def _tick(session: Session) -> None:
            session.execute(update(CollectRun).where(CollectRun.id == run.id)
                            .values(progress_done=CollectRun.progress_done + 1))
            session.commit()

        if not self.session_factory or workers <= 1 or len(items) <= 1:
            for item in items:
                try:
                    work(self, run, item)
                except RunCancelled:
                    raise
                except Exception:
                    # una tarea que falla no debe matar el run completo
                    log.exception("tarea de colecta falló (%r)", item)
                    self.db.rollback()
                    self.stats["task_errors"] = self.stats.get("task_errors", 0) + 1
                _tick(self.db)
            return

        def per_item(item) -> tuple[dict, bool]:
            session = self.session_factory()
            try:
                worker = Collector(session, self.fetcher)
                wrun = session.get(CollectRun, run.id)
                try:
                    work(worker, wrun, item)
                    session.commit()
                except RunCancelled:
                    session.commit()  # conserva lo ya almacenado
                    return worker.stats, True
                except Exception:
                    log.exception("tarea de colecta falló (%r)", item)
                    session.rollback()
                    worker.stats["task_errors"] = worker.stats.get("task_errors", 0) + 1
                _tick(session)
                return worker.stats, False
            finally:
                session.close()

        cancelled = False
        with ThreadPoolExecutor(max_workers=min(workers, len(items)),
                                thread_name_prefix="collect") as pool:
            for stats, was_cancelled in pool.map(per_item, items):
                self._merge_stats(stats)
                cancelled = cancelled or was_cancelled
        if cancelled:
            raise RunCancelled()

    def _merge_stats(self, other: dict) -> None:
        for key, value in other.items():
            if key == "reject_reasons":
                mine = self.stats.setdefault("reject_reasons", {})
                for reason, n in value.items():
                    mine[reason] = mine.get(reason, 0) + n
            elif isinstance(value, int):
                self.stats[key] = self.stats.get(key, 0) + value
