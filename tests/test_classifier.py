"""El clasificador de pertinencia: lo que decide y lo que NO decide.

No redacta nada — los resúmenes siguen siendo estrictamente extractivos —,
y no borra nada: un hecho que declara ajeno a la seguridad baja a nota con
su motivo a la vista, y el analista puede contradecirlo.

Ninguna prueba llama a la API: se sustituye `classifier.classify`.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from atalaya.db.models import Article, ArticleStatus, CollectRun, Event
from atalaya.process import classifier
from atalaya.process.pipeline import process_daily


@pytest.fixture
def veredictos(monkeypatch):
    """Sustituye al modelo. Devuelve el registro de lo que se le preguntó."""
    llamadas = []

    def falso(title, summary, country_name):
        llamadas.append((title, summary, country_name))
        if "llorar" in title.lower() or "virgen" in title.lower():
            return {"es_seguridad": False, "categoria": "no_securitario",
                    "motivo": "espectáculos; no afecta a la seguridad"}
        return {"es_seguridad": True, "categoria": "crimen_alto_impacto",
                "motivo": "violencia armada con víctimas"}

    monkeypatch.setattr(classifier, "backend", lambda: "claude")
    monkeypatch.setattr(classifier, "classify", falso)
    return llamadas


def _art(db, run, titulo, url, **kw):
    d = dict(run_id=run.id, url=url, domain=url.split("/")[2],
             source_name=url.split("/")[2], source_type="independiente",
             title=titulo,
             text="Una balacera dejó dos heridos en la vía pública este "
                  "viernes. Sujetos armados dispararon contra un grupo de "
                  "personas y huyeron. La policía acordonó la zona.",
             published_at=datetime.now(timezone.utc) - timedelta(hours=2),
             country="MX", lang="es", status=ArticleStatus.extracted.value)
    d.update(kw)
    a = Article(**d)
    db.add(a)
    db.flush()
    return a


def _run(db):
    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    return run


def test_lo_ajeno_a_la_seguridad_baja_a_nota_pero_no_desaparece(db, veredictos):
    """«Luck Ra se largó a llorar en pleno show» salió como ALERTA crimen de
    alto impacto con dos fuentes. Ninguna lista de palabras arregla eso."""
    run = _run(db)
    for i, dom in enumerate(("tn.com.ar", "pagina12.com.ar")):
        _art(db, run, "Luck Ra se largó a llorar en pleno show por las críticas",
             f"https://{dom}/nota-{i}")

    process_daily(db, run, countries_filter=["MX"])

    ev = db.scalar(select(Event))
    assert ev is not None                      # NO se ha borrado
    assert ev.category == "no_securitario"
    assert ev.event_type == "NOTA"             # nunca una alerta
    assert ev.recommendations_es in (None, [])  # ni recomendaciones inventadas
    assert "espectáculos" in ev.score_detail["clasificador"]["motivo"]


def test_un_hecho_de_seguridad_conserva_su_alerta(db, veredictos):
    run = _run(db)
    for i, dom in enumerate(("eluniversal.com.mx", "reforma.com")):
        _art(db, run, "Balacera deja dos heridos en el centro de Culiacán",
             f"https://{dom}/nota-{i}")

    process_daily(db, run, countries_filter=["MX"])

    ev = db.scalar(select(Event))
    assert ev.event_type == "ALERTA"
    assert ev.category == "crimen_alto_impacto"


def test_el_veredicto_no_se_vuelve_a_pagar_si_el_texto_no_cambia(db, veredictos):
    run = _run(db)
    for i, dom in enumerate(("eluniversal.com.mx", "reforma.com")):
        _art(db, run, "Balacera deja dos heridos en el centro de Culiacán",
             f"https://{dom}/nota-{i}")

    process_daily(db, run, countries_filter=["MX"])
    primera = len(veredictos)
    process_daily(db, run, countries_filter=["MX"])   # re-run idéntico

    assert len(veredictos) == primera                 # cache por huella


def test_sin_backend_se_conserva_la_clasificacion_lexica(db, monkeypatch):
    """Sin clave de API, sin dependencia o con la API caída, la colecta no
    cambia de comportamiento."""
    monkeypatch.setattr(classifier, "backend", lambda: "none")
    run = _run(db)
    for i, dom in enumerate(("eluniversal.com.mx", "reforma.com")):
        _art(db, run, "Balacera deja dos heridos en el centro de Culiacán",
             f"https://{dom}/nota-{i}")

    process_daily(db, run, countries_filter=["MX"])

    ev = db.scalar(select(Event))
    assert ev.category == "crimen_alto_impacto"
    assert (ev.score_detail or {}).get("clasificador") is None


def test_un_fallo_del_modelo_no_tumba_el_tratamiento(db, monkeypatch):
    monkeypatch.setattr(classifier, "backend", lambda: "claude")
    monkeypatch.setattr(classifier, "classify",
                        lambda *a, **k: None)        # como una API caída
    run = _run(db)
    for i, dom in enumerate(("eluniversal.com.mx", "reforma.com")):
        _art(db, run, "Balacera deja dos heridos en el centro de Culiacán",
             f"https://{dom}/nota-{i}")

    stats = process_daily(db, run, countries_filter=["MX"])

    assert db.scalar(select(Event)).category == "crimen_alto_impacto"
    assert stats["classifier_failed"] == 1


def test_el_modelo_juzga_el_texto_que_vera_el_analista(db, veredictos):
    """No el material bruto: el titular y el resumen ya construidos."""
    run = _run(db)
    _art(db, run, "Balacera deja dos heridos en el centro de Culiacán",
         "https://eluniversal.com.mx/nota-0")
    _art(db, run, "Balacera deja dos heridos en el centro de Culiacán",
         "https://reforma.com/nota-1")

    process_daily(db, run, countries_filter=["MX"])

    titulo, resumen, pais = veredictos[0]
    assert titulo.startswith("Balacera")
    assert resumen and "balacera" in resumen.lower()   # el resumen extractivo
    assert pais == "México"


def test_una_incoherencia_del_modelo_se_corrige(monkeypatch):
    """El esquema no puede imponer la relación entre los dos campos, y una
    etiqueta contradictoria confundiría al analista."""
    monkeypatch.setattr(classifier, "backend", lambda: "claude")

    class _Falso:
        def __init__(self, datos):
            self.datos = datos

    import json as _json

    class _Resp:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text",
                                  "text": _json.dumps({
                                      "es_seguridad": False,
                                      "categoria": "crimen_alto_impacto",
                                      "motivo": "x"})})()]

    class _Cliente:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    import sys
    sys.modules["anthropic"] = type("m", (), {"Anthropic": lambda: _Cliente()})
    try:
        v = classifier.classify("t", "s", "México")
    finally:
        sys.modules.pop("anthropic", None)

    assert v["categoria"] == "no_securitario"    # se impone la coherencia
