"""La traza de los rechazos: «ne jamais écarter en silence», en código.

Hasta ahora la regla no se cumplía. Casi todos los rechazos ocurren ANTES
de escribir en base: un contador se movía y el artículo desaparecía. El
analista no podía contradecir un filtro que no deja rastro, y nosotros no
podíamos responder por qué El Heraldo enseña 82 artículos pertinentes en
su portada y produce cero.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from atalaya.collect.collector import Collector
from atalaya.db.models import CollectRun, Reject


def _run(db) -> CollectRun:
    run = CollectRun(kind="daily", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    return run


def test_un_rechazo_por_criterio_deja_traza(db):
    col = Collector(db)
    run = _run(db)

    col._reject("sección ajena a la vigilancia: deportes",
                title="Chivas gana el clásico",
                url="https://www.eluniversal.com.mx/deportes/chivas/",
                run=run, country="MX")
    db.flush()

    rej = db.scalar(select(Reject))
    assert rej.reason.startswith("sección ajena")
    assert rej.domain == "eluniversal.com.mx"
    assert rej.country == "MX"
    assert rej.title == "Chivas gana el clásico"


def test_un_rechazo_mecanico_no_deja_traza(db):
    """«Sin fecha» o «fuera de ventana» no se discuten: llenar la tabla con
    eso ahogaría los rechazos que sí importan."""
    col = Collector(db)
    col._reject("fuera de ventana (fecha del flujo)")
    db.flush()

    assert db.scalar(select(Reject)) is None


def test_el_mismo_enlace_no_se_apunta_dos_veces(db):
    """Una colecta diaria vuelve a ver los mismos artículos: sin el UNIQUE,
    la tabla crecería con una fila por rechazo y por día."""
    col = Collector(db)
    run = _run(db)
    for _ in range(3):
        col._reject("sección ajena a la vigilancia: opinión",
                    title="Columna", url="https://x.test/opinion/nota",
                    run=run, country="MX")
    db.flush()

    assert len(list(db.scalars(select(Reject)))) == 1


def test_un_fallo_al_guardar_la_traza_no_tumba_la_colecta(db):
    """Es una anotación al margen, no el trabajo."""
    col = Collector(db)
    col.db = None                     # cualquier uso reventará

    col._reject("señales de granja de contenido: x.test",
                title="Nota", url="https://x.test/nota")

    assert col.stats["rejected"] == 1  # la colecta sigue contando y avanzando


def test_la_traza_aparece_en_la_cobertura(db):
    """El desplegable «descartados» salía vacío en todas las fuentes."""
    from atalaya.web.routes.coverage import coverage_blocks

    run = _run(db)
    Collector(db)._reject("hecho localizado fuera del perímetro: Indonesia",
                          title="Terremoto en Indonesia",
                          url="https://www.eluniversal.com.mx/mundo/sismo/",
                          run=run, country="MX")
    db.commit()

    bloque = next(b for b in coverage_blocks(db, ["MX"]) if b["code"] == "MX")
    fila = next(r for r in bloque["rows"] if r["domain"] == "eluniversal.com.mx")

    assert fila["rejected"] == 1
    assert fila["descartados"][0]["reason"].startswith("hecho localizado")
    assert fila["descartados"][0]["url"].endswith("/mundo/sismo/")


def test_la_traza_se_purga_pasado_el_plazo(db):
    """Sin poda, una tabla que crece en cada colecta acaba pesando más que
    los propios artículos."""
    from atalaya.process.pipeline import purge_rejects

    ahora = datetime.now(timezone.utc)
    db.add(Reject(url="https://x.test/vieja", reason="r",
                  created_at=ahora - timedelta(days=45)))
    db.add(Reject(url="https://x.test/reciente", reason="r", created_at=ahora))
    db.commit()

    assert purge_rejects(db, days=30) == 1
    quedan = [r.url for r in db.scalars(select(Reject))]
    assert quedan == ["https://x.test/reciente"]
