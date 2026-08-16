"""Cobertura (§7): qué se consultó, qué dio material y qué hay que mirar a
mano. No es una página: es una sección al pie del panel.

La lista plana de fuentes del panel de administración no contestaba la
pregunta que se hace el analista todas las mañanas: «para este país, ¿qué he
mirado de verdad?». Un medio puede estar verde en la lista y no haber dado
un solo artículo pertinente; otro puede fallar desde hace tres días sin que
nadie lo note porque Google News tapa el agujero.

Tuvo una página propia durante media hora. Era un error: repetía las
pestañas y los filtros del panel para responder a una pregunta que solo
tiene sentido pegada a los eventos que se están leyendo. Ahora es un
desplegable al final del panel, sobre los mismos filtros.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atalaya.config import load_apis, load_countries, load_sources
from atalaya.db.models import (
    Article, ArticleStatus, Event, EventStatus, Reject, SourceRecord,
)

WINDOW_HOURS = 24
MAX_ARTICLES = 4000     # techo de seguridad de la consulta
PER_SOURCE = 40         # enlaces desplegados por fuente y país


# Firmas del motivo real, leídas del último error guardado. La lista de
# «revisar a mano» solo sirve si contiene cosas que se pueden arreglar: un
# robots.txt que nos prohíbe no se arregla, se acata.
_CAUSAS = (
    ("robots", ("robots.txt",)),
    ("bloqueada", ("nos responde 403", "nos responde 401", "nos responde 429",
                   "403 Forbidden")),
    ("dns", ("no resuelve",)),
    ("tls", ("certificado",)),
)


def _causa(error: str | None) -> str:
    hay = (error or "").lower()
    for clave, firmas in _CAUSAS:
        if any(f.lower() in hay for f in firmas):
            return clave
    return "inalcanzable"


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
            detalle += f" — {rec.last_error[:160]}"
        return _causa(rec.last_error), detalle
    if rec is None or rec.last_ok_at is None:
        return "sin_datos", "nunca se ha consultado con éxito"
    if rejected:
        return "filtrado", f"{rejected} artículo(s) descartado(s) por los filtros"
    return "sin_material", "responde, pero nada pertinente en la ventana"


def api_rows(db: Session) -> list[dict]:
    """Estado de las API abiertas. Compartido con el panel de administración:
    quien pulsa «Probar las API» está en /admin y debe ver ahí el resultado,
    sin tener que adivinar en qué otra página mirar."""
    records = {r.domain: r for r in db.scalars(select(SourceRecord))}
    out = []
    for key, cfg in load_apis().items():
        rec = records.get(cfg.get("domain") or "")
        probada = rec is not None and rec.last_ok_at is not None
        # El estado debe describir la ÚLTIMA prueba, no la mejor de todas.
        # Mostrar «probada y activa» junto a «el sitio nos responde 429» era
        # una contradicción: el sello venía de un intento anterior.
        fallando = bool(rec is not None and rec.consecutive_failures)
        out.append({
            "key": key, "name": cfg.get("name", key),
            "url": cfg.get("url"), "domain": cfg.get("domain"),
            "enabled": bool(cfg.get("enabled")),
            "verified": probada,
            "estado": ("off" if not cfg.get("enabled") else
                       "fallando" if probada and fallando else
                       "ok" if probada else "untested"),
            "note": rec.probe_note if rec else None,
            "probe_at": rec.probe_at if rec else None,
            "last_ok": rec.last_ok_at if rec else None,
        })
    return out


# Orden de lectura: primero lo que exige una decisión nuestra, al final lo
# que funciona. `robots` va abajo del bloque de fallos: no hay nada que hacer.
ORDER = {"dns": 0, "inalcanzable": 1, "bloqueada": 2, "tls": 3, "sin_datos": 4,
         "robots": 5, "sin_material": 6, "filtrado": 7, "produce": 8}
# Lo que cuenta como «a revisar a mano»: solo lo accionable.
ACCIONABLE = ("dns", "inalcanzable", "tls", "sin_datos")


# Agrupaciones que el analista ya lee en los contadores: poder filtrar por
# ellas es poder pinchar en la cifra que le interesa.
GRUPOS = {
    "revisar": ACCIONABLE,
    "cerradas": ("bloqueada", "robots"),
}


def _pasa_filtros(fila: dict, estado: str | None, flujo: str | None) -> bool:
    if estado:
        permitidos = GRUPOS.get(estado, (estado,))
        if fila["estado"] not in permitidos:
            return False
    if flujo == "con" and not fila["rss"]:
        return False
    if flujo == "sin" and fila["rss"]:
        return False
    if flujo in ("configurado", "autodescubierto") and fila["rss_origen"] != flujo:
        return False
    return True


def coverage_blocks(db: Session, codes: list[str] | None = None,
                    estado: str | None = None,
                    flujo: str | None = None) -> list[dict]:
    """Cobertura por país, para los países pedidos (todos si no se dice).

    Vive en el panel, al pie de la lista de eventos y filtrada por el país
    que el analista está mirando: la pregunta «¿qué se ha consultado?» solo
    tiene sentido pegada a los eventos que se están leyendo. Una pestaña
    aparte la habría convertido en una página que nadie abre.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)

    # Los artículos mismos, no solo su recuento: el analista quiere abrir la
    # lista y pinchar la URL para juzgar por sí mismo lo que se ha leído —y
    # sobre todo lo que se ha descartado, que es donde se esconden nuestros
    # errores de filtro. Un número solo no se puede contradecir.
    listas: dict[tuple[str, str], dict[str, list]] = {}
    for art in db.scalars(
            select(Article).where(Article.fetched_at >= since)
            .order_by(Article.fetched_at.desc()).limit(MAX_ARTICLES)):
        cubo = listas.setdefault((art.domain, art.country or ""), {})
        clave = ("rejected" if art.status == ArticleStatus.rejected.value
                 else "kept")
        cubo.setdefault(clave, []).append({
            "url": art.url, "title": art.title,
            "reason": art.reject_reason,
            "at": art.fetched_at,
        })

    # Los rechazados que nunca llegaron a ser artículos: hasta ahora
    # desaparecían sin dejar nada, y el desplegable «descartados» salía
    # vacío en todas las fuentes. Son los que el analista puede discutir.
    for rej in db.scalars(
            select(Reject).where(Reject.created_at >= since)
            .order_by(Reject.created_at.desc()).limit(MAX_ARTICLES)):
        cubo = listas.setdefault((rej.domain or "", rej.country or ""), {})
        cubo.setdefault("rejected", []).append({
            "url": rej.url, "title": rej.title or rej.url,
            "reason": rej.reason, "at": rej.created_at,
        })

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
        if not country.daily or (codes and code not in codes):
            continue
        filas = []
        for src in sources:
            if not src.covers_country(code):
                continue
            rec = records.get(src.domain)
            cubo = listas.get((src.domain, code), {})
            retenidos = cubo.get("kept", [])
            descartados = cubo.get("rejected", [])
            # OJO: no llamar `estado` a esta variable — es el nombre del
            # parámetro de filtro de la función, y reasignarlo dejaba el
            # filtro con el veredicto de la última fuente del bucle. La
            # tabla salía vacía sin que nada fallara.
            veredicto, detalle = _verdict(rec, len(retenidos), len(descartados))
            filas.append({
                "name": src.name, "domain": src.domain, "type": src.type,
                "alcance": "regional" if "*" in src.covers else "nacional",
                "rss": src.rss or (rec.discovered_rss if rec else None),
                "rss_origen": "configurado" if src.rss else (
                    "autodescubierto" if rec and rec.discovered_rss else None),
                "kept": len(retenidos), "rejected": len(descartados),
                "articulos": retenidos[:PER_SOURCE],
                "descartados": descartados[:PER_SOURCE],
                "estado": veredicto, "detalle": detalle,
                "probe_note": rec.probe_note if rec else None,
                "probe_at": rec.probe_at if rec else None,
                "last_ok": rec.last_ok_at if rec else None,
                # a dónde ir a mirar a mano cuando la fuente no responde
                "home": f"https://{src.domain}/",
            })
        filas.sort(key=lambda r: (ORDER[r["estado"]], -r["kept"], r["name"]))
        # Los contadores se calculan SIEMPRE sobre todas las fuentes: son la
        # realidad del país. Filtrar la tabla no debe cambiar la verdad que
        # se anuncia encima de ella — solo lo que se muestra.
        visibles = [r for r in filas if _pasa_filtros(r, estado, flujo)]
        bloques.append({
            "code": code, "name": country.name,
            "rows": visibles,
            "shown": len(visibles), "filtrado": len(visibles) != len(filas),
            "events": events.get(code, 0),
            "produce": sum(1 for r in filas if r["estado"] == "produce"),
            "revisar": sum(1 for r in filas if r["estado"] in ACCIONABLE),
            "bloqueadas": sum(1 for r in filas
                              if r["estado"] in ("bloqueada", "robots")),
            "total": len(filas),
            "kept": sum(r["kept"] for r in filas),
            "rejected": sum(r["rejected"] for r in filas),
        })

    return bloques
