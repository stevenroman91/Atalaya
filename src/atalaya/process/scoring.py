"""Filtrage / scoring (§5.4) y clasificación (§5.5).

Un evento se publica si:
  recurrencia ≥ 2 fuentes independientes (dos estatales no cuentan entre sí)
  Y gravedad (integridad física o modus operandi reproducible)
  Y pertinencia geográfica  Y frescura (ventana del run).
1 sola fuente + gravedad extrema → cola «pendiente de confirmar», nunca alerta.
Cada decisión queda trazada en score_detail (auditable desde el dashboard).
"""
from __future__ import annotations

from dataclasses import dataclass

from atalaya.config import load_keywords
from atalaya.db.models import Article
from atalaya.process.cluster import Cluster, article_title, normalize


def _hit_terms(hay_norm: str, terms: list[str]) -> list[str]:
    return [t for t in terms if normalize(t) in hay_norm]


def _kw(section: str, sub: str | None, lang: str) -> list[str]:
    kws = load_keywords()
    node = kws[section][sub] if sub else kws[section]
    return node.get(lang, node.get("es", []))


@dataclass
class ScoreResult:
    publishable: bool
    pending_confirm: bool
    reasons: dict


def independent_source_count(articles: list[Article]) -> tuple[int, int, bool]:
    """(nº fuentes distintas, nº independientes, hay_estatal).

    - Dominios repetidos cuentan una vez.
    - Todas las fuentes estatales juntas cuentan como UNA sola fuente
      independiente (no se corroboran entre sí, §4/§5.4).
    - off_whitelist solo corrobora si ya hay al menos 1 fuente de lista blanca
      (§4): si no la hay, no cuenta.
    """
    domains: dict[str, str] = {}
    for a in articles:
        domains.setdefault(a.domain, a.source_type or "off_whitelist")
    total = len(domains)
    state = sum(1 for t in domains.values() if t == "estatal")
    off = sum(1 for t in domains.values() if t == "off_whitelist")
    whitelisted_non_state = total - state - off
    independent = whitelisted_non_state + (1 if state else 0)
    if whitelisted_non_state + state >= 1:
        independent += off
    return total, independent, state > 0


# El titular y la entradilla dicen de qué va la noticia; el cuerpo menciona
# todo lo demás. Leer 1500 caracteres de cuerpo hacía que cualquier crónica
# venezolana que nombrara de pasada el terremoto de junio —y las nombran
# todas— puntuara como gravedad extrema.
_LEDE = 400


def _titulares(cluster: Cluster) -> str:
    return " ".join(normalize(article_title(a)) for a in cluster.articles)


def _entradillas(cluster: Cluster) -> str:
    return " ".join(
        normalize(f"{article_title(a)} {(a.text or '')[:_LEDE]}")
        for a in cluster.articles
    )


def severity_signals(cluster: Cluster, lang: str) -> dict:
    """Señales de gravedad, leídas donde la prensa las pone.

    La gravedad extrema se exige EN EL TITULAR. Es la señal que basta por sí
    sola para publicar con una única fuente («a confirmar»), así que su
    listón debe ser el más alto de todos: un artículo que menciona un
    terremoto en su decimoquinta línea no es un terremoto.

    Panel real del 16/08: «Reconstruir el salario mínimo», «Diócesis de
    Guanare anunció la agenda de la Virgen de Coromoto», «Miltico llega a
    La Gran Sabana» — veintiséis tarjetas de este tipo, todas por el mismo
    mecanismo.
    """
    titulares = _titulares(cluster)
    entradillas = _entradillas(cluster)
    return {
        "physical_harm": _hit_terms(entradillas, _kw("severity", "physical_harm", lang)),
        "modus_operandi": _hit_terms(entradillas, _kw("severity", "modus_operandi", lang)),
        "extreme": _hit_terms(titulares, _kw("severity", "extreme", lang)),
    }


def geo_relevant(cluster: Cluster) -> bool:
    """La colecta ya restringe por zona (consulta zona×palabra o fuente del
    país). Reconfirmamos que al menos un artículo tenga zona o país asignado."""
    return any(a.country for a in cluster.articles)


def score_cluster(cluster: Cluster, lang: str) -> ScoreResult:
    total, independent, has_state = independent_source_count(cluster.articles)
    sev = severity_signals(cluster, lang)
    grave = bool(sev["physical_harm"] or sev["modus_operandi"])
    extreme = bool(sev["extreme"])
    geo = geo_relevant(cluster)
    # frescura: la colecta ya rechazó todo lo fuera de ventana (§7.4); aquí
    # exigimos que el cluster tenga fecha
    fresh = any(a.published_at for a in cluster.articles)

    reasons = {
        "sources_total": total,
        "sources_independent": independent,
        "has_state_media": has_state,
        "severity": sev,
        "geo_ok": geo,
        "fresh_ok": fresh,
    }
    publishable = independent >= 2 and grave and geo and fresh
    pending = (not publishable) and independent >= 1 and extreme and geo and fresh
    return ScoreResult(publishable=publishable, pending_confirm=pending, reasons=reasons)


# ── Clasificación (§5.5) ─────────────────────────────────────────────────────

def classify_category(cluster: Cluster, lang: str) -> str:
    """Categoría por señales léxicas, o «sin_clasificar».

    El valor por defecto era «crimen de bajo impacto»: un hecho sin ninguna
    señal de categoría se anunciaba como delito. Así el ACV de un diputado
    en plena reunión y un dron derribado sobre Rumanía salieron en el panel
    etiquetados «crimen de bajo impacto», en tarjeta ALERTA. Inventar una
    categoría es peor que confesar que no la hay: el analista ve el hecho
    igualmente —nada se descarta— pero sabe que la etiqueta no es nuestra.
    """
    # Una sola pasada, sobre titular + entradilla. Probé dos pasadas —
    # titulares primero, cuerpo después— y rompía el caso del autocar:
    # «Acidente com ônibus deixa ao menos 12 mortos» encuentra «mortos» en
    # el titular y se clasificaba como crimen antes de que el cuerpo
    # («tombou») pudiera decir que es un accidente. Una regla en dos
    # tiempos con prioridades cruzadas es imposible de predecir.
    hay = _entradillas(cluster)
    kws = load_keywords()["categories"]
    for cat, langs in kws.items():   # orden del YAML = prioridad
        if _hit_terms(hay, langs.get(lang, langs.get("es", []))):
            return cat
    return "sin_clasificar"


def classify_level(cluster: Cluster, lang: str) -> str:
    hay = _entradillas(cluster)
    kws = load_keywords()["level_advertencia"]
    hits = _hit_terms(hay, kws.get(lang, kws.get("es", [])))
    return "advertencia" if hits else "informativo"


def classify_type(category: str, sev: dict) -> str:
    """ALERTA si podemos formular recomendaciones concretas; NOTA INFORMATIVA
    para hechos de contexto nacional sin recomendación operacional (§5.5).

    Un hecho sin categoría reconocida nunca es ALERTA: no sabemos ante qué
    estamos, así que no hay conducta que recomendar. Sigue en el panel como
    nota — el analista lo ve y decide — pero sin la insignia roja ni unas
    recomendaciones fabricadas sobre una etiqueta que no sostenemos.
    """
    if category == "sin_clasificar":
        return "NOTA"
    if sev.get("extreme") and category not in ("crimen_alto_impacto", "crimen_bajo_impacto",
                                               "desastre_natural"):
        return "NOTA"
    if category in ("crimen_alto_impacto", "crimen_bajo_impacto", "desastre_natural",
                    "manifestacion", "accidente"):
        return "ALERTA"
    return "NOTA"
