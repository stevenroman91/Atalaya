"""Pertinencia de lo que llega al panel.

Tres defectos reportados por una usuaria en la primera prueba real:
recomendaciones sin relación con el hecho, una columna de opinión tratada
como suceso, y una noticia de Venezuela contada como evento de Nicaragua
por venir de un medio nicaragüense.
"""
from atalaya.collect.whitelist import off_topic_section, perimeter_country_for
from atalaya.process.summarize import build_recommendations, detect_incident


# ── secciones ajenas a la vigilancia ─────────────────────────────────────
def test_opinion_y_deportes_se_descartan():
    assert off_topic_section(
        "https://www.laprensani.com/2026/08/15/opinion/3762922-centenario") == "opinion"
    assert off_topic_section(
        "https://www.laprensani.com/2026/08/15/deportes/3763554-ferran-torres") == "deportes"
    assert off_topic_section(
        "https://www.laprensani.com/2026/08/15/editorial/3763131-obispo") == "editorial"


def test_sucesos_y_politica_se_conservan():
    assert off_topic_section(
        "https://www.laprensani.com/2026/08/11/sucesos/3759104-policia-incauta") is None
    assert off_topic_section(
        "https://www.laprensani.com/2026/08/15/politica/3763420-sanciones-oro") is None
    assert off_topic_section(
        "https://www.laprensani.com/2026/08/15/nacionales/3763503-accidente") is None


def test_la_seccion_debe_ser_un_segmento_entero():
    # «vida» como sección sí; «vidaurri» dentro de un titular no
    assert off_topic_section("https://x.com/vida/nota") == "vida"
    assert off_topic_section("https://x.com/nacionales/vidaurri-detenido") is None


# ── reatribución al país correcto ────────────────────────────────────────
def test_pais_del_perimetro_se_reatribuye():
    # el hecho ocurre en Venezuela: es del perímetro, no debe descartarse
    assert perimeter_country_for("Venezuela") == "VE"
    assert perimeter_country_for("Guatemala") == "GT"


def test_pais_fuera_del_perimetro_no_se_reatribuye():
    assert perimeter_country_for("Colombia") is None
    assert perimeter_country_for("Indonesia") is None


# ── recomendaciones ligadas al hecho ─────────────────────────────────────
def test_recomendacion_de_secuestro_dice_como_actuar():
    recs = build_recommendations(
        "crimen_alto_impacto", "la zona de Zacatecas",
        "Secuestran a un empresario en la carretera federal")

    assert any("no oponer resistencia" in r for r in recs)
    assert not any(r.startswith("Evitar la zona") for r in recs)


def test_recomendacion_de_tiroteo_es_conducta_inmediata():
    recs = build_recommendations(
        "crimen_alto_impacto", "la zona afectada",
        "Balacera deja tres muertos en el centro")

    assert any("protegerse" in r for r in recs)


def test_sin_incidente_reconocido_se_usa_la_categoria():
    recs = build_recommendations(
        "manifestacion", "la zona de Managua",
        "Concentración frente a la catedral")

    assert recs
    assert all("la zona de Managua" in r or "aglomeración" in r for r in recs)


def test_deteccion_de_incidentes():
    assert detect_incident("Cobro de piso a comerciantes") == "extorsion"
    assert detect_incident("Fuerte sismo sacude la costa") == "sismo"
    assert detect_incident("Bloquean la carretera al aeropuerto") == "bloqueo"
    assert detect_incident("Reunión del consejo municipal") is None
