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
        if "dudoso" in title.lower():
            return {"es_seguridad": False, "categoria": "no_securitario",
                    "motivo": "podría ser un hecho de seguridad, no está claro",
                    "confianza": 0.55, "dudoso": True}
        if "llorar" in title.lower() or "virgen" in title.lower():
            return {"es_seguridad": False, "categoria": "no_securitario",
                    "motivo": "espectáculos; no afecta a la seguridad",
                    "confianza": 0.97, "dudoso": False}
        return {"es_seguridad": True, "categoria": "crimen_alto_impacto",
                "motivo": "violencia armada con víctimas",
                "confianza": 0.95, "dudoso": False}

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


# ── repasar el panel con el modelo, sin esperar una colecta ──────────────
def test_reclasificar_repasa_los_eventos_ya_publicados(db, veredictos):
    """El barrido es léxico y dura segundos; la colecta dura media hora. Sin
    esta tercera vía, los eventos anteriores al clasificador no se habrían
    juzgado jamás — sus artículos ya salieron de la ventana de frescura."""
    from atalaya.db.models import EventStatus
    from atalaya.process.pipeline import reclassify_events

    run = _run(db)
    ev = Event(run_id=run.id, dedup_key="k-luckra", country="MX",
               title_es="Luck Ra se largó a llorar en pleno show",
               summary_es="El artista frenó unos minutos para una reflexión.",
               summary_version="v", event_type="ALERTA",
               category="crimen_alto_impacto", level="informativo",
               status=EventStatus.published.value,
               recommendations_es=["Evitar la zona afectada."],
               recurrence=2, independent_sources=2, has_state_media=False)
    db.add(ev)
    db.commit()

    stats = reclassify_events(db)

    db.refresh(ev)
    assert stats["classified"] == 1 and stats["reclassified"] == 1
    assert ev.category == "no_securitario"
    assert ev.event_type == "NOTA"
    assert ev.recommendations_es is None      # no se recomienda sobre una nota
    assert ev.status == EventStatus.published.value   # sigue visible


def test_reclasificar_sin_backend_no_toca_nada(db, monkeypatch):
    from atalaya.process import classifier as c
    from atalaya.process.pipeline import reclassify_events

    monkeypatch.setattr(c, "backend", lambda: "none")
    assert reclassify_events(db)["skipped_backend_none"] == 1


def test_un_choque_en_ruta_es_un_accidente():
    """«murió tras chocar con un camión» salía como crimen de alto impacto."""
    from atalaya.process.cluster import Cluster
    from atalaya.process.scoring import classify_category
    from atalaya.db.models import Article

    a = Article(url="https://x.test/1", domain="x.test", title=
                "Santa Fe: un joven de 29 años murió tras chocar con un camión",
                text="Un joven murió el jueves en un choque en la ruta nacional 19.",
                country="AR", lang="es")
    assert classify_category(Cluster(articles=[a]), "es") == "accidente"


def test_un_hecho_ajeno_no_queda_pendiente_de_corroborar(db, veredictos):
    """«Pendiente de corroboración» sobre un hecho declarado ajeno a la
    seguridad es una contradicción en la misma tarjeta: no hay nada que
    corroborar. Salió así en el panel del 16/08."""
    from atalaya.db.models import EventStatus
    from atalaya.process.pipeline import reclassify_events

    run = _run(db)
    ev = Event(run_id=run.id, dedup_key="k-virgen", country="VE",
               title_es="Diócesis anuncia la agenda de la Virgen de Coromoto",
               summary_es="El obispo indicó que la Iglesia orientará las jornadas.",
               summary_version="v", event_type="ALERTA",
               category="desastre_natural", level="advertencia",
               status=EventStatus.pending_confirm.value,
               recurrence=1, independent_sources=1, has_state_media=False)
    db.add(ev)
    db.commit()

    reclassify_events(db)

    db.refresh(ev)
    assert ev.category == "no_securitario"
    assert ev.status == EventStatus.published.value   # ya no «a confirmar»


def test_una_respuesta_truncada_no_se_intenta_parsear(monkeypatch):
    """Log real: «Unterminated string starting at: line 1 column 2» — el JSON
    cortado a 300 tokens. El mensaje debe decir la causa, no el síntoma."""
    import sys

    from atalaya.process import classifier as c

    monkeypatch.setattr(c, "backend", lambda: "claude")

    class _Resp:
        stop_reason = "max_tokens"
        content = [type("B", (), {"type": "text", "text": '{"es_seg'})()]

    class _Cliente:
        class messages:
            @staticmethod
            def create(**kw):
                assert kw["max_tokens"] >= 1024
                return _Resp()

    sys.modules["anthropic"] = type("m", (), {"Anthropic": lambda: _Cliente()})
    try:
        assert c.classify("t", "s", "México") is None
    finally:
        sys.modules.pop("anthropic", None)


# ── el umbral de confianza: 0,9 ──────────────────────────────────────────
# Decisión del operador. Alto a propósito: un modelo que se equivoca con
# aplomo cuesta más caro que uno que duda en voz alta — y aquí dudar tiene
# una salida prevista, el analista.

def test_un_veredicto_dudoso_se_marca_pero_no_se_aplica(db, veredictos):
    from atalaya.db.models import EventStatus
    from atalaya.process.pipeline import reclassify_events

    run = _run(db)
    ev = Event(run_id=run.id, dedup_key="k-dudoso", country="MX",
               title_es="Caso dudoso: hallan un cuerpo en circunstancias confusas",
               summary_es="Las autoridades no precisaron las causas.",
               summary_version="v", event_type="ALERTA",
               category="crimen_alto_impacto", level="advertencia",
               status=EventStatus.published.value,
               recurrence=2, independent_sources=2, has_state_media=False)
    db.add(ev)
    db.commit()

    stats = reclassify_events(db)

    db.refresh(ev)
    assert stats["dudosos"] == 1
    assert stats["reclassified"] == 0
    assert ev.category == "crimen_alto_impacto"      # la etiqueta NO cambia
    assert ev.event_type == "ALERTA"
    assert ev.score_detail["clasificador"]["dudoso"] is True


