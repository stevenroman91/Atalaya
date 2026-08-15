"""Modelos de datos de Atalaya.

Principios (§7 del pliego):
- Todo artículo almacenado lleva su texto bruto y timestamp de fetch.
- Un evento referencia los IDs de sus artículos fuente (trazabilidad total).
- El español es la lengua canónica; `translations` cachea fr/en/pt con la
  versión (hash) del canónico para regenerar si este cambia.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Colecta ──────────────────────────────────────────────────────────────────

class CollectRun(Base):
    """Una ejecución de un job (diario / semanal / mensual). Base de la idempotencia."""
    __tablename__ = "collect_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))          # daily | weekly | monthly
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)  # anulación cooperativa desde admin
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceRecord(Base):
    """Estado runtime de una fuente de la lista blanca (la definición vive en
    config/sources.yaml). Persiste el RSS autodescubierto y la salud de colecta."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    discovered_rss: Mapped[str | None] = mapped_column(String(1024))  # nunca inventada: hallada en <link rel=alternate>
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class ArticleStatus(str, enum.Enum):
    extracted = "extracted"        # texto íntegro disponible → puede resumirse
    title_only = "title_only"      # sin texto: solo se lista «título solamente»
    rejected = "rejected"          # fuera de ventana, granja de contenido, etc.


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("url", name="uq_articles_url"),
        Index("ix_articles_country_fetched", "country", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collect_runs.id"))
    url: Mapped[str] = mapped_column(String(2048))          # URL canónica, redirecciones GN resueltas
    gn_url: Mapped[str | None] = mapped_column(String(2048), index=True)
    domain: Mapped[str] = mapped_column(String(255))
    source_name: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str | None] = mapped_column(String(32))  # independiente|estatal|internacional|off_whitelist
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)          # texto bruto extraído (trafilatura)
    lang: Mapped[str | None] = mapped_column(String(8))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    country: Mapped[str | None] = mapped_column(String(2))
    zone_id: Mapped[str | None] = mapped_column(String(64))
    keyword: Mapped[str | None] = mapped_column(String(128))  # palabra clave que lo trajo
    theme: Mapped[str | None] = mapped_column(String(32))     # tema semanal, si aplica
    status: Mapped[str] = mapped_column(String(16), default=ArticleStatus.extracted.value)
    reject_reason: Mapped[str | None] = mapped_column(String(255))

    events: Mapped[list["EventArticle"]] = relationship(back_populates="article")


# ── Eventos (clusters) ───────────────────────────────────────────────────────

class EventStatus(str, enum.Enum):
    published = "published"            # alerta o nota visible en el dashboard
    pending_confirm = "pending_confirm"  # 1 fuente + gravedad extrema → cola «a confirmar»
    discarded = "discarded"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_country_occurred", "country", "occurred_at"),
        UniqueConstraint("dedup_key", name="uq_events_dedup_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collect_runs.id"))
    dedup_key: Mapped[str] = mapped_column(String(64))       # hash estable del cluster → idempotencia
    country: Mapped[str] = mapped_column(String(2))
    zone_id: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)

    title_es: Mapped[str] = mapped_column(Text)
    summary_es: Mapped[str | None] = mapped_column(Text)
    recommendations_es: Mapped[list | None] = mapped_column(JSON)   # 1–3 recomendaciones (solo ALERTA)
    summary_version: Mapped[str | None] = mapped_column(String(64)) # hash del canónico → invalida traducciones

    event_type: Mapped[str | None] = mapped_column(String(16))  # ALERTA | NOTA
    category: Mapped[str | None] = mapped_column(String(32))    # crimen_alto_impacto, …
    level: Mapped[str | None] = mapped_column(String(16))       # advertencia | informativo
    status: Mapped[str] = mapped_column(String(20), default=EventStatus.published.value)

    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # fecha del evento (mejor estimación sourced)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    recurrence: Mapped[int] = mapped_column(Integer, default=0)             # nº de fuentes distintas
    independent_sources: Mapped[int] = mapped_column(Integer, default=0)    # nº de fuentes independientes
    has_state_media: Mapped[bool] = mapped_column(Boolean, default=False)   # tag «medio estatal — contrastar»
    score_detail: Mapped[dict | None] = mapped_column(JSON)                 # trazas del scoring (auditabilidad)

    articles: Mapped[list["EventArticle"]] = relationship(back_populates="event")
    translations: Mapped[list["Translation"]] = relationship(back_populates="event")


