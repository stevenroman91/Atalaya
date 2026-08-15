"""Extracción del texto íntegro con trafilatura (§5.2). Si no hay texto, el
artículo queda «título solamente» y nunca se resume (§7.1)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import trafilatura
from dateutil import parser as dateparser

log = logging.getLogger(__name__)


def extract_article(html: str, url: str) -> dict:
    """Devuelve {text, title, date, lang} extraídos del HTML. Valores None si
    trafilatura no puede extraer con confianza."""
    out = {"text": None, "title": None, "date": None, "lang": None}
    if not html:
        return out
    try:
        raw = trafilatura.extract(
            html, url=url, output_format="json",
            include_comments=False, with_metadata=True, favor_precision=True,
        )
        if raw:
            data = json.loads(raw)
            out["text"] = (data.get("text") or "").strip() or None
            out["title"] = (data.get("title") or "").strip() or None
            out["lang"] = data.get("language")
            if data.get("date"):
                out["date"] = _parse_dt(data["date"])
    except Exception as exc:  # nunca romper la colecta por un artículo
        log.warning("extracción falló %s: %s", url, exc)
    return out


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = dateparser.parse(value)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OverflowError):
        return None
