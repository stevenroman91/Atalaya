"""Tests de auth (§9): invitación de uso único, lockout progresivo, sesiones."""
import pytest

from atalaya.web import auth


def test_invitation_flow(db):
    token = auth.create_invitation(db, "ana@example.org", role="analista")
    user = auth.accept_invitation(db, token, "contraseña-larga-1")
    assert user.email == "ana@example.org"
    # uso único
    with pytest.raises(auth.AuthError):
        auth.accept_invitation(db, token, "otra-contraseña-2")


def test_invitation_rejects_existing_user(db):
    token = auth.create_invitation(db, "bob@example.org")
    auth.accept_invitation(db, token, "contraseña-larga-1")
    with pytest.raises(auth.AuthError):
        auth.create_invitation(db, "bob@example.org")


def test_password_min_length(db):
    token = auth.create_invitation(db, "carla@example.org")
    with pytest.raises(auth.AuthError):
        auth.accept_invitation(db, token, "corta")


def test_login_and_progressive_lockout(db):
    token = auth.create_invitation(db, "dan@example.org")
    user = auth.accept_invitation(db, token, "contraseña-larga-1")

    ok = auth.login(db, "dan@example.org", "contraseña-larga-1", ip="10.0.0.1")
    assert ok.id == user.id

    # 5 fallos → cuenta bloqueada
    for i in range(5):
        with pytest.raises(auth.AuthError):
            auth.login(db, "dan@example.org", "mala", ip=f"10.0.1.{i}")
    db.refresh(user)
    assert user.locked_until is not None
    with pytest.raises(auth.AuthError) as exc:
        auth.login(db, "dan@example.org", "contraseña-larga-1", ip="10.0.2.1")
    assert exc.value.code in ("locked", "bad_credentials")


def test_ip_rate_limit(db):
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login(db, "nadie@example.org", "x", ip="10.9.9.9")
    with pytest.raises(auth.AuthError) as exc:
        auth.login(db, "nadie@example.org", "x", ip="10.9.9.9")
    assert exc.value.code == "rate_limited"


def test_sessions_and_reset(db):
    token = auth.create_invitation(db, "eva@example.org")
    user = auth.accept_invitation(db, token, "contraseña-larga-1")
    sess_token, csrf = auth.create_session(db, user)
    assert auth.get_session_user(db, sess_token)[0].id == user.id
    assert csrf

    reset = auth.create_password_reset(db, "eva@example.org")
    auth.use_password_reset(db, reset, "nueva-contraseña-99")
    # el reset revoca las sesiones existentes
    assert auth.get_session_user(db, sess_token) is None
    assert auth.login(db, "eva@example.org", "nueva-contraseña-99", ip="10.1.1.1")
    # el enlace es de uso único
    with pytest.raises(auth.AuthError):
        auth.use_password_reset(db, reset, "otra-mas-larga-77")
