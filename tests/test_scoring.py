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

    assert classify_type("operacion_seguridad", {"extreme": []}) == "NOTA"


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
