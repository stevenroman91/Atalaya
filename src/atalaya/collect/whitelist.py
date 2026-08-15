"""Correspondencia de dominios con la lista blanca y detección de granjas de
contenido para dominios externos (§4, §7.5)."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from atalaya.config import Source, load_off_whitelist_rules, load_sources


def norm_domain(url_or_domain: str) -> str:
    d = url_or_domain
    if "//" in d:
        d = urlparse(d).netloc
    d = d.lower().split(":")[0]
    return d[4:] if d.startswith("www.") else d


def match_source(url: str) -> Source | None:
    """Devuelve la fuente de la lista blanca a la que pertenece la URL, o None."""
    domain = norm_domain(url)
    best: Source | None = None
    for s in load_sources():
        sd = s.domain.lower()
        if domain == sd or domain.endswith("." + sd) or sd.endswith("." + domain):
            # el match más específico (dominio más largo) gana:
            # oaxaca.eluniversal.com.mx antes que eluniversal.com.mx
            if best is None or len(sd) > len(best.domain):
                best = s
    return best


_FARM_SIGNALS = [
    re.compile(r"\b(noticias?|news|diario|prensa)[-_]?(24|365|hoy|ya|top|viral)\b", re.I),
    re.compile(r"\.(xyz|top|click|site|online|live|icu|buzz)$"),
]


def looks_like_content_farm(domain: str, title: str = "", text: str = "") -> bool:
    """Heurísticas de rechazo para dominios fuera de lista blanca (§7.5).
    Conservador a propósito: en caso de duda el artículo externo solo puede
    corroborar, nunca fundar una alerta."""
    d = norm_domain(domain)
    for rx in _FARM_SIGNALS:
        if rx.search(d):
            return True
    blocked = load_off_whitelist_rules().get("blocked_domains") or []
    if d in {norm_domain(b) for b in blocked}:
        return True
    # texto plantilla: casi sin frases propias pese a mucho texto
    if text and len(text) > 400 and text.count(".") < 3:
        return True
    return False


def geo_filter_ok(source: Source, country: str, title: str, text: str, query_terms: list[str]) -> bool:
    """Para medios «fuera de país» (cubren un país que no es el suyo): el
    artículo debe mencionar el país/zona objetivo para no arrastrar su
    actualidad doméstica (§4)."""
    if source.origin == country:
        return True
    if "*" in source.covers and source.type in ("internacional", "estatal"):
        pass  # internacionales: mismo filtro de entidades
    hay = f"{title}\n{text}".lower()
    from atalaya.config import load_countries
    c = load_countries().get(country)
    needles = [c.name.lower()] if c else []
    needles += [t.lower() for t in query_terms]
    return any(n in hay for n in needles)


# ── localización del hecho (§4) ───────────────────────────────────────────
# La prensa local cubre a diario sucesos del extranjero (terremotos, guerras,
# compatriotas en el exterior). Publicados por un medio nacional, pasaban por
# eventos del país vigilado. El titular es la señal fuerte de dónde ocurre el
# hecho: si sitúa el suceso fuera del perímetro y no menciona el país ni una
# zona vigilada, el artículo no es un evento local.
_FOREIGN_PLACES = (
    "Estados Unidos", "EE. UU.", "EE.UU.", "EEUU", "Washington", "Texas",
    "California", "Florida", "Nueva York", "Chicago", "Los Ángeles",
    "España", "Madrid", "Barcelona", "Francia", "París", "Italia", "Roma",
    "Alemania", "Berlín", "Reino Unido", "Londres", "Portugal", "Lisboa",
    "Rusia", "Moscú", "Ucrania", "Kiev", "China", "Pekín", "Japón", "Tokio",
    "India", "Indonesia", "Filipinas", "Turquía", "Irán", "Teherán", "Irak",
    "Israel", "Gaza", "Siria", "Afganistán", "Marruecos", "Egipto", "Nigeria",
    "Sudáfrica", "Australia", "Canadá", "Colombia", "Bogotá", "Medellín",
    "Cali", "Perú", "Lima", "Chile", "Santiago de Chile", "Ecuador", "Quito",
    "Guayaquil", "Bolivia", "La Paz", "Uruguay", "Montevideo", "Cuba",
    "La Habana", "Haití", "República Dominicana", "Puerto Rico", "Costa Rica",
    "San José de Costa Rica", "Belice", "Jamaica", "Trinidad y Tobago",
    # grafías portuguesas (prensa brasileña)
    "Estados Unidos da América", "Espanha", "França", "Alemanha", "Rússia",
    "Ucrânia", "Japão", "Indonésia", "Filipinas", "Turquia", "Irã", "Irão",
    "Colômbia", "Peru", "Bolívia", "Uruguai", "México", "Haiti",
)


def _needle_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.I)


def event_abroad(country: str, title: str) -> str | None:
    """Lugar extranjero que sitúa el hecho fuera del perímetro, o None.

    Solo mira el titular: es donde la prensa nombra el lugar del suceso. Un
    gentilicio no cuenta como localización — «Nicaragüense muere en Texas»
    ocurre en Texas, no en Nicaragua.
    """
    if not title:
        return None
    from atalaya.config import load_countries

    countries = load_countries()
    target = countries.get(country)
    if target is None:
        return None

    # menciones que anclan el hecho en el perímetro vigilado
    local = [target.name]
    for zone in target.zones:
        local += list(zone.query_terms)
        if getattr(zone, "parent", None):
            local.append(zone.parent)
    if any(_needle_re(t).search(title) for t in local):
        return None

    # los demás países vigilados también son «extranjero» entre sí
    foreign = [c.name for code, c in countries.items() if code != country]
    foreign += list(_FOREIGN_PLACES)
    for place in foreign:
        if place.lower() == target.name.lower():
            continue
        if _needle_re(place).search(title):
            return place
    return None
