"""Orquestación del tratamiento diario: cluster → score → clasifica → redacta
→ persiste eventos. Idempotente: la clave de dédup del cluster es estable, un
re-run actualiza el evento existente en lugar de duplicarlo (§8).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from atalaya.collect.whitelist import (
    event_abroad, foreign_section, off_topic_section, perimeter_anchor,
    perimeter_country_for, strip_site_suffix,
)
from atalaya.config import load_countries, load_schedule, zone_by_id
from atalaya.db.models import (
    Article, ArticleStatus, CollectRun, Event, EventArticle, EventStatus, Reject,
)
from atalaya.process import classifier
from atalaya.process.cluster import Cluster, cluster_articles
from atalaya.process.scoring import (
    classify_category, classify_level, classify_type, independent_source_count,
    score_cluster, severity_signals,
)
from atalaya.process.summarize import build_recommendations, build_summary, summary_version

log = logging.getLogger(__name__)


def _cluster_zone(cluster: Cluster) -> str | None:
    zones = [a.zone_id for a in cluster.articles if a.zone_id]
    return max(set(zones), key=zones.count) if zones else None


def _cluster_occurred_at(cluster: Cluster) -> datetime | None:
    dates = [a.published_at for a in cluster.articles if a.published_at]
    return min(dates) if dates else None


def _screen_stored(db: Session, code: str, articles: list[Article], stats: dict) -> list[Article]:
    """Aplica los filtros de pertinencia a los artículos YA guardados.

    La selección va por ventana de frescura, no por run: cada noche se
    vuelven a leer artículos ingeridos por versiones anteriores del
    recolector, anteriores a estos filtros. Sin este paso, un artículo
    pernicioso guardado ayer sigue generando su evento hasta salir de la
    ventana — filtrar solo en la ingesta no basta.

    No se borra nada: el artículo queda en base con estado «rechazado» y el
    motivo, recuperable por el analista.
    """
    kept: list[Article] = []
    for a in articles:
        section = off_topic_section(a.url or "")
        if section:
            a.status = ArticleStatus.rejected.value
            a.reject_reason = f"sección ajena a la vigilancia: {section}"
            stats["screened"] += 1
            log.info("descartado en tratamiento [%s] %s", a.reject_reason, a.title[:100])
            continue

        abroad = event_abroad(code, a.title)
        if abroad:
            other = perimeter_country_for(abroad)
            if other and other != code:
                # el hecho está en otro país vigilado: se reatribuye, no se
                # pierde. Lo tratará el bucle de ese país (este run o el
                # siguiente, según el orden de configuración).
                a.country = other
                a.zone_id = None
                stats["reattributed"] += 1
            else:
                a.status = ArticleStatus.rejected.value
                a.reject_reason = f"hecho localizado fuera del perímetro: {abroad}"
                stats["screened"] += 1
                log.info("descartado en tratamiento [%s] %s", a.reject_reason, a.title[:100])
            continue

        # El propio medio lo archivó en «internacional»/«mundo»: el hecho
        # ocurre fuera de sus fronteras salvo que el titular nombre un lugar
        # del perímetro (un suceso en Caracas contado por O Globo sigue
        # siendo nuestro; la muerte de un profesor en Cambridge no).
        foreign = foreign_section(a.url or "")
        if foreign and not perimeter_anchor(strip_site_suffix(a.title or "")):
            a.status = ArticleStatus.rejected.value
            a.reject_reason = f"sección internacional del medio: {foreign}"
            stats["screened"] += 1
            log.info("descartado en tratamiento [%s] %s", a.reject_reason, a.title[:100])
            continue

        kept.append(a)
    return kept


def screen_event(ev: Event) -> tuple[str | None, str | None]:
    """Juzga un evento ya creado: (país al que reatribuir, motivo de retirada).

    Aplica las reglas al EVENTO, no a sus artículos. Es la diferencia que
    hacía inútil la primera versión de este barrido: solo miraba artículos
    marcados como rechazados, y un artículo únicamente se re-examina si
    sigue dentro de la ventana de frescura. Los eventos del día anterior
    quedaban congelados — demasiado viejos para volver a tratarse, pero
    aún visibles en el panel.
    """
    abroad = event_abroad(ev.country, ev.title_es or "", ev.summary_es or "")
    if abroad:
        other = perimeter_country_for(abroad)
        if other and other != ev.country:
            return other, None
        return None, f"hecho localizado fuera del perímetro: {abroad}"

    # La sección se lee en las URL de respaldo. Basta con que la mayoría sean
    # ajenas: el titular del evento sale del artículo más completo, y un
    # cluster mayoritariamente de opinión es una columna, no un suceso.
    urls = [ea.article.url or "" for ea in ev.articles if ea.article]
    off = [off_topic_section(u) for u in urls]
    ajenos = [s for s in off if s]
    if urls and len(ajenos) * 2 > len(urls):
        return None, f"sección ajena a la vigilancia: {ajenos[0]}"

    # Sección internacional del medio, mismo criterio de mayoría. El ancla
    # se busca en titular y resumen: si ninguno nombra un lugar vigilado,
    # el hecho ocurre fuera aunque la lista de topónimos no lo conozca.
    fuera = [s for s in (foreign_section(u) for u in urls) if s]
    if urls and len(fuera) * 2 > len(urls):
        texto = f"{strip_site_suffix(ev.title_es or '')}\n{ev.summary_es or ''}"
        if not perimeter_anchor(texto):
            return None, f"sección internacional del medio: {fuera[0]}"
    return None, None


def _country_geo(code: str) -> tuple[float, float] | None:
    """Marcador a nivel país: primera zona con coordenadas conocidas."""
    country = load_countries().get(code)
    if country is None:
        return None
    return next((z.geo for z in country.zones if z.geo), None)


def _ask_classifier(db: Session, *, key: str, title: str, summary: str | None,
                    country: str, stats: dict) -> dict | None:
    """Veredicto del modelo para este cluster, o None si no aplica.

    Cachea por huella del texto juzgado: un re-run sobre el mismo titular y
    el mismo resumen no vuelve a pagar. El techo por colecta protege la
    factura si un día llegan mil clusters en vez de treinta.
    """
    if classifier.backend() == "none":
        return None
    techo = int(load_schedule().get("classifier", {}).get("max_events", 120))
    if stats.get("classified", 0) >= techo:
        stats["classifier_capped"] = stats.get("classifier_capped", 0) + 1
        return None

    huella = classifier.fingerprint(title, summary)
    previo = db.scalar(select(Event).where(Event.dedup_key == key))
    if previo and (previo.score_detail or {}).get("clasificador", {}).get("huella") == huella:
        stats["classifier_cached"] = stats.get("classifier_cached", 0) + 1
        return previo.score_detail["clasificador"]

    veredicto = classifier.classify(title, summary, country)
    if veredicto is None:
        stats["classifier_failed"] = stats.get("classifier_failed", 0) + 1
        return None
    veredicto["huella"] = huella
    stats["classified"] = stats.get("classified", 0) + 1
    if not veredicto.get("es_seguridad"):
        stats["no_securitario"] = stats.get("no_securitario", 0) + 1
    return veredicto


def reclassify_events(db: Session, limit: int = 200) -> dict:
    """Pasa el clasificador por los eventos ya publicados.

    El barrido es léxico y dura segundos; la colecta dura media hora. Sin
    esta tercera vía, ver lo que aporta el modelo exigía esperar una colecta
    entera — y los eventos anteriores a su llegada no se habrían juzgado
    jamás, porque un evento solo se reprocesa mientras sus artículos siguen
    en la ventana de frescura. Es el mismo agujero que ya nos costó tres
    correcciones.
    """
    stats = {"classified": 0, "no_securitario": 0, "reclassified": 0,
             "classifier_failed": 0, "classifier_cached": 0, "dudosos": 0}
    if classifier.backend() == "none":
        stats["skipped_backend_none"] = 1
        return stats

    live = (EventStatus.published.value, EventStatus.pending_confirm.value)
    for ev in db.scalars(select(Event).where(Event.status.in_(live))
                         .order_by(Event.created_at.desc()).limit(limit)):
        country = load_countries().get(ev.country)
        veredicto = _ask_classifier(
            db, key=ev.dedup_key, title=ev.title_es or "", summary=ev.summary_es,
            country=country.name if country else ev.country, stats=stats)
        if not veredicto:
            continue
        ev.score_detail = {**(ev.score_detail or {}), "clasificador": veredicto}
        if veredicto.get("dudoso"):
            stats["dudosos"] = stats.get("dudosos", 0) + 1
            db.commit()
            continue                  # marcado, no aplicado: decide el analista
        # «Pendiente de corroboración» sobre un hecho que el modelo declara
        # ajeno a la seguridad es una contradicción en la misma tarjeta: no
        # hay nada que corroborar. Pasa a nota publicada — sigue visible.
        if (not veredicto.get("es_seguridad")
                and ev.status == EventStatus.pending_confirm.value):
            ev.status = EventStatus.published.value
        categoria = veredicto["categoria"]
        tipo = classify_type(categoria, (ev.score_detail or {}).get("severity", {}))
        if (categoria, tipo) != (ev.category, ev.event_type):
            ev.category, ev.event_type = categoria, tipo
            if tipo != "ALERTA":
                ev.recommendations_es = None
            stats["reclassified"] += 1
        db.commit()
    return stats


def purge_rejects(db: Session, days: int | None = None) -> int:
    """Borra las trazas de rechazo antiguas. Devuelve cuántas.

    La traza sirve al analista para discutir los filtros de estos días, no
    para siempre: sin poda, una tabla que crece con cada colecta acaba
    pesando más que los propios artículos en una base modesta.
    """
    if days is None:
        days = int(load_schedule().get("collector", {})
                   .get("reject_retention_days", 30))
    corte = datetime.now(timezone.utc) - timedelta(days=days)
    n = db.query(Reject).filter(Reject.created_at < corte).delete(
        synchronize_session=False)
    db.commit()
    if n:
        log.info("purgadas %d trazas de rechazo anteriores a %s", n, corte.date())
    return n


def sweep_events(db: Session) -> dict:
    """Repasa el panel sin recolectar nada. Devuelve el recuento.

    Es una operación de mantenimiento sobre lo que ya está en base: ni una
    petición de red, unos segundos. Existe suelta porque estaba enterrada
    en el tratamiento diario, y eso obligaba a esperar una colecta entera
    —media hora— para aplicar una corrección de filtro.
    """
    stats = {"retired": 0, "reattributed": 0, "geocoded": 0, "retitled": 0,
             "reclassified": 0}
    _retire_screened_events(db, stats)
    db.commit()
    return stats


def _rescore_event(ev: Event, stats: dict) -> None:
    """Vuelve a juzgar gravedad y categoría de un evento ya publicado.

    Tercera vez que tropezamos con lo mismo: corregir una regla no toca lo
    que ya está en base. El barrido sabía repasar el perímetro y las
    coordenadas, pero no la gravedad — así que los 26 eventos «a confirmar»
    creados cuando la gravedad se leía en el cuerpo del artículo seguirían
    en el panel indefinidamente, esperando una ventana de frescura que ya
    pasó.

    Solo léxico: ni una petición de red, para que el barrido siga tardando
    segundos. El clasificador trabaja en la colecta.
    """
    articles = [ea.article for ea in ev.articles if ea.article]
    if not articles:
        return
    country = load_countries().get(ev.country)
    lang = country.lang if country else "es"
    cluster = Cluster(articles=articles)
    sev = severity_signals(cluster, lang)

    # Un «a confirmar» se sostiene ÚNICAMENTE sobre la gravedad extrema: si
    # ya no la hay, no queda nada que confirmar.
    if ev.status == EventStatus.pending_confirm.value and not sev["extreme"]:
        ev.status = EventStatus.discarded.value
        ev.score_detail = {**(ev.score_detail or {}),
                           "retirado": "sin gravedad extrema en el titular"}
        stats["retired"] += 1
        log.info("evento retirado [gravedad revisada] %s", (ev.title_es or "")[:100])
        return

    categoria = classify_category(cluster, lang)
    tipo = classify_type(categoria, sev)
    if (categoria, tipo) != (ev.category, ev.event_type):
        ev.category, ev.event_type = categoria, tipo
        if tipo != "ALERTA":
            ev.recommendations_es = None   # no se recomienda sobre una nota
        stats["reclassified"] = stats.get("reclassified", 0) + 1


def _retire_screened_events(db: Session, stats: dict) -> None:
    """Repasa los eventos publicados, sin límite de antigüedad.

    Retira del panel —estado «descartado», nunca borrados—, reatribuye al
    país donde ocurre el hecho, y rellena las coordenadas que falten.

    Lo último importa para el mapa: hasta ahora las coordenadas solo se
    rellenaban al actualizar un evento, y un evento se actualiza únicamente
    si su cluster se vuelve a tratar — o sea, si sus artículos siguen en la
    ventana de frescura. Los eventos de días anteriores quedaban sin
    coordenadas para siempre, y el mapa vacío.
    """
    zones = zone_by_id()
    live = (EventStatus.published.value, EventStatus.pending_confirm.value)
    for ev in db.scalars(select(Event).where(Event.status.in_(live))):
        other, reason = screen_event(ev)
        if other:
            ev.country = other
            ev.zone_id = None           # la zona anterior era de otro país
            ev.lat = ev.lon = None      # se recalculan abajo, sobre el país real
            stats["reattributed"] += 1
            log.info("evento reatribuido a %s: %s", other, (ev.title_es or "")[:100])
        elif reason:
            ev.status = EventStatus.discarded.value
            ev.score_detail = {**(ev.score_detail or {}), "retirado": reason}
            stats["retired"] += 1
            log.info("evento retirado del panel [%s] %s", reason, (ev.title_es or "")[:100])
            continue

        _rescore_event(ev, stats)

        limpio = strip_site_suffix(ev.title_es or "")
        if limpio and limpio != (ev.title_es or ""):
            ev.title_es = limpio        # la firma del medio no es del titular
            stats["retitled"] += 1

        if ev.lat is None or ev.lon is None:
            zone = zones.get(ev.zone_id) if ev.zone_id else None
            geo = (zone.geo if zone and zone.geo else None) or _country_geo(ev.country)
            if geo:
                ev.lat, ev.lon = geo
                stats["geocoded"] += 1


def process_daily(db: Session, run: CollectRun, countries_filter: list[str] | None = None) -> dict:
    stats = {"clusters": 0, "published": 0, "pending_confirm": 0, "discarded": 0,
             "updated": 0, "screened": 0, "reattributed": 0, "retired": 0,
             "geocoded": 0, "retitled": 0, "classified": 0}
    countries = load_countries()
    zones = zone_by_id()

    for code, country in countries.items():
        if not country.daily or (countries_filter and code not in countries_filter):
            continue
        # Selección por ventana de frescura, NO por run_id: los artículos de
        # un run interrumpido (o deduplicados en el run actual) siguen siendo
        # procesables — el upsert por dedup_key garantiza la idempotencia.
        sched = load_schedule()["daily"]
        window = float(sched.get("window_hours", 24)) + float(sched.get("overlap_hours", 2))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window)
        articles = list(db.scalars(
            select(Article).where(
                Article.country == code,
                Article.published_at >= cutoff,
                Article.status.in_([ArticleStatus.extracted.value, ArticleStatus.title_only.value]),
                Article.theme.is_(None),   # los artículos temáticos son del flujo semanal
            )
        ))
        articles = _screen_stored(db, code, articles, stats)
        if not articles:
            continue

        for cluster in cluster_articles(articles):
            stats["clusters"] += 1
            result = score_cluster(cluster, country.lang)
            total, independent, has_state = independent_source_count(cluster.articles)

            if result.publishable:
                status = EventStatus.published.value
            elif result.pending_confirm:
                status = EventStatus.pending_confirm.value
            else:
                stats["discarded"] += 1
                continue    # no persistimos el ruido; queda trazado en logs/stats

            category = classify_category(cluster, country.lang)
            level = classify_level(cluster, country.lang)
            etype = classify_type(category, result.reasons["severity"])

            zone_id = _cluster_zone(cluster)
            zone = zones.get(zone_id) if zone_id else None
            # {lugar} de las recomendaciones: sintagma completo. Sin zona
            # conocida, genérico — nunca «la zona de México» (país entero).
            place = f"la zona de {zone.name}" if zone else "la zona afectada"
            # Prioridad a las coordenadas que trae la fuente: USGS y GDACS
            # dan el punto real del hecho. La zona es un centroide y el país
            # es su capital — un sismo a 300 km salía en la capital.
            geo = next(((a.lat, a.lon) for a in cluster.articles
                        if a.lat is not None and a.lon is not None), None)
            if geo is None and zone and zone.geo:
                geo = zone.geo
            if geo is None:
                # sin zona precisa: marcador a nivel país (primera zona con
                # geo — p. ej. mx-nacional / la capital) para que el evento
                # aparezca igualmente en el mapa
                geo = next((z.geo for z in country.zones if z.geo), None)

            rep = cluster.representative
            summary = build_summary(cluster)

            # El clasificador juzga el texto que verá el analista —titular y
            # resumen ya construidos—, no el material bruto. Va aquí y no
            # antes por eso, y porque solo se paga por lo que va a publicarse.
            veredicto = _ask_classifier(db, key=cluster.dedup_key(),
                                        title=rep.title, summary=summary,
                                        country=country.name, stats=stats)
            if veredicto:
                result.reasons["clasificador"] = veredicto
                # Dudoso: se guarda para que el analista lo vea y decida,
                # pero no se aplica. Marcar y dejar pasar, nunca tirar.
                if not veredicto.get("dudoso"):
                    category = veredicto["categoria"]
                    etype = classify_type(category, result.reasons["severity"])
                    if (not veredicto.get("es_seguridad")
                            and status == EventStatus.pending_confirm.value):
                        # nada que corroborar en un hecho ajeno a la seguridad
                        status = EventStatus.published.value

            recommendations = (
                build_recommendations(category, place,
                                      f"{rep.title}\n{summary or ''}")
                if etype == "ALERTA" and status == EventStatus.published.value else None
            )
            version = summary_version(rep.title, summary, recommendations)
            key = cluster.dedup_key()

            existing = db.scalar(select(Event).where(Event.dedup_key == key))
            if existing:
                # re-run o cluster que ganó fuentes: actualizar, no duplicar
                existing.recurrence = total
                existing.independent_sources = independent
                existing.has_state_media = has_state
                existing.status = status
                existing.event_type = etype
                existing.category = category
                existing.level = level
                existing.summary_es = summary
                existing.recommendations_es = recommendations
                existing.summary_version = version
                existing.score_detail = result.reasons
                if existing.lat is None and geo:   # eventos antiguos sin coordenadas
                    existing.lat, existing.lon = geo
                    existing.zone_id = existing.zone_id or zone_id
                event = existing
                stats["updated"] += 1
            else:
                event = Event(
                    run_id=run.id, dedup_key=key, country=code, zone_id=zone_id,
                    lat=geo[0] if geo else None, lon=geo[1] if geo else None,
                    title_es=rep.title, summary_es=summary,
                    recommendations_es=recommendations, summary_version=version,
                    event_type=etype, category=category, level=level, status=status,
                    occurred_at=_cluster_occurred_at(cluster),
                    recurrence=total, independent_sources=independent,
                    has_state_media=has_state, score_detail=result.reasons,
                )
                db.add(event)
                db.flush()
                if status == EventStatus.published.value:
                    stats["published"] += 1
                else:
                    stats["pending_confirm"] += 1

            linked = {ea.article_id for ea in event.articles}
            for a in cluster.articles:
                if a.id not in linked:
                    db.add(EventArticle(event_id=event.id, article_id=a.id))

    db.flush()          # los rechazos deben ser visibles para el barrido
    _retire_screened_events(db, stats)
    db.commit()
    return stats
