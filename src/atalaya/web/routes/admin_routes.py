"""Espace admin (§6.1): invitations, comptes, santé de la collecte."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from sqlalchemy import func

from atalaya.config import load_schedule
from atalaya.db.models import Article, CollectRun, Invitation, SourceRecord, User
from atalaya.web import auth as auth_layer
from urllib.parse import quote_plus

from atalaya.web.deps import check_csrf, get_db, render, require_admin

router = APIRouter(prefix="/admin")


log = logging.getLogger(__name__)

# Un solo diagnóstico a la vez, en todo el proceso. Sin esto, dos clics
# seguidos lanzan dos hilos con su propio fetcher: cada uno respeta el
# retardo de cortesía por su cuenta e ignora al otro, así que juntos
# martillean el mismo host. Es exactamente lo que pasó con GDELT — el 429
# no venía de su API caprichosa, venía de nosotros dos veces.
_PROBE_LOCK = threading.Lock()


def _releasing(fn):
    """Envuelve el cuerpo de un hilo de diagnóstico: pase lo que pase, el
    cerrojo se suelta. Un hilo que muere con el cerrojo en la mano deja el
    botón inutilizable hasta el próximo despliegue."""
    def wrapped():
        try:
            fn()
        except Exception:
            log.exception("hilo de diagnóstico falló")
        finally:
            _PROBE_LOCK.release()
    return wrapped


@router.get("")
def admin_home(request: Request, invite_link: str | None = None, error: str | None = None,
               notice: str | None = None, probe: str | None = None,
               probe_msg: str | None = None, swept: str | None = None,
               user_sess=Depends(require_admin), db: Session = Depends(get_db)):
    user, sess = user_sess
    invitations = list(db.scalars(select(Invitation).order_by(desc(Invitation.created_at)).limit(50)))
    users = list(db.scalars(select(User).order_by(User.email)))
    alert_days = int(load_schedule().get("collector", {}).get("source_failure_alert_days", 3))
    sources = list(db.scalars(select(SourceRecord).order_by(
        desc(SourceRecord.consecutive_failures), SourceRecord.domain)))
    runs = list(db.scalars(select(CollectRun).order_by(desc(CollectRun.started_at)).limit(20)))
    active_run = db.scalar(_active_run_query())
    progress = None
    if active_run is not None:
        stored = db.scalar(select(func.count(Article.id))
                           .where(Article.run_id == active_run.id)) or 0
        progress = {"done": active_run.progress_done,
                    "total": active_run.progress_total,
                    "stored": stored, "run_id": active_run.id}
    from atalaya.web.routes.coverage import api_rows

    return render(request, "admin.html", user=user, csrf=sess.csrf_token,
                  apis=api_rows(db),
                  invitations=invitations, users=users, sources=sources,
                  runs=runs, invite_link=invite_link, error=error, notice=notice,
                  collect_running=active_run is not None, progress=progress,
                  failing_threshold=alert_days, probe=probe, probe_msg=probe_msg,
                  swept=swept)


def _active_run_query():
    """Runs sin terminar y recientes. Un run cuyo proceso murió (redeploy,
    restart) queda con finished_at NULL para siempre: pasadas 2 h se ignora
    para no bloquear el botón de colecta manual."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    return (select(CollectRun)
            .where(CollectRun.finished_at.is_(None), CollectRun.started_at > cutoff)
            .order_by(desc(CollectRun.started_at)))


def _collect_in_background(kind: str) -> None:
    """Ejecuta un job de colecta en un hilo con su propia sesión DB."""
    from atalaya.db import SessionLocal
    from atalaya.jobs.runner import run_daily, run_weekly

    def worker():
        try:
            with SessionLocal() as job_db:
                (run_daily if kind == "daily" else run_weekly)(job_db, origin="manual")
        except Exception:
            log.exception("colecta %s lanzada desde admin falló", kind)

    threading.Thread(target=worker, daemon=True, name=f"collect-{kind}").start()