class EventArticle(Base):
    """Trazabilidad §7.2: qué artículos sustentan cada evento."""
    __tablename__ = "event_articles"
    __table_args__ = (UniqueConstraint("event_id", "article_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))

    event: Mapped[Event] = relationship(back_populates="articles")
    article: Mapped[Article] = relationship(back_populates="events")


class Translation(Base):
    """Cache de traducciones del canónico español. version = summary_version del
    evento en el momento de traducir; si difiere, la traducción se regenera."""
    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("event_id", "lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    lang: Mapped[str] = mapped_column(String(8))
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    recommendations: Mapped[list | None] = mapped_column(JSON)
    version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped[Event] = relationship(back_populates="translations")


# ── Vigilancia semanal / síntesis mensual ────────────────────────────────────

class WeeklyItem(Base):
    """Artículo clasificado país × tema para pre-estructurar la síntesis mensual."""
    __tablename__ = "weekly_items"
    __table_args__ = (
        UniqueConstraint("article_id", "theme"),
        Index("ix_weekly_country_week", "country", "iso_week"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collect_runs.id"))
    country: Mapped[str] = mapped_column(String(2))
    theme: Mapped[str] = mapped_column(String(32))
    iso_week: Mapped[str] = mapped_column(String(10))       # p.ej. 2026-W33
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    mini_summary_es: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped[Article] = relationship()


class MonthlySynthesis(Base):
    __tablename__ = "monthly_syntheses"
    __table_args__ = (UniqueConstraint("country", "month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    country: Mapped[str] = mapped_column(String(2))
    month: Mapped[str] = mapped_column(String(7))           # YYYY-MM
    overview_es: Mapped[str | None] = mapped_column(Text)   # síntesis global del país
    sections: Mapped[dict] = mapped_column(JSON, default=dict)   # {tema: {sintesis, articulos:[{article_id,...}]}}
    incidents: Mapped[list] = mapped_column(JSON, default=list)  # tabla de incidentes [{fecha, localizacion, nivel, categoria, descripcion, fuentes:[{url,name}], event_id}]
    version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    translations_json: Mapped[dict | None] = mapped_column(JSON)  # {lang: {overview, sections:{tema: sintesis}}}


# ── Cuentas y auth ───────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="analista")  # admin | analista
    display_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Preferencias
    lang: Mapped[str] = mapped_column(String(8), default="es")
    timezone: Mapped[str] = mapped_column(String(64), default="America/Mexico_City")
    countries: Mapped[list] = mapped_column(JSON, default=list)      # países seguidos, [] = onboarding pendiente
    mx_zones: Mapped[list | None] = mapped_column(JSON)              # None/[] = todas las zonas MX
    email_briefing: Mapped[bool] = mapped_column(Boolean, default=False)
    theme: Mapped[str] = mapped_column(String(8), default="system")  # system | light | dark
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)

    # Marcador «nuevo» por cuenta
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Anti fuerza bruta (bloqueo progresivo)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    lockouts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invitation(Base):
    """Invitación de admin: enlace de uso único y duración limitada. Solo se
    guarda el hash del token."""
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="analista")
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebSession(Base):
    """Sesiones en base (revocables). La cookie lleva el token firmado; aquí
    solo su hash."""
    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    """Rate limiting de login por IP (además del bloqueo por cuenta)."""
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_ip_at", "ip", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
