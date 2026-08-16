"""Dédoublonnage / clustering (§5.3).

Agrupa artículos que cubren el mismo evento: similitud de títulos
(token_set_ratio de rapidfuzz sobre títulos normalizados) reforzada por
entidades compartidas (nombres propios y cifras). Un cluster = un evento
candidato; el nº de fuentes distintas = la recurrencia.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from atalaya.collect.whitelist import strip_site_suffix
from atalaya.db.models import Article

TITLE_THRESHOLD = 68          # % token_set_ratio (títulos sin stopwords) para considerar mismo evento
ENTITY_BOOST = 12             # +boost si comparten ≥2 entidades

_STOP = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "en",
    "a", "al", "y", "o", "que", "por", "para", "con", "sin", "se", "su", "sus",
    "es", "son", "fue", "tras", "este", "esta", "sobre", "como", "más", "mas",
    "hay", "ya", "lo", "le", "les", "no", "sí", "da", "do", "dos", "das", "em",
    "um", "uma", "os", "as", "ao", "aos", "na", "no", "nos", "nas", "com",
    "por", "para", "foi", "após", "sob", "mais",
}


def normalize(text: str) -> str:
    """Minúsculas, sin acentos, sin puntuación y sin stopwords — las palabras
    vacías compartidas inflan la similitud entre eventos distintos."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(w for w in text.split() if w not in _STOP)


def extract_entities(title: str, text: str = "") -> set[str]:
    """Entidades aproximadas: nombres propios (mayúscula interna en frase) y
    cifras del título + primer párrafo. Sin dependencia de un NER pesado."""
    sample = title + ". " + (text or "")[:400]
    ents: set[str] = set()
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-ZÁÉÍÓÚÑ][a-záéíóúñü]+(?:\s+(?:de|del|la|el|los|las)\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+|\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+)*)", sample):
        ent = normalize(m.group(1))
        if ent and ent not in _STOP and len(ent) > 3:
            ents.add(ent)
    for m in re.finditer(r"\b\d{1,4}\b", title):
        ents.add(m.group(0))
    return ents


@dataclass
class Cluster:
    articles: list[Article] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)

    @property
    def representative(self) -> Article:
        """Artículo de referencia: texto más completo, luego el más antiguo
        (primero en publicar)."""
        return sorted(
            self.articles,
            key=lambda a: (-(len(a.text or "")), a.published_at or a.fetched_at),
        )[0]

    def dedup_key(self) -> str:
        """Clave estable del cluster para idempotencia: dominio+URL de los
        artículos ordenados no sirve (crece entre runs); usamos la URL del
        primer artículo publicado, estable aunque el cluster gane fuentes."""
        first = sorted(self.articles, key=lambda a: (a.published_at or a.fetched_at, a.url))[0]
        return hashlib.sha256(first.url.encode()).hexdigest()[:32]


def _boosted(shared: set[str]) -> bool:
    """¿Las entidades compartidas identifican de verdad el mismo hecho?

    Dos cifras sueltas no bastan. «Romário deixa PL após **5** anos» y
    «Terremoto de magnitude **5** atinge o sul da Espanha» compartían el
    número 5 y la palabra «Brasil» —que venía de la firma « | CNN Brasil »
    pegada a los dos titulares—, sumaban el boost y cruzaban el umbral por
    kilómetro y medio: la dimisión de un diputado quedó agrupada con un
    seísmo español, y el seísmo le prestó su categoría «desastre natural».
    Se exige al menos un nombre propio compartido, no solo cifras.
    """
    named = {e for e in shared if not e.isdigit()}
    return len(shared) >= 2 and bool(named)


def article_title(a: Article) -> str:
    """Titular sin la firma del medio. Se recorta también aquí, no solo en
    la ingesta: los artículos guardados antes llevan « | CNN Brasil » en el
    título, y esos dos tokens compartidos acercaban entre sí a todos los
    artículos del mismo medio."""
    return strip_site_suffix(a.title or "")


def _similar(a: Article, b_norm: str, b_ents: set[str], a_norm_cache: dict) -> bool:
    a_norm = a_norm_cache.setdefault(a.id, normalize(article_title(a)))
    score = fuzz.token_set_ratio(a_norm, b_norm)
    a_ents = extract_entities(article_title(a), a.text or "")
    if _boosted(a_ents & b_ents):
        score += ENTITY_BOOST
    return score >= TITLE_THRESHOLD


def cluster_articles(articles: list[Article]) -> list[Cluster]:
    """Clustering greedy single-pass por país (los llamantes ya separan por
    país). Suficiente para volúmenes diarios (centenares de artículos)."""
    clusters: list[Cluster] = []
    norm_cache: dict[int, str] = {}
    for art in sorted(articles, key=lambda a: (a.published_at or a.fetched_at, a.url)):
        art_norm = normalize(article_title(art))
        art_ents = extract_entities(article_title(art), art.text or "")
        placed = False
        for cl in clusters:
            if any(_similar(existing, art_norm, art_ents, norm_cache) for existing in cl.articles):
                cl.articles.append(art)
                cl.entities |= art_ents
                placed = True
                break
        if not placed:
            clusters.append(Cluster(articles=[art], entities=art_ents))
    return clusters