@router.post("/collect-now")
async def collect_now(request: Request, user_sess=Depends(require_admin),
                      db: Session = Depends(get_db)):
    await check_csrf(request, user_sess)
    in_progress = db.scalar(_active_run_query())
    if in_progress is not None:
        return RedirectResponse("/admin?notice=running", status_code=303)
    _collect_in_background("daily")
    return RedirectResponse("/admin?notice=started", status_code=303)


def probe_domain(domain: str, fetcher=None) -> str:
    """Qué daría leer la portada de esta fuente. Mide, no escribe.

    Existe porque la lectura de portada se programó sin poder verla nunca:
    el entorno de desarrollo no tiene salida a internet. Devuelve números
    —cuántos enlaces, cuántos con forma de artículo, cuántos pertinentes—
    para decidir con datos, no con suposiciones.
    """
    from atalaya.collect.collector import Collector
    from atalaya.collect.fetcher import PoliteFetcher
    from atalaya.collect.whitelist import norm_domain, off_topic_section

    d = norm_domain(domain)
    f = fetcher or PoliteFetcher()
    resp = f.get(f"https://{d}/")
    if not resp:
        # «inalcanzable» a secas no dice qué hacer. Un 403 y un robots.txt
        # que nos prohíbe no piden la misma respuesta — y ante el segundo la
        # respuesta correcta es no volver a llamar.
        clave, texto = getattr(f, "last_failure", None) or ("red", "sin respuesta")
        return f"portada inalcanzable — {texto}"

    base = str(getattr(resp, "url", "") or f"https://{d}/")
    html = resp.text or ""
    anchors = len(Collector._LINK_RE.findall(html))
    links = Collector._article_links_from_html(base, html, norm_domain(base))
    useful = [(u, t) for u, t in links if not off_topic_section(u)]
    # Encontrar artículos manda sobre el recuento bruto: una portada sobria
    # con 4 enlaces útiles vale más que una con 200 de navegación.
    if not links and anchors < 10:
        return ("casi sin enlaces en el HTML: portada construida por "
                "JavaScript — leerla no aportaría nada")
    if not links:
        # Sin muestras no se puede arreglar el filtro sin adivinar. Se
        # enseñan rutas reales del propio dominio para ver su forma.
        muestras = Collector._rejected_paths(base, html, norm_domain(base))[:4]
        detalle = (" Rutas de ejemplo: " + " · ".join(muestras)) if muestras else ""
        return (f"{anchors} enlaces, pero ninguno con forma de artículo: "
                f"sus URL no encajan con el filtro.{detalle}")
    if not useful:
        return f"{len(links)} artículos, todos en secciones ajenas a la vigilancia"
    return (f"{anchors} enlaces · {len(links)} con forma de artículo · "
            f"{len(useful)} pertinentes. Ejemplo: «{useful[0][1][:70]}»")


def probe_api(key: str, cfg: dict, fetcher=None) -> tuple[bool, str]:
    """(¿utilizable?, veredicto legible) para un punto de entrada de API.

    Es el equivalente de `probe_domain` para las API abiertas, y existe por
    la misma razón: el entorno de desarrollo no tiene salida a internet, así
    que la única prueba real posible ocurre desde producción. Mientras esta
    prueba no pase, el colector ignora la API — nada entra en base porque la
    URL «parecía correcta».
    """
    from atalaya.collect.apis import parse_gdacs, parse_gdelt, parse_usgs
    from atalaya.collect.fetcher import PoliteFetcher

    url = cfg.get("url") or ""
    if not url:
        return False, "sin URL configurada"
    kind = cfg.get("kind")
    if kind == "gdelt_doc":
        url = (f"{url}?query=%22M%C3%A9xico%22&mode=artlist&format=json"
               f"&maxrecords=5&timespan=1d")
    f = fetcher or PoliteFetcher()
    resp = f.get(url, retries=2)      # GDELT corta la conexión de vez en cuando
    if not resp:
        _, texto = getattr(f, "last_failure", None) or ("red", "sin respuesta")
        return False, f"sin respuesta — {texto}"
    try:
        if kind == "gdelt_doc":
            items = parse_gdelt(resp.json())
            muestra = items[0]["title"] if items else ""
        elif kind == "usgs_geojson":
            items = parse_usgs(resp.json(), float(cfg.get("min_magnitude", 4.0)))
            muestra = items[0].title if items else ""
        elif kind == "gdacs_rss":
            items = parse_gdacs(resp.text, str(cfg.get("min_level", "orange")))
            muestra = items[0].title if items else ""
        else:
            return False, f"tipo desconocido: {kind}"
    except Exception as exc:
        return False, f"respuesta no parseable: {type(exc).__name__}"

    # Cero elementos NO es un fallo: significa que hoy no hay nada por encima
    # del umbral. Lo que se prueba es que la respuesta se parsea.
    detalle = f"{len(items)} elemento(s) pertinente(s)"
    if muestra:
        detalle += f". Ejemplo: «{muestra[:70]}»"
    return True, detalle


