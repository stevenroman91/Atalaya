"""Planificador interno: los jobs se lanzan solos desde el servicio web.

Por qué existe. Los tres `railway/cron-*.json` están en el repo desde el
principio y ninguna colecta automática llegó a correr nunca: un fichero de
configuración no crea un servicio, y los tres servicios Railway que debían
usarlos no existían. Nadie lo vio durante días porque la única señal era una
ausencia — ninguna línea en la tabla de runs. Un servicio de vigilancia que
depende de que alguien se acuerde de pulsar un botón no vigila nada.

Cómo decide. No duerme hasta la hora siguiente: mira el reloj y la base.
Para cada job calcula **cuál fue el último horario previsto** y pregunta si
ya corrió algo desde entonces. Si no, lo lanza. Esa diferencia importa: un
planificador que duerme pierde para siempre el horario que le pilló
reiniciando —y aquí cada despliegue reinicia el proceso—, mientras que este
recupera el hueco en cuanto vuelve.

Convive con los crons de Railway sin duplicar nada: un run lanzado por un
contenedor de cron cuenta como «ya corrió este horario» igual que uno
manual. Si algún día se crean esos servicios, esto se apaga solo — o con
ATALAYA_SCHEDULER=off.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from atalaya.config import load_schedule
from atalaya.db.models import CollectRun

log = logging.getLogger(__name__)

# Un run sin terminar más viejo que esto es un proceso muerto (despliegue,
# crash), no una colecta en curso: el mismo criterio que usa el panel de
# administración para no bloquear el botón para siempre.
STALE_HOURS = 2


# ── lectura de la expresión cron ─────────────────────────────────────────
def _field(spec: str, low: int, high: int) -> set[int]:
    """Un campo cron → el conjunto de valores que acepta.

    Se admite lo que usamos: `*`, listas, rangos y pasos. Nada más: una
    expresión que no entendemos debe fallar aquí, a la vista, y no ser
    interpretada a medias como «todos los valores».
    """
    valores: set[int] = set()
    for parte in spec.split(","):
        paso = 1
        if "/" in parte:
            parte, _, s = parte.partition("/")
            paso = int(s)
        if parte in ("*", ""):
            desde, hasta = low, high
        elif "-" in parte:
            a, _, b = parte.partition("-")
            desde, hasta = int(a), int(b)
        else:
            desde = hasta = int(parte)
        if not (low <= desde <= high and low <= hasta <= high and desde <= hasta):
            raise ValueError(f"campo cron fuera de rango: {spec}")
        valores.update(range(desde, hasta + 1, paso))
    return valores


def _parse(expr: str) -> tuple[set[int], ...]:
    campos = expr.split()
    if len(campos) != 5:
        raise ValueError(f"expresión cron inválida: {expr!r}")
    rangos = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    return tuple(_field(c, lo, hi) for c, (lo, hi) in zip(campos, rangos))


def _coincide(campos: tuple[set[int], ...], dt: datetime) -> bool:
    minuto, hora, dia, mes, semana = campos
    return (dt.minute in minuto and dt.hour in hora and dt.month in mes
            # cron clásico: día del mes Y día de la semana se combinan en O
            # cuando ambos están restringidos. Nuestros jobs solo restringen
            # uno de los dos, así que la Y basta y es la lectura prudente.
            and dt.day in dia and (dt.isoweekday() % 7) in semana)


def last_fire(expr: str, tz_name: str, now: datetime,
              lookback_days: int = 40) -> datetime | None:
    """Último horario previsto por `expr` en o antes de `now` (en UTC).

    Se recorre hacia atrás minuto a minuto. Son unos miles de comparaciones
    enteras una vez por minuto: irrelevante frente a media hora de colecta,
    y evita escribir un calculador de fechas cron que habría que depurar.
    """
    campos = _parse(expr)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    actual = now.astimezone(tz).replace(second=0, microsecond=0)
    for i in range(lookback_days * 24 * 60):
        candidato = actual - timedelta(minutes=i)
        if _coincide(campos, candidato):
            return candidato.astimezone(timezone.utc)
    return None


# ── ¿toca lanzar? ────────────────────────────────────────────────────────
def _hay_run_en_curso(db: Session) -> bool:
    corte = datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)
    return db.scalar(select(CollectRun).where(
        CollectRun.finished_at.is_(None), CollectRun.started_at > corte)) is not None


def due(db: Session, kind: str, expr: str, tz_name: str,
        now: datetime | None = None) -> bool:
    """¿Ha pasado un horario previsto de `kind` sin que corriera nada?

    Cuenta cualquier run posterior al horario, haya terminado bien o mal.
    Contar solo los que acabaron en éxito parecía más estricto y era una
    trampa: un despliegue que mata la colecta la dejaría marcada como
    fallida, el planificador la relanzaría, el despliegue siguiente la
    volvería a matar. Un hueco espera al horario siguiente — hay tres al día.
    """
    now = now or datetime.now(timezone.utc)
    horario = last_fire(expr, tz_name, now)
    if horario is None:
        return False
    ultimo = db.scalar(select(CollectRun).where(CollectRun.kind == kind)
                       .order_by(CollectRun.started_at.desc()))
    if ultimo is not None:
        inicio = ultimo.started_at
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=timezone.utc)
        if inicio >= horario:
            return False
    return True


def pending_jobs(db: Session, now: datetime | None = None) -> list[str]:
    """Los jobs que habría que lanzar ahora mismo, del más urgente al menos.

    Nunca más de uno a la vez: la colecta satura la red del contenedor y
    lanzar dos en paralelo alargaría las dos. El resto espera al minuto
    siguiente, que llega enseguida.
    """
    cfg = load_schedule()
    pendientes = []
    for kind in ("daily", "weekly", "monthly"):
        bloque = cfg.get(kind) or {}
        expr, tz_name = bloque.get("cron"), bloque.get("timezone", "UTC")
        if not expr:
            continue
        try:
            if due(db, kind, expr, tz_name, now):
                pendientes.append(kind)
        except ValueError:
            log.error("horario de %s ilegible (%r): ese job no se lanzará solo",
                      kind, expr)
    return pendientes


# ── el hilo ──────────────────────────────────────────────────────────────
def enabled() -> bool:
    """Apagable sin desplegar código: ATALAYA_SCHEDULER=off. Sirve el día
    que existan los servicios de cron de Railway y sobre todo el día que
    haya que parar las colectas sin parar el sitio."""
    forzado = os.environ.get("ATALAYA_SCHEDULER", "").lower()
    if forzado in ("off", "0", "false"):
        return False
    if forzado in ("on", "1", "true"):
        return True
    return bool((load_schedule().get("scheduler") or {}).get("enabled", True))


def _lanzar(kind: str) -> None:
    from atalaya.db import SessionLocal
    from atalaya.jobs.runner import run_daily, run_monthly, run_weekly

    fn = {"daily": run_daily, "weekly": run_weekly, "monthly": run_monthly}[kind]
    with SessionLocal() as db:
        if kind == "monthly":
            fn(db)                      # run_monthly no lleva origen
        else:
            fn(db, origin="scheduler")


def tick(now: datetime | None = None) -> str | None:
    """Un latido: mira si toca algo y lo lanza. Devuelve el job lanzado."""
    from atalaya.db import SessionLocal

    with SessionLocal() as db:
        if _hay_run_en_curso(db):
            return None
        pendientes = pending_jobs(db, now)
    if not pendientes:
        return None
    kind = pendientes[0]
    log.info("planificador: lanzando el job %s", kind)
    try:
        _lanzar(kind)
    except Exception:
        # Un job que revienta no debe llevarse el planificador: el hueco lo
        # recogerá el horario siguiente, y el error queda en el run.
        log.exception("planificador: el job %s falló", kind)
    return kind


def start() -> threading.Thread | None:
    """Arranca el hilo del planificador junto al servicio web."""
    if not enabled():
        log.info("planificador desactivado (ATALAYA_SCHEDULER)")
        return None
    intervalo = float((load_schedule().get("scheduler") or {})
                      .get("poll_seconds", 60))

    def bucle():
        # La primera espera evita lanzar una colecta en el arranque de un
        # proceso efímero (un test, un contenedor que se reinicia en bucle).
        while True:
            time.sleep(intervalo)
            try:
                tick()
            except Exception:
                log.exception("planificador: latido fallido")

    hilo = threading.Thread(target=bucle, daemon=True, name="scheduler")
    hilo.start()
    log.info("planificador arrancado (cada %.0f s)", intervalo)
    return hilo
