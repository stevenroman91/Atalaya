"""Localización del hecho: la prensa local cubre sucesos del extranjero.

Casos reales reportados por una usuaria en la primera prueba en producción:
el sismo de Colombia y otro de Indonesia aparecían como eventos de países
vigilados solo porque los publicaba su prensa nacional.
"""
from atalaya.collect.whitelist import event_abroad


def test_sismo_en_colombia_no_es_evento_de_nicaragua():
    title = ("La tragedia de Ana María Saavedra, la joven trilliza que perdió "
             "a sus dos hermanas y a sus padres en el terremoto en Colombia")
    assert event_abroad("NI", title) == "Colombia"


def test_sismo_en_indonesia_no_es_evento_de_mexico():
    assert event_abroad("MX", "Fuerte sismo sacude Indonesia: al menos 20 muertos") \
        == "Indonesia"


def test_compatriota_en_el_extranjero_no_es_evento_local():
    # el hecho ocurre en Texas; el gentilicio no lo trae de vuelta al país
    title = ("Nicaragüense muere en accidente en EE. UU., familiares piden "
             "ayuda para repatriarlo")
    assert event_abroad("NI", title) is not None


def test_suceso_nacional_se_conserva():
    assert event_abroad("NI", "Policía incauta medio millón de dólares en dos "
                              "operativos") is None
    assert event_abroad("MX", "Balacera deja tres muertos en Zacatecas") is None


def test_mencion_del_pais_ancla_el_hecho_aunque_cite_el_extranjero():
    # el suceso ocurre en el país; EE. UU. aparece como actor, no como lugar
    title = "Senadores de EE. UU. proponen sancionar el oro de Nicaragua"
    assert event_abroad("NI", title) is None


def test_zona_vigilada_ancla_el_hecho():
    title = "Detienen a presunto extorsionador en Roma Norte CDMX"
    assert event_abroad("MX", title) is None


def test_paises_del_perimetro_son_extranjeros_entre_si():
    assert event_abroad("MX", "Incendio en un mercado de Guatemala") == "Guatemala"
    assert event_abroad("GT", "Motín en una cárcel de Honduras") == "Honduras"


def test_subcadenas_no_disparan_falsos_positivos():
    # "Cuba" dentro de otra palabra no localiza el hecho en Cuba
    assert event_abroad("NI", "Incubadoras donadas al hospital de Managua") is None