def test_el_dudoso_aparece_en_su_seccion_con_la_propuesta(db, veredictos):
    """Trancher n'est pas notre rôle, exposer l'incertitude l'est."""
    from atalaya.db.models import EventStatus
    from atalaya.process.pipeline import reclassify_events
    from atalaya.web.events_view import EventFilters, doubtful_events

    run = _run(db)
    db.add(Event(run_id=run.id, dedup_key="k-dudoso2", country="MX",
                 title_es="Caso dudoso: hallan un cuerpo",
                 summary_es="Sin precisiones.", summary_version="v",
                 event_type="ALERTA", category="crimen_alto_impacto",
                 level="advertencia", status=EventStatus.published.value,
                 recurrence=2, independent_sources=2, has_state_media=False))
    db.commit()
    reclassify_events(db)

    filas = doubtful_events(db, EventFilters(countries=["MX"]))

    assert len(filas) == 1
    assert filas[0]["propuesta"] == "no_securitario"   # lo que proponía
    assert filas[0]["actual"] == "crimen_alto_impacto"  # lo que conserva
    assert filas[0]["confianza"] == 0.55
    assert "no está claro" in filas[0]["motivo"]


def test_sin_campo_de_confianza_se_trata_como_dudoso(monkeypatch):
    """Una respuesta antigua en cache no debe aplicarse a ciegas: sin
    confianza declarada, el lado prudente del error es dudar."""
    import json as _json
    import sys

    from atalaya.process import classifier as c

    monkeypatch.setattr(c, "backend", lambda: "claude")

    class _Resp:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": _json.dumps({
            "es_seguridad": True, "categoria": "crimen_alto_impacto",
            "motivo": "x"})})()]

    class _Cliente:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    sys.modules["anthropic"] = type("m", (), {"Anthropic": lambda: _Cliente()})
    try:
        assert c.classify("t", "s", "México")["dudoso"] is True
    finally:
        sys.modules.pop("anthropic", None)


def test_el_umbral_se_lee_de_la_config():
    from atalaya.process.classifier import threshold

    assert threshold() == 0.9


def _evento_con_veredicto(db, run, veredicto, **kw):
    from atalaya.db.models import EventStatus

    datos = dict(run_id=run.id, dedup_key="k-cache", country="MX",
                 title_es="Balacera deja dos heridos en el centro",
                 summary_es="Sujetos armados dispararon contra un grupo.",
                 summary_version="v", event_type="ALERTA",
                 category="crimen_alto_impacto", level="advertencia",
                 status=EventStatus.published.value, recurrence=2,
                 independent_sources=2, has_state_media=False)
    datos.update(kw)
    ev = Event(**datos, score_detail={"clasificador": {
        **veredicto,
        "huella": classifier.fingerprint(datos["title_es"], datos["summary_es"]),
    }})
    db.add(ev)
    db.commit()
    return ev


def test_un_veredicto_en_cache_sin_confianza_se_vuelve_a_preguntar(db, veredictos):
    """Los veredictos guardados antes de que existiera el umbral no dicen
    nada sobre su propia certeza. La cache los devolvía tal cual y pasaban
    por seguros: el umbral no surtía efecto sobre el stock existente."""
    from atalaya.process.pipeline import reclassify_events

    _evento_con_veredicto(db, _run(db), {
        "es_seguridad": True, "categoria": "crimen_alto_impacto",
        "motivo": "veredicto anterior al umbral"})

    stats = reclassify_events(db)

    assert len(veredictos) == 1                    # se ha vuelto a preguntar
    assert stats["classified"] == 1
    assert stats["classifier_cached"] == 0


def test_el_umbral_se_reevalua_sobre_la_cache(db, veredictos, monkeypatch):
    """El umbral pertenece al operador, no al veredicto: subirlo en
    `schedule.yaml` debe marcar el stock existente sin repagar el modelo."""
    from atalaya.process.pipeline import reclassify_events

    ev = _evento_con_veredicto(db, _run(db), {
        "es_seguridad": True, "categoria": "crimen_alto_impacto",
        "motivo": "violencia armada con víctimas",
        "confianza": 0.95, "dudoso": False})
    monkeypatch.setattr(classifier, "threshold", lambda: 0.99)

    stats = reclassify_events(db)

    db.refresh(ev)
    assert veredictos == []                        # sin llamada al modelo
    assert stats["classifier_cached"] == 1
    assert stats["dudosos"] == 1
    assert ev.score_detail["clasificador"]["dudoso"] is True


def test_un_veredicto_sin_confianza_declarada_se_expone_igual(db):
    """Su etiqueta se aplicó sin que nadie sepa con qué certeza. Callarlo
    sería hacerlo pasar por seguro."""
    from atalaya.web.events_view import EventFilters, doubtful_events

    _evento_con_veredicto(db, _run(db), {
        "es_seguridad": True, "categoria": "crimen_alto_impacto",
        "motivo": "veredicto anterior al umbral"})

    filas = doubtful_events(db, EventFilters(countries=["MX"]))

    assert len(filas) == 1
    assert filas[0]["confianza"] is None
    assert filas[0]["propuesta"] == "crimen_alto_impacto"
