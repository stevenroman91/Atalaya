"""Fuentes de datos abiertas (§5.2 bis): GDELT, USGS, GDACS.

Por qué existen. La lista blanca es una lista de medios elegidos a mano: es
buena y es corta, y una noticia que ningún medio de la lista publica no
existe para Atalaya. Estas tres API amplían la base sin tocar las reglas.

  · GDELT indexa en continuo miles de medios locales que no tenemos. No
    aporta texto: aporta URLs. Se ingieren por la vía normal —nuestro robot
    va a buscar la página, la extrae y la somete a los mismos filtros—, así
    que el resumen sigue siendo estrictamente extractivo y la fuente sigue
    identificándose.
  · USGS y GDACS son datos oficiales geolocalizados. Traen lo que la prensa
    nos hace adivinar: magnitud, hora exacta y coordenadas reales. Un evento
    apoyado solo en ellos no se convierte en alerta por arte de magia: entra
    en la regla de siempre —fuente única + gravedad extrema → «a confirmar».

Nada de esto se consulta mientras el operador no haya probado el punto de
entrada desde producción («Probar las API» en el panel). El módulo solo
parsea; quien va a la red es el colector, con su robot identificado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser

from atalaya.config import load_countries

# Los nombres que USGS y GDACS usan en inglés, hacia el código del perímetro.
# Datos geográficos públicos, no URLs: no hay nada que «verificar» aquí.
_EN_NAMES = {
    "mexico": "MX", "guatemala": "GT", "honduras": "HN", "nicaragua": "NI",
    "el salvador": "SV", "panama": "PA", "argentina": "AR", "brazil": "BR",
    "brasil": "BR", "venezuela": "VE", "paraguay": "PY",
}


@dataclass
class ApiItem:
    """Un hecho traído por una API oficial, listo para volverse artículo."""
    url: str
    title: str
    text: str
    published_at: datetime
    country: str
    lat: float | None = None
    lon: float | None = None
    source_name: str = ""
    domain: str = ""
    extra: dict = field(default_factory=dict)


def country_from_place(place: str) -> str | None:
    """Código del país vigilado nombrado en un topónimo, o None.

    USGS escribe «12 km SSE of Ciudad Hidalgo, Mexico»; GDACS rellena
    <gdacs:country>. Se busca el nombre del país, en inglés o en español —
    no las coordenadas: una caja envolvente que dibujáramos a ojo sería un
    dato inventado, y las cajas de países vecinos se solapan.
    """
    if not place:
        return None
    hay = place.lower()
    for name, code in _EN_NAMES.items():
        if name in hay:
            return code
    for code, c in load_countries().items():
        if c.name.lower() in hay:
            return code
    return None


# ── GDELT 2.0 Doc API ────────────────────────────────────────────────────

def gdelt_query(country_name: str, keywords: list[str], lang: str = "es",
                langs: dict | None = None) -> str:
    """Consulta GDELT: el hecho debe ocurrir en el país, no solo mencionarlo.

    `sourcecountry:` filtra por el país del MEDIO, que es justo el error que
    llevamos toda la semana corrigiendo. Se usa el nombre del país como
    término de búsqueda y se deja que nuestros propios filtros de perímetro
    hagan el trabajo fino después.

    La lengua sí se fija. Sin ella, la primera prueba real devolvió «Head to
    Head Analysis : FIBRA Macquarie México & Its Rivals» — boletines
    bursátiles en inglés que mencionan México de pasada. No son prensa
    local, y su texto no es resumible en español de forma extractiva.
    """
    terms = " OR ".join(f'"{k}"' for k in keywords[:8])
    idioma = (langs or _GDELT_LANGS).get(lang)
    filtro = f" sourcelang:{idioma}" if idioma else ""
    return f'({terms}) "{country_name}"{filtro}'


# Cómo nombra GDELT las lenguas. Editable en apis.yaml sin tocar código: no
# tenemos forma de verificar su sintaxis desde el entorno de desarrollo, y
# una consulta mal formada la rechaza con un texto en claro, no con JSON.
_GDELT_LANGS = {"es": "spanish", "pt": "portuguese"}


def parse_gdelt(payload: dict) -> list[dict]:
    """Artículos GDELT → entradas con forma de flujo, para `_ingest_entry`.

    Devolver entradas y no artículos es deliberado: así los enlaces pasan
    por exactamente el mismo camino que los de Google News —pantalla de
    sección, perímetro, ventana, extracción, granja de contenido— sin una
    sola excepción escrita para GDELT.
    """
    out: list[dict] = []
    for art in (payload or {}).get("articles") or []:
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue
        out.append({"link": url, "title": title,
                    "published": _gdelt_date(art.get("seendate"))})
    return out


def _gdelt_date(raw: str | None) -> str | None:
    """«20260816T103000Z» → ISO 8601. Formato propio de GDELT."""
    if not raw or len(raw) < 15:
        return None
    try:
        dt = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


# ── USGS — sismos ────────────────────────────────────────────────────────

def parse_usgs(payload: dict, min_magnitude: float = 4.0) -> list[ApiItem]:
    """GeoJSON de USGS → hechos sísmicos del perímetro.

    El texto es descriptivo y factual, construido solo con los campos del
    boletín: magnitud, lugar y hora. No se interpreta nada — es la misma
    disciplina que el resumen extractivo, aplicada a un dato estructurado.
    """
    items: list[ApiItem] = []
    for feat in (payload or {}).get("features") or []:
        props = feat.get("properties") or {}
        mag = props.get("mag")
        if mag is None or float(mag) < min_magnitude:
            continue
        place = props.get("place") or ""
        code = country_from_place(place)
        if code is None:
            continue                      # fuera del perímetro
        ms = props.get("time")
        if not ms:
            continue
        when = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        lon, lat = (coords + [None, None])[:2]
        depth = coords[2] if len(coords) > 2 else None
        texto = (f"Sismo de magnitud {mag} registrado el "
                 f"{when.strftime('%Y-%m-%d %H:%M UTC')} en {place}.")
        if depth is not None:
            texto += f" Profundidad estimada: {depth} km."
        texto += " Boletín del Servicio Geológico de Estados Unidos (USGS)."
        items.append(ApiItem(
            url=props.get("url") or "", title=props.get("title") or place,
            text=texto, published_at=when, country=code,
            lat=lat, lon=lon, source_name="USGS", domain="earthquake.usgs.gov",
            extra={"magnitude": float(mag)}))
    return [i for i in items if i.url]


# ── GDACS — alertas multirriesgo ─────────────────────────────────────────

_GDACS_LEVELS = {"green": 0, "orange": 1, "red": 2}


def parse_gdacs(xml: str, min_level: str = "orange") -> list[ApiItem]:
    """RSS de GDACS → alertas del perímetro por encima de un nivel.

    Se filtra por nivel porque GDACS publica también lo verde, que es
    ruido para una vigilancia de seguridad: una tormenta sin impacto no
    cambia la conducta de nadie sobre el terreno.
    """
    umbral = _GDACS_LEVELS.get(min_level, 1)
    feed = feedparser.parse(xml or "")
    items: list[ApiItem] = []
    for e in feed.entries:
        nivel = (e.get("gdacs_alertlevel") or "").strip().lower()
        if _GDACS_LEVELS.get(nivel, -1) < umbral:
            continue
        code = country_from_place(e.get("gdacs_country") or e.get("title") or "")
        if code is None:
            continue
        when = _entry_dt(e)
        if when is None:
            continue
        lat, lon = _to_float(e.get("geo_lat")), _to_float(e.get("geo_long"))
        texto = (e.get("summary") or e.get("description") or "").strip()
        items.append(ApiItem(
            url=(e.get("link") or "").strip(), title=(e.get("title") or "").strip(),
            text=texto, published_at=when, country=code, lat=lat, lon=lon,
            source_name="GDACS", domain="gdacs.org",
            extra={"alertlevel": nivel}))
    return [i for i in items if i.url and i.title]


def _entry_dt(entry) -> datetime | None:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    return datetime(*st[:6], tzinfo=timezone.utc)


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── ReliefWeb (OCHA) — situaciones humanitarias ──────────────────────────

def parse_reliefweb(payload: dict) -> list[ApiItem]:
    """API de ReliefWeb → desastres declarados en el perímetro.

    ReliefWeb no publica sucesos del día: publica *situaciones* —una
    inundación que dura tres semanas, un terremoto y sus réplicas—. Por eso
    trae el estado («alert», «ongoing», «past») y por eso se descarta lo
    pasado: una situación cerrada es historia, no vigilancia.
    """
    items: list[ApiItem] = []
    for row in (payload or {}).get("data") or []:
        f = row.get("fields") or {}
        if (f.get("status") or "").lower() == "past":
            continue
        code = None
        for c in f.get("country") or []:
            code = country_from_place(c.get("name") or "")
            if code:
                break
        if code is None:
            continue
        when = _iso_dt((f.get("date") or {}).get("created"))
        if when is None:
            continue
        nombre = (f.get("name") or "").strip()
        tipos = ", ".join(t.get("name", "") for t in (f.get("type") or []) if t.get("name"))
        texto = f"{nombre}."
        if tipos:
            texto += f" Tipo de desastre: {tipos}."
        texto += " Ficha de situación de ReliefWeb (OCHA)."
        items.append(ApiItem(
            url=(f.get("url") or "").strip(), title=nombre, text=texto,
            published_at=when, country=code,
            source_name="ReliefWeb (OCHA)", domain="reliefweb.int",
            extra={"status": f.get("status")}))
    return [i for i in items if i.url and i.title]


def _iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── NOAA / NHC — ciclones ────────────────────────────────────────────────

def parse_nhc(xml: str) -> list[ApiItem]:
    """Boletines del National Hurricane Center → avisos del perímetro.

    Solo se conservan los que nombran un país vigilado: el Atlántico y el
    Pacífico oriental cubren mucho más que nuestro perímetro, y un huracán
    que gira hacia Florida no cambia la conducta de nadie en Guatemala.
    """
    feed = feedparser.parse(xml or "")
    items: list[ApiItem] = []
    for e in feed.entries:
        titulo = (e.get("title") or "").strip()
        cuerpo = (e.get("summary") or e.get("description") or "").strip()
        code = country_from_place(f"{titulo} {cuerpo}")
        if code is None:
            continue
        when = _entry_dt(e)
        if when is None:
            continue
        items.append(ApiItem(
            url=(e.get("link") or "").strip(), title=titulo,
            text=cuerpo or titulo, published_at=when, country=code,
            source_name="NOAA / National Hurricane Center", domain="nhc.noaa.gov"))
    return [i for i in items if i.url and i.title]


# ── despacho común de las API oficiales ──────────────────────────────────
# Las cuatro traen hechos ya formados (no URLs de prensa) y siguen el mismo
# camino: una URL, una respuesta, una lista de ApiItem. Tenerlo en un solo
# sitio evita que el colector y el diagnóstico diverjan — probar algo
# distinto de lo que se usará no prueba nada, y ya nos pasó una vez.

_OFICIALES = ("usgs_geojson", "gdacs_rss", "reliefweb_json", "nhc_rss")


def api_url(cfg: dict) -> str:
    """URL final de una API oficial, con sus parámetros obligatorios."""
    url = cfg.get("url") or ""
    if cfg.get("kind") == "reliefweb_json":
        # `appname` es obligatorio en la API de ReliefWeb: identifica al
        # cliente, como nuestro User-Agent. No es una clave.
        from urllib.parse import urlencode
        url += "?" + urlencode({
            "appname": cfg.get("appname", "atalaya"),
            "profile": "list",
            "preset": "latest",
            "limit": int(cfg.get("limit", 40)),
        })
    return url


def parse_official(cfg: dict, resp) -> list[ApiItem]:
    """Respuesta → hechos, según el tipo declarado en config."""
    kind = cfg.get("kind")
    if kind == "usgs_geojson":
        return parse_usgs(resp.json(), float(cfg.get("min_magnitude", 4.0)))
    if kind == "gdacs_rss":
        return parse_gdacs(resp.text, str(cfg.get("min_level", "orange")))
    if kind == "reliefweb_json":
        return parse_reliefweb(resp.json())
    if kind == "nhc_rss":
        return parse_nhc(resp.text)
    raise ValueError(f"tipo de API desconocido: {kind}")
