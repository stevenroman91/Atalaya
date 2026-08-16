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
    # resto de Europa y del mundo: la prensa del perímetro cubre a diario
    # sucesos de países que esta lista no nombraba, y el filtro los dejaba
    # pasar por desconocidos — un dron sobre Rumanía acabó de alerta brasileña
    "Rumanía", "Rumania", "Romênia", "Roménia", "Bucarest",
    "Hungría", "Hungria", "Budapest", "Polonia", "Polônia", "Varsovia",
    "Moldavia", "Moldávia", "Bielorrusia", "Bielorrússia",
    "Grecia", "Grécia", "Atenas", "Bulgaria", "Serbia", "Sérvia", "Croacia",
    "Chequia", "República Checa", "Praga", "Eslovaquia", "Austria", "Viena",
    "Suiza", "Suíça", "Ginebra", "Genebra", "Bélgica", "Bruselas", "Bruxelas",
    "Países Bajos", "Holanda", "Ámsterdam", "Amsterdã", "Dinamarca",
    "Suecia", "Suécia", "Estocolmo", "Noruega", "Oslo", "Finlandia",
    "Finlândia", "Irlanda", "Dublín", "Escocia", "Escócia", "Inglaterra",
    "Gales", "Cambridge", "Oxford", "Mánchester", "Manchester",
    "Estambul", "Istambul", "Ankara", "Líbano", "Beirut", "Jordania",
    "Jordânia", "Arabia Saudí", "Arábia Saudita", "Emiratos Árabes",
    "Catar", "Qatar", "Yemen", "Iémen", "Kuwait", "Cisjordania",
    "Corea del Sur", "Coreia do Sul", "Seúl", "Seul", "Corea del Norte",
    "Coreia do Norte", "Vietnam", "Vietnã", "Tailandia", "Tailândia",
    "Malasia", "Malásia", "Singapur", "Singapura", "Camboya", "Myanmar",
    "Pakistán", "Paquistão", "Bangladés", "Bangladesh", "Nepal",
    "Sri Lanka", "Taiwán", "Taiwan", "Hong Kong", "Nueva Delhi",
    "Argelia", "Argélia", "Túnez", "Tunísia", "Libia", "Líbia", "Sudán",
    "Sudão", "Etiopía", "Etiópia", "Kenia", "Quênia", "Nairobi", "Ghana",
    "Senegal", "Mali", "Malí", "Níger", "Chad", "Somalia", "Somália",
    "Congo", "Angola", "Mozambique", "Moçambique", "Zimbabue", "Zimbábue",
    "Nueva Zelanda", "Nova Zelândia", "Papúa", "Fiyi",
    "Miami", "Houston", "Dallas", "Boston", "Filadelfia", "Atlanta",
    "Denver", "Seattle", "Las Vegas", "San Francisco", "Nueva Jersey",
    "Arizona", "Nevada", "Ohio", "Michigan", "Minnesota", "Luisiana",
)


def _needle_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.I)


# El nombre del medio pegado al final del titular por el flujo RSS.
_SITE_SUFFIX_RE = re.compile(r"\s+[|–—]\s+[^|–—]{2,40}\s*$")


def strip_site_suffix(title: str) -> str:
    """Quita el nombre del medio pegado al final del titular.

    Los flujos rematan el titular con « | CNN Brasil » o « — O Globo ». No
    es solo ruido: mentía sobre el lugar del hecho. «Número de mortos na
    Colômbia sobe para 294 | CNN Brasil» contiene «Brasil», y el filtro de
    perímetro leía esa firma como un ancla local — así entraba el terremoto
    colombiano en el panel de Brasil, y con él todo lo que publica un medio
    que se llama como su país. El nombre del medio no sitúa nada.

    Solo se corta tras barra vertical o raya: el guion corto separa
    demasiados titulares legítimos para arriesgarse.
    """
    if not title:
        return ""
    return _SITE_SUFFIX_RE.sub("", title).strip() or title.strip()


def event_abroad(country: str, title: str, summary: str = "") -> str | None:
    """Lugar extranjero que sitúa el hecho fuera del perímetro, o None.

    El titular manda: es donde la prensa nombra el lugar del suceso, y un
    ancla local en él zanja la cuestión. Un gentilicio no cuenta como
    localización — «Nicaragüense muere en Texas» ocurre en Texas.

    Pero un titular puede no nombrar ningún lugar: «La trágica historia de
    la trilliza colombiana que sobrevivió al terremoto» no dice dónde pasó
    —solo el gentilicio— y así se colaba como suceso argentino. Cuando el
    titular no ancla nada, se mira el resumen: si nombra un lugar extranjero
    y ninguno del perímetro vigilado, el hecho ocurre fuera.
    """
    if not title:
        return None
    from atalaya.config import load_countries

    title = strip_site_suffix(title)
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
    foreign = [p for p in foreign if p.lower() != target.name.lower()]

    for place in foreign:
        if _needle_re(place).search(title):
            return place

    # Titular mudo sobre el lugar: el resumen decide, y solo si es unánime.
    # Exigir que NO haya ancla local evita descartar un suceso local cuyo
    # texto menciona el extranjero de pasada.
    if not summary or any(_needle_re(t).search(summary) for t in local):
        return None
    for place in foreign:
        if _needle_re(place).search(summary):
            return place
    return None


