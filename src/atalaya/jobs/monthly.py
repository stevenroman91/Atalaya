"""Síntesis mensual (§6.3), generada el 1 de cada mes para cada país (salvo
México) a partir del material semanal.

Anti-alucinación: la síntesis se construye EXCLUSIVAMENTE con el material
almacenado (mini-resúmenes semanales, eventos del pipeline diario). El modo
por defecto es determinista (composición de frases extraídas). Si hay
ANTHROPIC_API_KEY, la redacción de los párrafos de síntesis se delega a Claude
con la consigna estricta de usar únicamente el material provisto — el material
citado (títulos, URL, fechas) nunca pasa por el modelo.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from atalaya.config import (
    CATEGORY_LABELS_ES, THEME_LABELS_ES, WEEKLY_THEMES, load_countries, zone_by_id,
)
from atalaya.db.models import (
    Article, Event, EventStatus, MonthlySynthesis, WeeklyItem,
)

log = logging.getLogger(__name__)


def previous_month(today: date | None = None) -> str:
    today = today or date.today()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12 else datetime(y, m + 1, 1, tzinfo=timezone.utc)
    return start, end


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and \
        os.environ.get("ATALAYA_TRANSLATE", "").lower() != "none"


def _compose_section_text(theme: str, items: list[dict]) -> str:
    """Síntesis determinista: composición de los mini-resúmenes almacenados."""
    if not items:
        return "Sin material relevante registrado este mes."
    parts = [it["resumen"] for it in items[:4] if it.get("resumen")]
    if not parts:
        return f"{len(items)} artículos registrados este mes (solo título disponible)."
    return " ".join(parts)


def _llm_section_text(country_name: str, theme: str, items: list[dict]) -> str | None:
    """Redacción con Claude, restringida al material provisto (§7)."""
    import anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("ATALAYA_TRANSLATE_MODEL", "claude-opus-5")
    material = json.dumps(
        [{"fecha": it["fecha"], "titulo": it["titulo"], "resumen": it["resumen"]}
         for it in items], ensure_ascii=False)
    try:
        resp = client.messages.create(
            model=model, max_tokens=1024,
            system=(
                "Redactas la sección «{tema}» de una síntesis mensual de seguridad "
                "sobre {pais} para oficiales de seguridad diplomáticos. REGLAS NO "
                "NEGOCIABLES: usa EXCLUSIVAMENTE la información del material adjunto; "
                "prohibido añadir hechos, cifras o contexto que no figuren en él; si "
                "las fuentes divergen, expón la horquilla; tono factual, sin "
                "sensacionalismo; 1 párrafo de 3 a 6 frases en español."
            ).format(tema=THEME_LABELS_ES[theme], pais=country_name),
            messages=[{"role": "user", "content": "Material del mes:\n" + material}],
        )
        if resp.stop_reason == "refusal":
            return None
        return next((b.text for b in resp.content if b.type == "text"), None)
    except Exception as exc:
        log.warning("síntesis LLM falló (%s/%s): %s", country_name, theme, exc)
        return None


def generate_monthly(db: Session, month: str | None = None,
                     countries: list[str] | None = None) -> dict:
    month = month or previous_month()
    start, end = _month_bounds(month)
    stats = {"month": month, "countries": [], "regenerated": 0}
    zones = zone_by_id()
    use_llm = _llm_available()

    for code, country in load_countries().items():
        if not country.weekly or (countries and code not in countries):
            continue

        # ── material semanal del mes, por tema ───────────────────────────
        rows = db.execute(
            select(WeeklyItem, Article).join(Article, WeeklyItem.article_id == Article.id)
            .where(WeeklyItem.country == code,
                   Article.published_at >= start, Article.published_at < end)
            .order_by(Article.published_at)
        ).all()
        sections: dict = {}
        for theme in WEEKLY_THEMES:
            items = [{
                "article_id": a.id, "url": a.url, "titulo": a.title,
                "resumen": w.mini_summary_es,
                "fecha": a.published_at.date().isoformat() if a.published_at else None,
                "fuente": a.source_name,
            } for w, a in rows if w.theme == theme]
            text = (_llm_section_text(country.name, theme, items) if use_llm and items else None) \
                or _compose_section_text(theme, items)
            sections[theme] = {"sintesis": text, "articulos": items}

        # ── tabla de incidentes: eventos del pipeline diario del mes ─────
        events = db.scalars(
            select(Event).where(
                Event.country == code,
                Event.status == EventStatus.published.value,
                Event.occurred_at >= start, Event.occurred_at < end,
            ).order_by(Event.occurred_at)
        )
        incidents = []
        for ev in events:
            zone = zones.get(ev.zone_id) if ev.zone_id else None
            incidents.append({
                "event_id": ev.id,
                "fecha": ev.occurred_at.date().isoformat() if ev.occurred_at else None,
                "localizacion": zone.name if zone else country.name,
                "nivel": ev.level,
                "categoria": CATEGORY_LABELS_ES.get(ev.category, ev.category),
                "descripcion": ev.summary_es or ev.title_es,
                "fuentes": [{"url": ea.article.url, "name": ea.article.source_name}
                            for ea in ev.articles],
            })

        # ── párrafo de apertura ──────────────────────────────────────────
        n_items = sum(len(s["articulos"]) for s in sections.values())
        overview = (
            f"Durante {month}, la vigilancia sobre {country.name} registró "
            f"{n_items} artículos relevantes y {len(incidents)} incidentes de seguridad. "
            + ("Sin incidentes de seguridad registrados en el período."
               if not incidents else
               f"Los incidentes se concentran en las categorías: "
               f"{', '.join(sorted({i['categoria'] for i in incidents}))}.")
        )

        existing = db.scalar(select(MonthlySynthesis).where(
            MonthlySynthesis.country == code, MonthlySynthesis.month == month))
        if existing:
            existing.overview_es, existing.sections, existing.incidents = overview, sections, incidents
            stats["regenerated"] += 1
        else:
            db.add(MonthlySynthesis(country=code, month=month, overview_es=overview,
                                    sections=sections, incidents=incidents))
        stats["countries"].append(code)
    db.commit()
    return stats
