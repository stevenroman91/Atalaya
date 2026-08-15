"""Capa de autenticación de Atalaya (§6.0) — aislada del framework web para
poder enchufar un SSO más adelante.

- Sin registro abierto: cuentas solo por invitación de un admin.
- Contraseñas: Argon2id. Tokens (invitación, reset, sesión): aleatorios de
  32 bytes; en base solo se guarda su hash SHA-256.
- Rate limiting de login por IP + bloqueo progresivo por cuenta.
- Sesiones en base (revocables) con CSRF token asociado.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atalaya.config import load_auth_config
from atalaya.db.models import (
    Invitation, LoginAttempt, PasswordReset, User, WebSession, utcnow,
)

_hasher = PasswordHasher()  # Argon2id por defecto


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _token() -> str:
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    cfg = load_auth_config()
    min_len = int(cfg.get("passwords", {}).get("min_length", 10))
    if len(password) < min_len:
        raise AuthError("password_too_short", f"mínimo {min_len} caracteres")
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# ── Admin inicial (§6.0: un solo admin creado al desplegar vía env) ──────────

def create_admin_from_env(db: Session) -> tuple[User, bool]:
    email = os.environ.get("ATALAYA_ADMIN_EMAIL")
    password = os.environ.get("ATALAYA_ADMIN_PASSWORD")
    if not email or not password:
        raise AuthError("missing_env", "define ATALAYA_ADMIN_EMAIL y ATALAYA_ADMIN_PASSWORD")
    existing = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if existing:
        return existing, False
    user = User(email=email.lower(), password_hash=hash_password(password),
                role="admin", onboarded=True, countries=[])
    db.add(user)
    db.commit()
    return user, True


# ── Invitaciones ─────────────────────────────────────────────────────────────

def create_invitation(db: Session, email: str, role: str = "analista",
                      created_by: int | None = None) -> str:
    cfg = load_auth_config()["invitations"]
    email = email.strip().lower()
    allowed = cfg.get("allowed_email_domains") or []
    if allowed and email.split("@")[-1] not in allowed:
        raise AuthError("domain_not_allowed", "dominio de e-mail no autorizado")
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise AuthError("already_user", "ya existe una cuenta con ese e-mail")
    token = _token()
    db.add(Invitation(
        email=email, role=role, token_hash=_hash_token(token),
        expires_at=utcnow() + timedelta(hours=float(cfg.get("ttl_hours", 72))),
        created_by=created_by,
    ))
    db.commit()
    return token


def get_valid_invitation(db: Session, token: str) -> Invitation | None:
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == _hash_token(token)))
    if not inv or inv.used_at is not None:
        return None
    expires = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(tzinfo=timezone.utc)
    if expires < utcnow():
        return None
    return inv


def accept_invitation(db: Session, token: str, password: str) -> User:
    inv = get_valid_invitation(db, token)
    if not inv:
        raise AuthError("invalid_invitation", "invitación inválida, usada o caducada")
    user = User(email=inv.email, password_hash=hash_password(password), role=inv.role)
    inv.used_at = utcnow()      # uso único
    db.add(user)
    db.commit()
    return user


# ── Login con rate limiting + bloqueo progresivo ─────────────────────────────

def _ip_rate_limited(db: Session, ip: str) -> bool:
    cfg = load_auth_config()["rate_limit"]
    window_start = utcnow() - timedelta(minutes=float(cfg["login_window_minutes"]))
    failures = db.scalar(select(func.count(LoginAttempt.id)).where(
        LoginAttempt.ip == ip, LoginAttempt.ok.is_(False), LoginAttempt.at >= window_start))
    return (failures or 0) >= int(cfg["login_max_attempts"])


def login(db: Session, email: str, password: str, ip: str) -> User:
    cfg = load_auth_config()["rate_limit"]
    email = email.strip().lower()
    if _ip_rate_limited(db, ip):
        raise AuthError("rate_limited", "demasiados intentos; espera unos minutos")

    user = db.scalar(select(User).where(func.lower(User.email) == email))

    def _fail() -> None:
        db.add(LoginAttempt(ip=ip, email=email, ok=False))
        db.commit()

    if not user or not user.active:
        _fail()
        raise AuthError("bad_credentials", "credenciales inválidas")

    if user.locked_until:
        locked = user.locked_until if user.locked_until.tzinfo else user.locked_until.replace(tzinfo=timezone.utc)
        if locked > utcnow():
            _fail()
            raise AuthError("locked", "cuenta bloqueada temporalmente")

    if not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= int(cfg["login_max_attempts"]):
            # bloqueo progresivo: base * 2^n, con techo
            minutes = min(
                float(cfg["lockout_base_minutes"]) * (2 ** user.lockouts),
                float(cfg["lockout_max_minutes"]),
            )
            user.locked_until = utcnow() + timedelta(minutes=minutes)
            user.lockouts += 1
            user.failed_logins = 0
        _fail()
        raise AuthError("bad_credentials", "credenciales inválidas")

    user.failed_logins = 0
    user.locked_until = None
    db.add(LoginAttempt(ip=ip, email=email, ok=True))
    db.commit()
    return user


# ── Sesiones ─────────────────────────────────────────────────────────────────

def create_session(db: Session, user: User) -> tuple[str, str]:
    """Devuelve (token de sesión para la cookie, token CSRF)."""
    token = _token()
    csrf = secrets.token_urlsafe(16)
    db.add(WebSession(user_id=user.id, token_hash=_hash_token(token), csrf_token=csrf))
    db.commit()
    return token, csrf


def get_session_user(db: Session, token: str | None) -> tuple[User, WebSession] | None:
    if not token:
        return None
    sess = db.scalar(select(WebSession).where(
        WebSession.token_hash == _hash_token(token), WebSession.revoked.is_(False)))
    if not sess:
        return None
    cfg = load_auth_config()["sessions"]
    now = utcnow()
    last = sess.last_active_at if sess.last_active_at.tzinfo else sess.last_active_at.replace(tzinfo=timezone.utc)
    created = sess.created_at if sess.created_at.tzinfo else sess.created_at.replace(tzinfo=timezone.utc)
    if last + timedelta(minutes=float(cfg["idle_timeout_minutes"])) < now:
        return None
    if created + timedelta(hours=float(cfg["absolute_lifetime_hours"])) < now:
        return None
    user = db.get(User, sess.user_id)
    if not user or not user.active:
        return None
    sess.last_active_at = now
    db.commit()
    return user, sess


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    sess = db.scalar(select(WebSession).where(WebSession.token_hash == _hash_token(token)))
    if sess:
        sess.revoked = True
        db.commit()


# ── Reset de contraseña ──────────────────────────────────────────────────────

def create_password_reset(db: Session, email: str) -> str | None:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if not user:
        return None   # no revelar si la cuenta existe
    ttl = float(load_auth_config()["password_reset"]["ttl_hours"])
    token = _token()
    db.add(PasswordReset(user_id=user.id, token_hash=_hash_token(token),
                         expires_at=utcnow() + timedelta(hours=ttl)))
    db.commit()
    return token


def use_password_reset(db: Session, token: str, new_password: str) -> User:
    pr = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == _hash_token(token)))
    if not pr or pr.used_at is not None:
        raise AuthError("invalid_reset", "enlace inválido o ya usado")
    expires = pr.expires_at if pr.expires_at.tzinfo else pr.expires_at.replace(tzinfo=timezone.utc)
    if expires < utcnow():
        raise AuthError("invalid_reset", "enlace caducado")
    user = db.get(User, pr.user_id)
    user.password_hash = hash_password(new_password)
    pr.used_at = utcnow()
    # revocar todas las sesiones existentes
    for s in db.scalars(select(WebSession).where(WebSession.user_id == user.id)):
        s.revoked = True
    db.commit()
    return user