# ── sección internacional (§4) ───────────────────────────────────────────
# Un diario clasifica él mismo lo que ocurre fuera de sus fronteras. Esa
# etiqueta la pone la redacción, no nosotros: es la señal más fiable que
# existe de que el hecho no ocurre en el país del medio — más fiable que
# cualquier lista de topónimos, que siempre tendrá agujeros.
_FOREIGN_SECTIONS = (
    "internacional", "internacionales", "internacionais", "mundo",
    "world", "international", "exterior", "extranjero", "estrangeiro",
)


def foreign_section(url: str) -> str | None:
    """Sección de la URL que sitúa el hecho fuera del país del medio, o None."""
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return None
    for part in path.split("/"):
        if part in _FOREIGN_SECTIONS:
            return part
    return None


def perimeter_anchor(text: str) -> str | None:
    """Lugar del perímetro vigilado nombrado en el texto, o None.

    Sirve para no tirar por la borda lo que la sección internacional de un
    medio publica sobre otro país vigilado: un atentado en Caracas contado
    por O Globo va en «mundo», pero ocurre dentro del perímetro.
    """
    if not text:
        return None
    from atalaya.config import load_countries

    for c in load_countries().values():
        if _needle_re(c.name).search(text):
            return c.name
        for zone in c.zones:
            for term in zone.query_terms:
                if len(term) > 3 and _needle_re(term).search(text):
                    return term
    return None


def perimeter_country_in(text: str) -> str | None:
    """Código del país vigilado nombrado en un texto, o None.

    Para las fuentes regionales, que cubren todo el perímetro: su artículo
    no viene de un flujo por país, así que el país hay que leerlo en el
    propio texto. Sin ancla no se atribuye nada — atribuir «por defecto»
    es exactamente cómo el terremoto colombiano acabó en el panel de Brasil.
    """
    if not text:
        return None
    from atalaya.config import load_countries

    for code, c in load_countries().items():
        if _needle_re(c.name).search(text):
            return code
        for zone in c.zones:
            for term in zone.query_terms:
                if len(term) > 3 and _needle_re(term).search(text):
                    return code
    return None


def perimeter_country_for(place: str) -> str | None:
    """Código del país vigilado que corresponde a un lugar, o None.

    Un hecho en Venezuela publicado por la prensa nicaragüense no está fuera
    del perímetro: está en otro país del perímetro. Reatribuirlo conserva la
    información en vez de perderla — descartarlo sería tan falso como
    contarlo en Nicaragua.
    """
    from atalaya.config import load_countries

    for code, c in load_countries().items():
        if c.name.lower() == place.lower():
            return code
    return None


# ── secciones no pertinentes (§4) ────────────────────────────────────────
# Los flujos generales de un diario mezclan deportes, opinión y espectáculos
# con los sucesos. Nada de eso es un evento de seguridad, y la opinión es
# además incompatible con el resumen extractivo: una columna no describe un
# hecho, lo comenta. Se descarta por la ruta de la URL, que los medios
# estructuran por sección de forma fiable.
_OFF_TOPIC_SECTIONS = (
    "opinion", "opinión", "editorial", "columna", "columnas", "columnistas",
    "blog", "blogs", "analisis", "análisis", "cartas",
    # rúbricas de formato: un podcast o una galería comentan la actualidad,
    # no describen un hecho — y no son resumibles de forma extractiva
    "video", "videos", "podcast", "podcasts", "galeria", "galería",
    "fotogaleria", "fotogalería", "multimedia", "infografia", "infografía",
    "deportes", "deporte", "futbol", "fútbol", "beisbol", "béisbol", "nba",
    "espectaculos", "espectáculos", "farandula", "farándula", "gente",
    "vida", "estilo", "moda", "gastronomia", "gastronomía", "viajes",
    "cultura", "arte", "musica", "música", "cine", "series", "television",
    "tecnologia", "tecnología", "ciencia", "salud", "bienestar",
    "horoscopo", "horóscopo", "recetas", "mascotas", "motor", "autos",
    "english",  # ediciones traducidas: duplican la portada en otro idioma
)


def off_topic_section(url: str) -> str | None:
    """Sección de la URL que descarta el artículo, o None."""
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return None
    for part in path.split("/"):
        if part in _OFF_TOPIC_SECTIONS:
            return part
    return None
