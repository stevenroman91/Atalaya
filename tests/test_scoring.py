"""Tests del dédoublonnage y del scoring (§9)."""
from datetime import datetime, timedelta, timezone

from atalaya.db.models import Article
from atalaya.process.cluster import Cluster, cluster_articles
from atalaya.process.scoring import (
    classify_category, classify_level, classify_type, independent_source_count,
    score_cluster,
)


def _art(id, title, domain, source_type="independiente", text="", hours_ago=2, country="GT"):
    return Article(
        id=id, url=f"https://{domain}/{id}", domain=domain, source_name=domain,
        source_type=source_type, title=title, text=text, country=country,
        published_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        fetched_at=datetime.now(timezone.utc),
    )


def test_cluster_groups_same_event():
    arts = [
        _art(1, "Balacera en la Zona Viva deja 3 heridos en Ciudad de Guatemala", "prensalibre.com"),
        _art(2, "Balacera en Zona Viva de la capital deja cinco heridos", "soy502.com"),
        _art(3, "Congreso de seguridad vial se celebra en Ciudad de Guatemala", "soy502.com"),
    ]
    clusters = cluster_articles(arts)
    assert len(clusters) == 2
    big = max(clusters, key=lambda c: len(c.articles))
    assert {a.id for a in big.articles} == {1, 2}


def test_recurrence_needs_two_independent_sources():
    # 2 fuentes estatales no cuentan como independientes entre sí (§5.4)
    a = _art(1, "Tiroteo deja heridos", "prensa-latina.cu", "estatal", "balacera con heridos")
    b = _art(2, "Tiroteo deja heridos en la capital", "telesurtv.net", "estatal", "balacera con heridos")
    total, independent, has_state = independent_source_count([a, b])
    assert total == 2 and independent == 1 and has_state

    result = score_cluster(Cluster(articles=[a, b]), "es")
    assert not result.publishable

    # estatal + independiente = 2 independientes → publicable
    c = _art(3, "Tiroteo con heridos en la capital", "prensalibre.com", "independiente",
             "balacera dejó varios heridos")
    result2 = score_cluster(Cluster(articles=[a, c]), "es")
    assert result2.publishable


def test_off_whitelist_only_corroborates():
    # dos dominios fuera de lista blanca no fundan una alerta (§4)
    a = _art(1, "Balacera deja heridos", "blog-noticias-x.com", "off_whitelist", "balacera heridos")
    b = _art(2, "Balacera deja heridos hoy", "portal-y.com", "off_whitelist", "balacera heridos")
    _, independent, _ = independent_source_count([a, b])
    assert independent == 0
    # con una fuente de lista blanca sí corrobora
    c = _art(3, "Balacera en la zona deja heridos", "soy502.com", "independiente", "balacera heridos")
    _, independent2, _ = independent_source_count([a, c])
    assert independent2 == 2


def test_severity_required():
    a = _art(1, "Feria del libro abre en la capital", "prensalibre.com", text="gran afluencia cultural")
    b = _art(2, "Feria del libro abre sus puertas en la capital", "soy502.com", text="gran afluencia cultural")
    result = score_cluster(Cluster(articles=[a, b]), "es")
    assert not result.publishable and not result.pending_confirm


def test_single_source_extreme_goes_to_pending():
    a = _art(1, "Atentado con explosivos en sede municipal", "prensalibre.com",
             text="un atentado con artefacto explosivo dañó la fachada")
    result = score_cluster(Cluster(articles=[a]), "es")
    assert not result.publishable
    assert result.pending_confirm


def test_classification():
    a = _art(1, "Balacera deja 3 muertos", "prensalibre.com", text="balacera con muertos")
    cl = Cluster(articles=[a])
    assert classify_category(cl, "es") == "crimen_alto_impacto"
    assert classify_level(cl, "es") == "informativo"
    assert classify_type("crimen_alto_impacto", {"extreme": []}) == "ALERTA"

    b = _art(2, "Ola de robos en la zona 10: van al menos 12 asaltos", "soy502.com",
             text="serie de asaltos con el mismo modus operandi")
    cl2 = Cluster(articles=[b])
    assert classify_level(cl2, "es") == "advertencia"
    assert classify_category(cl2, "es") == "crimen_bajo_impacto"

    # Cambiado con motivo: «Decretan toque de queda en Antón y Río Hato por
    # homicidios» salía en nota informativa. Un operativo o un toque de queda
    # es justo lo que cambia la conducta de una delegación sobre el terreno,
    # y tenemos plantillas de recomendación para esta categoría.
    assert classify_type("operacion_seguridad", {"extreme": []}) == "ALERTA"


def test_divergence_sentence():
    from atalaya.process.summarize import casualty_figures, divergence_sentence
    a = _art(1, "Balacera deja 3 heridos", "prensalibre.com",
             text="La balacera dejó 3 heridos según bomberos.")
    a.source_name = "Prensa Libre"
    b = _art(2, "Balacera deja 5 heridos", "soy502.com",
             text="El ataque dejó 5 heridos según socorristas.")
    b.source_name = "Soy502"
    sentence = divergence_sentence(casualty_figures([a, b]))
    assert sentence and "Prensa Libre" in sentence and "Soy502" in sentence
    assert "difieren" in sentence


# ── el panel de Brasil: lo que no se sabe no se inventa ──────────────────
# «Líder do PT recebe alta de UTI após sofrer AVC» y un dron derribado sobre
# Rumanía salieron etiquetados «crimen de bajo impacto», en tarjeta ALERTA:
# era el valor por defecto cuando ninguna señal de categoría acertaba.

