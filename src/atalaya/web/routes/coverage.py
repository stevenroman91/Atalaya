"""Cobertura por país (§7): qué se consultó, qué dio material y qué hay que
mirar a mano.

La lista plana de fuentes del panel de administración no contestaba la
pregunta que se hace el analista todas las mañanas: «para este país, ¿qué he
mirado de verdad?». Un medio puede estar verde en la lista y no haber dado
un solo artículo pertinente; otro puede fallar desde hace tres días sin que
nadie lo note porque Google News tapa el agujero. Aquí se agrupa por país y
cada fuente lleva un veredicto explícito.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atalaya.config import load_countries, load_sources
from atalaya.db.models import Article, ArticleStatus, Event, EventStatus, SourceRecord
from atalaya.web.deps import get_db, render, require_admin

router = APIRouter(prefix="/admin")

WINDOW_HOURS = 24


def _verdict(rec: SourceRecord | None, kept: int, rejected: int) -> tuple[str, str]:
    """(clave de estado, detalle). El estado ordena y colorea; el detalle
    dice qué hacer. Nunca «ok» a secas: «ok» sin artículos es una fuente que
    responde y no aporta, y eso también hay que poder verlo."""
    if kept:
        return "produce", f"{kept} artículo(s) retenido(s)"
    if rec is not None and rec.consecutive_failures:
        # el fallo manda sobre «nunca consultada»: dice qué hacer, y una
        # fuente que nunca funcionó y además falla es un caso de fallo
        detalle = f"{rec.consecutive_failures} fallo(s) seguidos"
        if rec.last_error:
            detalle += f" — {rec.last_error[:120]}"
        return "inalcanzable", detalle
    if rec is None or rec.last_ok_at is None:
        return "sin_datos", "nunca se ha consultado con éxito"
    if rejected:
        return "filtrado", f"{rejected} artículo(s) descartado(s) por los filtros"
    return "sin_material", "responde, pero nada pertinente en la ventana"


ORDER = {"inalcanzable": 0, "sin_datos": 1, "sin_material": 2,
         "filtrado": 3, "produce": 4}


@router.get("/cobertura")
def coverage(request: Request, user_sess=Depends(require_admin),
             db: Session = Depends(get_db)):
    user, sess = user_sess
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)

    # (dominio, país, estado) → nº de artículos, en una sola consulta
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for domain, code, status, n in db.execute(
            select(Article.domain, Article.country, Article.status,
                   func.count(Article.id))
            .where(Article.fetched_at >= since)
            .group_by(Article.domain, Article.country, Article.status)):
        counts.setdefault((domain, code), {})[status] = n

    records = {r.domain: r for r in db.scalars(select(SourceRecord))}
    events = dict(db.execute(
        select(Event.country, func.count(Event.id))
        .where(Event.created_at >= since,
               Event.status.in_([EventStatus.published.value,
                                 EventStatus.pending_confirm.value]))
        .group_by(Event.country)).all())

    sources = load_sources()
    bloques = []
    for code, country in load_countries().items():
        if not country.daily:
            continue
        filas = []
        for src in sources:
            if not src.covers_country(code):
                continue
            rec = records.get(src.domain)
            c = counts.get((src.domain, code), {})
            kept = (c.get(ArticleStatus.extracted.value, 0)
                    + c.get(ArticleStatus.title_only.value, 0))
            rejected = c.get(ArticleStatus.rejected.value, 0)
            estado, detalle = _verdict(rec, kept, rejected)
            filas.append({
                "name": src.name, "domain": src.domain, "type": src.type,
                "alcance": "regional" if "*" in src.covers else "nacional",
                "rss": src.rss or (rec.discovered_rss if rec else None),
                "rss_origen": "configurado" if src.rss else (
                    "autodescubierto" if rec and rec.discovered_rss else None),
                "kept": kept, "rejected": rejected,
                "estado": estado, "detalle": detalle,
                "probe_note": rec.probe_note if rec else None,
                "last_ok": rec.last_ok_at if rec else None,
            })
        filas.sort(key=lambda r: (ORDER[r["estado"]], -r["kept"], r["name"]))
        bloques.append({
            "code": code, "name": country.name,
            "rows": filas,
            "events": events.get(code, 0),
            "produce": sum(1 for r in filas if r["estado"] == "produce"),
            "revisar": sum(1 for r in filas
                           if r["estado"] in ("inalcanzable", "sin_datos")),
            "total": len(filas),
        })

    return render(request, "cobertura.html", user=user, csrf=sess.csrf_token,
                  bloques=bloques, window_hours=WINDOW_HOURS)
