"""i18n de la interfaz: ficheros locales/{es,fr,en,pt}.json."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from atalaya.config import SUPPORTED_LANGS

LOCALES_DIR = Path(os.environ.get("ATALAYA_LOCALES_DIR",
                                  Path(__file__).resolve().parents[3] / "locales"))


@lru_cache(maxsize=8)
def load_locale(lang: str) -> dict:
    lang = lang if lang in SUPPORTED_LANGS else "es"
    with open(LOCALES_DIR / f"{lang}.json", encoding="utf-8") as fh:
        data = json.load(fh)
    if lang != "es":  # fallback al español para claves faltantes
        with open(LOCALES_DIR / "es.json", encoding="utf-8") as fh:
            base = json.load(fh)
        base.update(data)
        return base
    return data


def translator(lang: str):
    strings = load_locale(lang)
    def t(key: str, **kwargs) -> str:
        value = strings.get(key, key)
        return value.format(**kwargs) if kwargs else value
    return t