def _brart(title, text="", **kw):
    from datetime import datetime, timezone
    from atalaya.db.models import Article
    d = dict(url=f"https://x.test/{abs(hash(title))}", domain="x.test",
             source_name="X", source_type="independiente", title=title,
             text=text, country="BR", lang="pt",
             published_at=datetime.now(timezone.utc))
    d.update(kw)
    return Article(**d)


def test_sin_senal_de_categoria_no_se_inventa_un_delito():
    from atalaya.process.cluster import Cluster
    from atalaya.process.scoring import classify_category, classify_type

    cl = Cluster(articles=[_brart(
        "Líder do PT na Câmara recebe alta de UTI após sofrer AVC",
        "O deputado sofreu um acidente vascular cerebral durante uma "
        "reunião do Colégio de Líderes e seguirá internado no hospital.")])

    cat = classify_category(cl, "pt")
    assert cat == "sin_clasificar"
    assert classify_type(cat, {}) == "NOTA"      # nunca ALERTA


def test_un_accidente_de_carretera_no_es_un_crimen():
    from atalaya.process.cluster import Cluster
    from atalaya.process.scoring import classify_category, classify_type

    cl = Cluster(articles=[_brart(
        "Acidente com ônibus deixa ao menos 12 mortos",
        "O ônibus saiu da pista, caiu em uma vala e tombou na madrugada. "
        "A polícia informou que o motorista do ônibus foi detido.")])

    cat = classify_category(cl, "pt")
    assert cat == "accidente"
    assert classify_type(cat, {}) == "ALERTA"    # sigue interesando al analista


def test_un_tiroteo_sigue_siendo_crimen_de_alto_impacto():
    from atalaya.process.cluster import Cluster
    from atalaya.process.scoring import classify_category

    cl = Cluster(articles=[_brart(
        "Tiroteio deixa dois mortos no centro do Rio",
        "Homens armados dispararam contra um grupo de pessoas.")])

    assert classify_category(cl, "pt") == "crimen_alto_impacto"


# ── clustering: dos cifras sueltas no hacen un mismo hecho ───────────────
def test_una_cifra_compartida_no_agrupa_dos_hechos_distintos():
    from atalaya.process.cluster import cluster_articles

    a = _brart("Romário deixa PL após 5 anos e declara apoio a Paes no Rio | CNN Brasil")
    b = _brart("Terremoto de magnitude 5 atinge sul da Espanha e danifica casas | CNN Brasil")
    a.id, b.id = 1, 2

    clusters = cluster_articles([a, b])

    assert len(clusters) == 2


def test_dos_medios_sobre_el_mismo_hecho_siguen_agrupandose():
    from atalaya.process.cluster import cluster_articles

    a = _brart("Balacera deja dos heridos en el mercado oriental de Managua")
    b = _brart("Dos heridos tras balacera en el mercado oriental de Managua",
             domain="y.test", source_name="Y")
    a.id, b.id = 3, 4

    assert len(cluster_articles([a, b])) == 1


# ── el panel del 16/08: 26 tarjetas «a confirmar», presque toutes du bruit ─
# Toutes par le même mécanisme : severity_signals lisait 1500 caractères de
# corps. Au Venezuela, toute chronique nomme le séisme du 24 juin quelque
# part — donc tout devenait « gravité extrême », donc tout se publiait avec
# une seule source. Une info n'est pas ce que son quinzième paragraphe
# mentionne.

def _cl(titulo, texto=""):
    from atalaya.process.cluster import Cluster
    return Cluster(articles=[_brart(titulo, texto)])


def test_una_mencion_de_paso_no_es_gravedad_extrema():
    from atalaya.process.scoring import severity_signals

    cl = _cl("Miltico llega a La Gran Sabana: la cicloaventura que conmovió",
             "Después de más de 40 días de viaje y unos 1.500 kilómetros en "
             "bicicleta, el joven llegó a la Gran Sabana. " + "x" * 600 +
             " Por la emergencia del doble terremoto que golpeó a Venezuela, "
             "Miltico detuvo su ruta.")

    assert severity_signals(cl, "es")["extreme"] == []


def test_un_terremoto_en_el_titular_sigue_siendo_extremo():
    from atalaya.process.scoring import severity_signals

    cl = _cl("Terremoto de magnitud 6.8 sacude la costa de Guerrero",
             "El sismo se sintió en varios estados.")

    assert severity_signals(cl, "es")["extreme"] == ["terremoto"]


def test_una_cronica_economica_no_se_vuelve_desastre_natural():
    from atalaya.process.scoring import classify_category

    cl = _cl("Reconstruir el salario mínimo es otra tarea pendiente",
             "El salario mínimo lleva cuatro años anclado en 130 bolívares, "
             "hoy apenas equivale a 0,17 centavos de dólar por mes.")

    assert classify_category(cl, "es") == "sin_clasificar"


def test_un_ataque_a_tiros_es_crimen_de_alto_impacto():
    """Salió como «manifestación» en el panel real."""
    from atalaya.process.scoring import classify_category

    cl = _cl("Ataque a tiros en Alto de Los Lagos deja un hombre herido",
             "El hecho de violencia se dio en los predios de la torre "
             "habitacional H-112, pasadas las 2:30 a. m.")

    assert classify_category(cl, "es") == "crimen_alto_impacto"
