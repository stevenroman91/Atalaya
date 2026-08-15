"""Capa de traducción (§6.0).

El español es la lengua canónica. Al final de cada run se generan y cachean
las versiones fr/en/pt para las lenguas usadas por al menos una cuenta.
`Translation.version` guarda el hash del canónico: si el resumen cambia, la
traducción se regenera. Nunca se traduce al vuelo en una petición web.

Backends:
  - "claude"  (por defecto si ANTHROPIC_API_KEY está definida): traducción
    fiel vía API de Claude, con la regla anti-alucinación en el prompt y
    salida estructurada (JSON Schema) para que nada se invente ni se omita.
  - "none": no se traduce; el dashboard muestra el canónico español con la
    indicación «resumen disponible solo en español». La interfaz (i18n) y las
    plantillas de recomendaciones están traducidas de forma determinista en
    locales/*.json, así que la UX sigue siendo coherente.

Regla §6.0: las URL y títulos de artículos fuente NUNCA se traducen — por eso
solo se envían título canónico, resumen y recomendaciones.
"""
from __future__ import annotations

import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from atalaya.db.models import Event, EventStatus, Translation, User

log = logging.getLogger(__name__)

TRANSLATABLE_LANGS = ["fr", "en", "pt"]

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "recommendations"],
    "additionalProperties": False,
}

_LANG_NAMES = {"fr": "francés", "en": "inglés", "pt": "portugués"}

_SYSTEM = (
    "Eres un traductor profesional para un servicio de vigilancia de seguridad "
    "destinado a oficiales de seguridad de delegaciones diplomáticas. Traduces del "
    "español al {lang}. Reglas NO negociables: traducción fiel, sin añadir ni omitir "
    "información; las cifras, fechas, nombres propios y topónimos se conservan "
    "idénticos; las citas textuales se traducen marcadas como citas; ningún "
    "embellecimiento ni interpretación. Si un campo llega vacío, devuélvelo vacío."
)


def backend() -> str:
    forced = os.environ.get("ATALAYA_TRANSLATE", "").lower()
    if forced in ("claude", "none"):
        return forced
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") else "none"


def languages_in_use(db: Session) -> list[str]:
    """Lenguas efectivamente usadas por al menos una cuenta activa (§6.0)."""
    langs = {u.lang for u in db.scalars(select(User).where(User.active.is_(True)))}
    return [l for l in TRANSLATABLE_LANGS if l in langs]


def _translate_with_claude(lang: str, title: str, summary: str | None,
                           recommendations: list[str] | None) -> dict | None:
    import anthropic

    client = anthropic.Anthropic()
    model = os.environ.get("ATALAYA_TRANSLATE_MODEL", "claude-opus-5")
    payload = {
        "title": title,
        "summary": summary or "",
        "recommendations": recommendations or [],
    }
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            system=_SYSTEM.format(lang=_LANG_NAMES[lang]),
            messages=[{
                "role": "user",
                "content": "Traduce los campos de este JSON:\n" + json.dumps(payload, ensure_ascii=False),
            }],
        )
        if response.stop_reason == "refusal":
            log.warning("traducción rechazada (%s)", lang)
            return None
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)
    except Exception as exc:
        log.warning("traducción falló (%s): %s", lang, exc)
        return None


def translate_pending(db: Session, langs: list[str] | None = None, limit: int = 200) -> dict:
    """Genera/regenera las traducciones cuyo canónico es nuevo o cambió."""
    stats = {"translated": 0, "skipped_backend_none": 0, "failed": 0}
    if backend() == "none":
        stats["skipped_backend_none"] = 1
        return stats
    langs = langs or languages_in_use(db)
    if not langs:
        return stats

    events = list(db.scalars(select(Event).where(
        Event.status.in_([EventStatus.published.value, EventStatus.pending_confirm.value])
    ).order_by(Event.created_at.desc()).limit(limit)))

    for event in events:
        existing = {t.lang: t for t in event.translations}
        for lang in langs:
            tr = existing.get(lang)
            if tr and tr.version == event.summary_version:
                continue  # cache válido
            result = _translate_with_claude(
                lang, event.title_es, event.summary_es, event.recommendations_es)
            if not result:
                stats["failed"] += 1
                continue
            if tr:
                tr.title, tr.summary = result["title"], result["summary"]
                tr.recommendations = result["recommendations"]
                tr.version = event.summary_version
            else:
                db.add(Translation(
                    event_id=event.id, lang=lang, title=result["title"],
                    summary=result["summary"], recommendations=result["recommendations"],
                    version=event.summary_version,
                ))
            stats["translated"] += 1
    db.commit()
    return stats
