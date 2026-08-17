"""El planificador interno: que los jobs se lancen solos.

Ninguna colecta automática corrió jamás en producción — los servicios de
cron que debían usar `railway/cron-*.json` no existían, y la única señal
era una ausencia. Estas pruebas cubren la decisión («¿toca?»), no el hilo.
"""
from datetime import datetime, timedelta, timezone

import pytest

from atalaya.db.models import CollectRun
from atalaya.jobs import scheduler


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _run(db, kind, started, finished=True):
    r = CollectRun(kind=kind, started_at=started, stats={"origin": "cron"},
                   finished_at=started + timedelta(minutes=25) if finished else None,
                   ok=True if finished else None)
    db.add(r)
    db.commit()
    return r


# ── lectura de la expresión ──────────────────────────────────────────────
def test_el_ultimo_horario_previsto_es_el_de_hoy():
    # 0 3,6,12 * * * en México = 09/12/18 UTC. A las 13:00 UTC el último
    # horario pasado es el de 12:00 UTC (06:00 en México).
    fire = scheduler.last_fire("0 3,6,12 * * *", "America/Mexico_City",
                               _utc(2026, 8, 17, 13, 30))
    assert fire == _utc(2026, 8, 17, 12)


def test_antes_del_primer_horario_se_mira_el_dia_anterior():
    fire = scheduler.last_fire("0 3,6,12 * * *", "America/Mexico_City",
                               _utc(2026, 8, 17, 2))
    assert fire == _utc(2026, 8, 16, 18)


def test_el_semanal_cae_en_su_dia():
    # viernes 07:00 México = viernes 13:00 UTC. El 17/08/2026 es lunes.
    fire = scheduler.last_fire("0 7 * * 5", "America/Mexico_City",
                               _utc(2026, 8, 17, 10))
    assert fire == _utc(2026, 8, 14, 13)     # el viernes anterior


def test_una_expresion_ilegible_no_se_interpreta_a_medias():
    """Tomarla por «todos los valores» lanzaría una colecta por minuto."""
    with pytest.raises(ValueError):
        scheduler.last_fire("0 25 * * *", "UTC", _utc(2026, 8, 17, 10))
    with pytest.raises(ValueError):
        scheduler.last_fire("cada hora", "UTC", _utc(2026, 8, 17, 10))


# ── ¿toca lanzar? ────────────────────────────────────────────────────────
EXPR, TZ = "0 3,6,12 * * *", "America/Mexico_City"


def test_sin_ningun_run_toca(db):
    assert scheduler.due(db, "daily", EXPR, TZ, _utc(2026, 8, 17, 13)) is True


def test_si_ya_corrio_despues_del_horario_no_toca(db):
    _run(db, "daily", _utc(2026, 8, 17, 12, 5))
    assert scheduler.due(db, "daily", EXPR, TZ, _utc(2026, 8, 17, 13)) is False


def test_un_horario_perdido_se_recupera(db):
    """Un planificador que duerme pierde para siempre el horario que le
    pilló reiniciando, y aquí cada despliegue reinicia el proceso."""
    _run(db, "daily", _utc(2026, 8, 17, 9, 10))     # corrió a las 9, no a las 12
    assert scheduler.due(db, "daily", EXPR, TZ, _utc(2026, 8, 17, 12, 40)) is True


def test_una_colecta_manual_satisface_el_horario(db):
    """No hay que recolectar dos veces lo que el analista acaba de pedir."""
    r = _run(db, "daily", _utc(2026, 8, 17, 12, 30))
    r.stats = {"origin": "manual"}
    db.commit()
    assert scheduler.due(db, "daily", EXPR, TZ, _utc(2026, 8, 17, 13)) is False


def test_un_run_fallido_cuenta_igual(db):
    """Contar solo los éxitos era una trampa: un despliegue mata la colecta,
    el planificador la relanza, el despliegue siguiente la vuelve a matar."""
    r = _run(db, "daily", _utc(2026, 8, 17, 12, 5))
    r.ok = False
    r.stats = {"interrupted": True}
    db.commit()
    assert scheduler.due(db, "daily", EXPR, TZ, _utc(2026, 8, 17, 13)) is False


def test_no_se_lanza_nada_mientras_haya_una_colecta_en_curso(db, monkeypatch):
    _run(db, "daily", datetime.now(timezone.utc) - timedelta(minutes=10),
         finished=False)
    lanzados = []
    monkeypatch.setattr(scheduler, "_lanzar", lambda kind: lanzados.append(kind))

    assert scheduler.tick() is None
    assert lanzados == []


def test_un_run_muerto_hace_horas_no_bloquea(db, monkeypatch):
    """Un proceso muerto deja su run abierto para siempre; pasadas dos horas
    deja de considerarse una colecta en curso."""
    _run(db, "daily", datetime.now(timezone.utc) - timedelta(hours=5),
         finished=False)
    lanzados = []
    monkeypatch.setattr(scheduler, "_lanzar", lambda kind: lanzados.append(kind))

    assert scheduler.tick() == "daily"
    assert lanzados == ["daily"]


def test_un_job_que_revienta_no_se_lleva_el_planificador(db, monkeypatch):
    def explota(kind):
        raise RuntimeError("la colecta falló")

    monkeypatch.setattr(scheduler, "_lanzar", explota)
    assert scheduler.tick() == "daily"       # se registra, no se propaga


def test_se_apaga_con_la_variable_de_entorno(monkeypatch):
    monkeypatch.setenv("ATALAYA_SCHEDULER", "off")
    assert scheduler.enabled() is False
    assert scheduler.start() is None
    monkeypatch.setenv("ATALAYA_SCHEDULER", "on")
    assert scheduler.enabled() is True


def test_solo_se_lanza_un_job_a_la_vez(db, monkeypatch):
    """La colecta satura la red del contenedor: dos en paralelo alargarían
    las dos. El resto espera al minuto siguiente, que llega enseguida."""
    # base vacía: los tres jobs tienen un horario pasado sin run posterior
    assert scheduler.pending_jobs(db, _utc(2026, 8, 17, 13)) == [
        "daily", "weekly", "monthly"]

    lanzados = []
    monkeypatch.setattr(scheduler, "_lanzar", lambda kind: lanzados.append(kind))
    assert scheduler.tick(_utc(2026, 8, 17, 13)) == "daily"
    assert lanzados == ["daily"]
