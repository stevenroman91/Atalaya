"""Carga de configuración YAML (zonas, palabras clave, fuentes, auth, horarios).

Todo lo editable sin tocar código vive en config/*.yaml. Este módulo solo lee
y valida; nunca escribe. La ruta base se puede sobreescribir con ATALAYA_CONFIG_DIR.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("ATALAYA_CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))

SUPPORTED_LANGS = ["es", "fr", "en", "pt"]
CANONICAL_LANG = "es"

WEEKLY_THEMES = ["politica", "economia", "sanitario", "natural", "seguridad"]
THEME_LABELS_ES = {
    "politica": "Política",
    "economia": "Economía",
    "sanitario": "Riesgos sanitarios",
    "natural": "Riesgos naturales",
    "seguridad": "Seguridad",
}

CATEGORIES = [
    "crimen_alto_impacto",
    "crimen_bajo_impacto",
    "desastre_natural",
    "operacion_seguridad",
    "manifestacion",
    "accidente",
    "sin_clasificar",
]
CATEGORY_LABELS_ES = {
    "crimen_alto_impacto": "crimen de alto impacto",
    "crimen_bajo_impacto": "crimen de bajo impacto",
    "desastre_natural": "desastre natural",
    "operacion_seguridad": "operación de seguridad",
    "manifestacion": "manifestación",
    "accidente": "accidente",
    "sin_clasificar": "sin clasificar",
}


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Zone:
    id: str
    name: str
    country: str
    query_terms: list[str]
    geo: tuple[float, float] | None = None
    parent: str | None = None


@dataclass
class Country:
    code: str
    name: str
    lang: str
    gn: dict
    daily: bool
    weekly: bool
    zones: list[Zone] = field(default_factory=list)


@dataclass
class Source:
    name: str
    domain: str
    origin: str
    covers: list[str]
    lang: str
    type: str  # independiente | estatal | internacional
    rss: str | None = None
    section_url: str | None = None
    notes: str | None = None

    @property
    def is_state_media(self) -> bool:
        return self.type == "estatal"

    def covers_country(self, code: str) -> bool:
        return "*" in self.covers or code in self.covers


@lru_cache(maxsize=1)
def load_countries() -> dict[str, Country]:
    raw = _load_yaml("zones.yaml")["countries"]
    out: dict[str, Country] = {}
    for code, c in raw.items():
        zones = [
            Zone(
                id=z["id"],
                name=z["name"],
                country=code,
                query_terms=z.get("query_terms", [z["name"]]),
                geo=tuple(z["geo"]) if z.get("geo") else None,
                parent=z.get("parent"),
            )
            for z in c.get("zones", [])
        ]
        out[code] = Country(
            code=code, name=c["name"], lang=c.get("lang", "es"), gn=c.get("gn", {}),
            daily=bool(c.get("daily")), weekly=bool(c.get("weekly")), zones=zones,
        )
    return out


@lru_cache(maxsize=1)
def load_keywords() -> dict:
    return _load_yaml("keywords.yaml")


@lru_cache(maxsize=1)
def load_sources() -> list[Source]:
    raw = _load_yaml("sources.yaml")["sources"]
    return [Source(**{k: v for k, v in s.items()}) for s in raw]


@lru_cache(maxsize=1)
def load_off_whitelist_rules() -> dict:
    return _load_yaml("sources.yaml").get("off_whitelist", {})


@lru_cache(maxsize=1)
def load_auth_config() -> dict:
    return _load_yaml("auth.yaml")


@lru_cache(maxsize=1)
def load_schedule() -> dict:
    return _load_yaml("schedule.yaml")


def source_by_domain() -> dict[str, Source]:
    return {s.domain: s for s in load_sources()}


def zone_by_id() -> dict[str, Zone]:
    return {z.id: z for c in load_countries().values() for z in c.zones}


def clear_caches() -> None:
    """Para tests y para el panel admin tras editar la config."""
    for fn in (load_countries, load_keywords, load_sources, load_auth_config,
               load_schedule, load_off_whitelist_rules):
        fn.cache_clear()
