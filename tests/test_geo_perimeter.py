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


# ── titular sin lugar: el gentilicio no basta, el resumen decide ─────────
# Caso real: «La trágica historia de la trilliza colombiana que sobrevivió
# al terremoto» apareció como suceso de Argentina. El titular no nombra
# ningún lugar —solo el gentilicio, que a propósito no cuenta— así que el
# filtro lo dejaba pasar. El resumen sí dice dónde: «terremoto de magnitud
# 7,4 en Colombia», «En Cali».

def test_titular_mudo_con_resumen_extranjero_se_localiza_fuera():
    titulo = ("La trágica historia de la trilliza colombiana que sobrevivió "
              "al terremoto debajo de una mesa")
    resumen = ("Cuatro días después del devastador terremoto de magnitud 7,4 "
               "en Colombia, siguen apareciendo nuevos relatos de dolor. "
               "En Cali, una de las ciudades más afectadas, de una familia de "
               "cinco integrantes solo sobrevivió una hija.")

    assert event_abroad("AR", titulo) is None          # el titular solo, no basta
    assert event_abroad("AR", titulo, resumen) == "Colombia"


def test_el_resumen_no_descarta_un_suceso_local_que_cita_el_extranjero():
    """Un hecho local cuyo texto menciona el extranjero de pasada se queda."""
    titulo = "Detienen a tres tras un asalto en el centro"
    resumen = ("Los detenidos habían ingresado desde Estados Unidos la semana "
               "pasada, según la fiscalía de México. El asalto ocurrió en la "
               "colonia Roma Norte.")

    assert event_abroad("MX", titulo, resumen) is None


def test_el_titular_local_zanja_aunque_el_resumen_hable_de_fuera():
    titulo = "Balacera deja dos heridos en México"
    resumen = "El caso recuerda a los hechos de Colombia y Estados Unidos."

    assert event_abroad("MX", titulo, resumen) is None