def _probe_apis_in_background() -> bool:
    """Prueba las API una tras otra y guarda el veredicto.

    Devuelve False si ya hay un diagnóstico en marcha: mejor decirlo que
    duplicar las peticiones a espaldas del que las recibe.
    """
    from atalaya.collect.fetcher import PoliteFetcher
    from atalaya.config import load_apis
    from atalaya.db import SessionLocal

    if not _PROBE_LOCK.acquire(blocking=False):
        return False

    def probar_todas():
        fetcher = PoliteFetcher()
        with SessionLocal() as job_db:
            for key, cfg in load_apis().items():
                domain = cfg.get("domain")
                if not domain:
                    continue
                try:
                    ok, note = probe_api(key, cfg, fetcher)
                except Exception as exc:
                    log.exception("prueba de la API %s falló", key)
                    ok, note = False, f"la prueba falló: {type(exc).__name__}"
                rec = job_db.scalar(select(SourceRecord)
                                    .where(SourceRecord.domain == domain))
                if rec is None:
                    rec = SourceRecord(domain=domain, name=cfg.get("name", key))
                    job_db.add(rec)
                rec.probe_note = note
                rec.probe_at = datetime.now(timezone.utc)
                if ok:
                    # last_ok_at es la llave: el colector no toca una API sin él
                    rec.last_ok_at = datetime.now(timezone.utc)
                    rec.consecutive_failures = 0
                    rec.last_error = None
                else:
                    # el default de la columna solo se aplica al INSERT: en una
                    # fila recién añadida y aún sin flush, el valor es None y
                    # `+= 1` reventaba el hilo entero
                    rec.consecutive_failures = (rec.consecutive_failures or 0) + 1
                    rec.last_error = note[:500]
                job_db.commit()

    worker = _releasing(probar_todas)

    threading.Thread(target=worker, daemon=True, name="probe-apis").start()
    return True


@router.post("/probe-apis")
async def probe_apis(request: Request, user_sess=Depends(require_admin),
                     db: Session = Depends(get_db)):
    await check_csrf(request, user_sess)
    # aviso propio: el genérico decía «un par de minutos» y mandaba recargar
    # sin decir dónde mirar. Son tres peticiones: unos segundos.
    lanzado = _probe_apis_in_background()
    return RedirectResponse(
        "/admin?notice=probing_apis" if lanzado else "/admin?notice=probe_busy",
        status_code=303)


def _probe_all_in_background() -> bool:
    """Diagnostica en un hilo todas las fuentes en fallo, una tras otra.

    Secuencial a propósito: son peticiones a sitios que ya nos cuestan
    trabajo, y la cortesía del fetcher no se negocia por comodidad. Por lo
    mismo, uno a la vez: dos hilos a la vez no son dos veces más rápidos,
    son dos veces más groseros.
    """
    from atalaya.collect.fetcher import PoliteFetcher
    from atalaya.db import SessionLocal

    if not _PROBE_LOCK.acquire(blocking=False):
        return False

    def diagnosticar_todas():
        fetcher = PoliteFetcher()
        with SessionLocal() as job_db:
            failing = list(job_db.scalars(
                select(SourceRecord).where(SourceRecord.consecutive_failures > 0)))
            for src in failing:
                try:
                    note = probe_domain(src.domain, fetcher)
                except Exception as exc:                      # una fuente no tumba el barrido
                    log.exception("diagnóstico de %s falló", src.domain)
                    note = f"el diagnóstico falló: {type(exc).__name__}"
                src.probe_note = note
                src.probe_at = datetime.now(timezone.utc)
                job_db.commit()                               # visible al recargar

    worker = _releasing(diagnosticar_todas)

    threading.Thread(target=worker, daemon=True, name="probe-all").start()
    return True


