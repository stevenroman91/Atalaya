"""Rédaction des résumés (§5.6) — méthode STRICTEMENT extractive.

Le résumé (2–4 phrases, espagnol canonique) est composé de phrases prises
telles quelles dans le texte des articles récupérés — jamais générées. C'est
la garantie anti-hallucination la plus forte disponible sans LLM : chaque
phrase du résumé existe littéralement dans un article stocké en base (§7.2).

Divergences (§7.6) : si les bilans chiffrés diffèrent entre sources, une
phrase attribue chaque chiffre à sa source, sans trancher.
"""
from __future__ import annotations

import hashlib
import re

import yaml

from atalaya.config import CONFIG_DIR
from atalaya.db.models import Article
from atalaya.process.cluster import Cluster, normalize

MAX_SENTENCES = 4

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])")

# cifras de víctimas: «3 muertos», «dos heridos», «5 personas fallecidas»…
_CASUALTY = re.compile(
    r"\b(\d{1,4})\s+(?:personas\s+)?(muert[oa]s?|fallecid[oa]s?|herid[oa]s?|"
    r"víctimas|vítimas|mort[oa]s?|ferid[oa]s?)\b", re.I)


_NOISE_PREFIX = re.compile(r"^\[[^\]]{1,40}\]\s*")   # «[Publicidad]», «[Video]»…


def split_sentences(text: str) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(text or ""):
        s = _NOISE_PREFIX.sub("", s.strip())
        if len(s) > 25:
            out.append(s)
    return out


def _score_sentence(sent: str, entities: set[str]) -> int:
    """Prioriza frases con entidades del cluster, lugares y cifras."""
    n = normalize(sent)
    score = sum(1 for e in entities if e in n)
    if _CASUALTY.search(sent):
        score += 3
    if re.search(r"\b(en|na|no)\s+[A-ZÁÉÍÓÚÑ]", sent):
        score += 1
    return score


def casualty_figures(articles: list[Article]) -> dict[str, set[tuple[int, str]]]:
    """{source_name: {(n, tipo)}} para detectar divergencias entre fuentes."""
    out: dict[str, set[tuple[int, str]]] = {}
    for a in articles:
        found = set()
        for m in _CASUALTY.finditer(f"{a.title}. {(a.text or '')[:2000]}"):
            kind = "muertos" if re.match(r"muert|fallecid|mort|víctim|vítim", m.group(2), re.I) else "heridos"
            found.add((int(m.group(1)), kind))
        if found:
            out[a.source_name or a.domain] = found
    return out


def divergence_sentence(figures: dict[str, set[tuple[int, str]]]) -> str | None:
    """Si las fuentes dan cifras distintas para el mismo tipo, se expone la
    horquilla atribuyendo cada cifra (§7.6). Nunca se tranche."""
    by_kind: dict[str, dict[str, int]] = {}
    for source, vals in figures.items():
        for n, kind in vals:
            prev = by_kind.setdefault(kind, {})
            prev[source] = max(prev.get(source, 0), n)
    parts = []
    for kind, per_source in by_kind.items():
        if len(set(per_source.values())) > 1:
            attribution = "; ".join(f"{src} reporta {n}" for src, n in sorted(per_source.items()))
            parts.append(f"Las cifras de {kind} difieren según la fuente: {attribution}.")
    return " ".join(parts) if parts else None


def build_summary(cluster: Cluster) -> str | None:
    """2–4 frases extraídas del artículo de referencia (texto más completo),
    más la frase de divergencia si aplica. None si ningún artículo tiene
    texto íntegro (§7.1: sin texto no hay resumen)."""
    rep = cluster.representative
    if not rep.text:
        return None
    sentences = split_sentences(rep.text)
    if not sentences:
        return None
    # el lede responde quoi/où/quand; luego las frases mejor puntuadas en orden
    chosen = [sentences[0]]
    scored = sorted(
        ((i, s, _score_sentence(s, cluster.entities)) for i, s in enumerate(sentences[1:8], start=1)),
        key=lambda t: -t[2],
    )
    for i, s, sc in scored:
        if len(chosen) >= MAX_SENTENCES - 1:
            break
        if sc > 0 and s not in chosen:
            chosen.append(s)
    chosen = [s for _, s in sorted(
        ((sentences.index(s), s) for s in chosen), key=lambda t: t[0])]

    div = divergence_sentence(casualty_figures(cluster.articles))
    if div:
        chosen = chosen[: MAX_SENTENCES - 1] + [div]
    return " ".join(chosen[:MAX_SENTENCES])


def summary_version(title: str, summary: str | None, recommendations: list[str] | None) -> str:
    payload = f"{title}\n{summary or ''}\n{'|'.join(recommendations or [])}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# Tipo de incidente concreto: una recomendación útil dice cómo comportarse
# ante ESE hecho. El orden importa — el primero que coincide gana, así que
# los tipos más específicos van antes que los genéricos.
_INCIDENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("secuestro", r"secuestr\w*|plagio|priv\w+ de (la )?libertad|levant(ón|on)"),
    ("extorsion", r"extorsi\w*|cobro de piso|derecho de piso|montadeud\w*"),
    ("tiroteo", r"balacer\w*|tiroteo|ataque armado|disparos|r[aá]fag\w*"),
    ("robo_violento", r"asalt\w*|atrac\w*|robo a mano armada|despoj\w+ violent\w*"),
    ("homicidio", r"homicidi\w*|asesinat\w*|ejecuci[oó]n|cuerpo sin vida|masacre"),
    ("bloqueo", r"bloque\w*|cierre de (la )?(v[ií]a|carretera)|tom\w+ de (la )?carretera"),
    ("incendio", r"incendi\w*|conflagraci[oó]n"),
    ("sismo", r"sismo|terremot\w*|temblor"),
    ("inundacion", r"inundaci\w*|desbord\w*|crecid\w+ del r[ií]o"),
)


def detect_incident(text: str) -> str | None:
    """Tipo de incidente reconocido en el texto, o None."""
    if not text:
        return None
    low = text.lower()
    for name, pattern in _INCIDENT_PATTERNS:
        if re.search(pattern, low):
            return name
    return None


def build_recommendations(category: str, place: str,
                          text: str | None = None) -> list[str]:
    """Recomendaciones del evento: por tipo de incidente si se reconoce, si
    no por categoría. `text` es el título más el resumen."""
    with open(CONFIG_DIR / "recommendations.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    incident = detect_incident(text or "")
    tpl = cfg.get("incidents", {}).get(incident) if incident else None
    if not tpl:
        tpl = cfg["recommendations"].get(category, [])
    return [t.format(lugar=place) for t in tpl][:3]