@router.post("/sweep")
async def sweep(request: Request, user_sess=Depends(require_admin),
                db: Session = Depends(get_db)):
    """Repasa el panel con los filtros vigentes, sin recolectar.

    Sin este botón, corregir un filtro obligaba a lanzar una colecta entera
    —media hora— para ver el efecto. El barrido no toca la red: relee lo que
    ya está en base y devuelve las cifras en el acto.
    """
    await check_csrf(request, user_sess)
    from atalaya.process.pipeline import sweep_events

    s = sweep_events(db)
    msg = (f"{s['retired']} retirados · {s['reattributed']} reatribuidos · "
           f"{s['geocoded']} geolocalizados · {s['retitled']} titulares limpiados")
    return RedirectResponse(f"/admin?notice=swept&swept={quote_plus(msg)}",
                            status_code=303)


@router.post("/probe-all")
async def probe_all(request: Request, user_sess=Depends(require_admin),
                    db: Session = Depends(get_db)):
    await check_csrf(request, user_sess)
    lanzado = _probe_all_in_background()
    return RedirectResponse(
        "/admin?notice=probing" if lanzado else "/admin?notice=probe_busy",
        status_code=303)


@router.post("/probe-home")
async def probe_home(request: Request, user_sess=Depends(require_admin),
                     db: Session = Depends(get_db)):
    """¿Qué daría leer la portada de una fuente sin flujo? Mide, no escribe.

    Existe porque la lectura de portada se programó sin poder verla nunca:
    el entorno de desarrollo no tiene salida a internet. Este botón mira la
    página de verdad y devuelve números — cuántos enlaces, cuántos parecen
    artículos, cuántos sobreviven al filtro de sección — para decidir con
    datos si vale la pena encenderla. No toca la base ni la salud de la
    fuente: es una consulta, no una colecta.
    """
    await check_csrf(request, user_sess)
    form = await request.form()
    domain = (form.get("domain") or "").strip()
    if not domain:
        return RedirectResponse("/admin", status_code=303)

    src = db.scalar(select(SourceRecord).where(SourceRecord.domain == domain))
    note = probe_domain(domain)
    if src is not None:
        src.probe_note = note
        src.probe_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/collect-cancel")
async def collect_cancel(request: Request, user_sess=Depends(require_admin),
                         db: Session = Depends(get_db)):
    await check_csrf(request, user_sess)
    run = db.scalar(_active_run_query())
    if run is None:
        return RedirectResponse("/admin", status_code=303)
    run.cancel_requested = True
    db.commit()
    return RedirectResponse("/admin?notice=cancelling", status_code=303)


@router.post("/invite")
async def invite(request: Request, user_sess=Depends(require_admin),
                 db: Session = Depends(get_db)):
    user, _ = user_sess
    await check_csrf(request, user_sess)
    form = await request.form()
    try:
        token = auth_layer.create_invitation(
            db, email=str(form.get("email", "")),
            role=str(form.get("role", "analista")), created_by=user.id)
    except auth_layer.AuthError as exc:
        return RedirectResponse(f"/admin?error={exc.code}", status_code=303)
    base = os.environ.get("ATALAYA_BASE_URL", str(request.base_url).rstrip("/"))
    link = f"{base}/auth/invite/{token}"
    return RedirectResponse(f"/admin?invite_link={link}", status_code=303)


@router.post("/users/{user_id}/toggle")
async def toggle_user(request: Request, user_id: int,
                      user_sess=Depends(require_admin), db: Session = Depends(get_db)):
    admin, _ = user_sess
    await check_csrf(request, user_sess)
    target = db.get(User, user_id)
    if target and target.id != admin.id:
        target.active = not target.active
        db.commit()
    return RedirectResponse("/admin", status_code=303)
