"""E2E: colecta real por HTTP contra el servidor de fixtures → extracción →
clustering → scoring → eventos publicados, con filtrado por preferencias y
recorrido web multi-usuario completo (§10)."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from atalaya.collect.fetcher import PoliteFetcher
from atalaya.db.models import Article, Event, EventStatus
from atalaya.jobs.runner import run_daily
from atalaya.web import auth


def _fetcher(base: str) -> PoliteFetcher:
    f = PoliteFetcher(base_url_override=base)
    f.delay = 0.0
    return f


def test_daily_pipeline_end_to_end(db, fixture_base):
    run = run_daily(db, countries=["GT"], fetcher=_fetcher(fixture_base))
    stats = run.stats

    # el artículo reciclado de 2019 fue rechazado por fecha real (§7.4)
    assert stats["collect"]["reject_reasons"].get("fuera de ventana (fecha real del artículo)")

    arts = list(db.scalars(select(Article)))
    assert all(a.url.startswith("https://") for a in arts)          # URLs reales resueltas
    assert all("news.google.com" not in a.url for a in arts)        # redirecciones GN resueltas
    assert any(a.source_type == "estatal" for a in arts)            # teleSUR marcado estatal
    assert all(a.text for a in arts if a.status == "extracted")     # texto íntegro almacenado

    events = list(db.scalars(select(Event)))
    published = [e for e in events if e.status == EventStatus.published.value]
    pending = [e for e in events if e.status == EventStatus.pending_confirm.value]

    # 1) la balacera (3 fuentes, 2 independientes) se publica como ALERTA
    assert len(published) == 1
    ev = published[0]
    assert ev.event_type == "ALERTA"
    assert ev.category == "crimen_alto_impacto"
    assert ev.recurrence == 3 and ev.independent_sources >= 2
    assert ev.has_state_media                       # tag «medio estatal — contrastar»
    assert ev.recommendations_es                    # 1–3 recomendaciones
    assert ev.summary_es and "difieren" in ev.summary_es  # divergencia 3 vs 5 heridos (§7.6)
    assert len(ev.articles) == 3                    # trazabilidad artículo↔evento (§7.2)

    # 2) el atentado (1 fuente, gravedad extrema) va a «pendiente de confirmar»
    assert len(pending) == 1
    assert "atentado" in pending[0].title_es.lower()

    # 3) idempotencia: relanzar no duplica nada (§8)
    n_events = len(events)
    run_daily(db, countries=["GT"], fetcher=_fetcher(fixture_base))
    assert len(list(db.scalars(select(Event)))) == n_events


def test_multiuser_web_flow(db, fixture_base):
    """§10(5): un admin invita, el usuario crea su cuenta, elige países e
    idioma, y ve un dashboard filtrado en consecuencia."""
    run_daily(db, countries=["GT"], fetcher=_fetcher(fixture_base))

    import os
    os.environ["ATALAYA_ADMIN_EMAIL"] = "admin@example.org"
    os.environ["ATALAYA_ADMIN_PASSWORD"] = "admin-password-123"
    auth.create_admin_from_env(db)

    from atalaya.web.app import app
    client = TestClient(app)

    # 1. login admin + invitación desde el panel
    r = client.post("/auth/login", data={"email": "admin@example.org",
                                         "password": "admin-password-123"},
                    follow_redirects=False)
    assert r.status_code == 303
    admin_cookie = r.cookies

    r = client.get("/admin", cookies=admin_cookie)
    assert r.status_code == 200
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/admin/invite", data={"email": "agente@ue.example",
                                           "role": "analista", "csrf_token": csrf},
                    cookies=admin_cookie, follow_redirects=False)
    assert "invite_link=" in r.headers["location"]
    token = r.headers["location"].split("/auth/invite/")[1]

    # 2. el invitado crea su cuenta con el enlace de uso único
    r = client.post(f"/auth/invite/{token}",
                    data={"password": "contraseña-larga-1", "password2": "contraseña-larga-1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/onboarding"
    user_cookie = r.cookies

    # 3. onboarding: sigue Guatemala, en francés
    r = client.get("/account/onboarding", cookies=user_cookie)
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/account", data={"countries": "GT", "lang": "fr",
                                      "timezone": "Europe/Brussels",
                                      "from_onboarding": "1", "csrf_token": csrf},
                    cookies=user_cookie, follow_redirects=False)
    assert r.status_code == 303

    # 4. dashboard filtrado y en francés
    r = client.get("/dashboard", cookies=user_cookie)
    assert r.status_code == 200
    assert "ALERTE" in r.text                      # interfaz en francés
    assert "Balacera" in r.text                    # el evento GT es visible
    assert "medio estatal" not in r.text or "média d'État" in r.text
    assert "résumé disponible en espagnol uniquement" in r.text  # sin traducción cacheada → canónico marcado

    # 5. un usuario que NO sigue GT no ve el evento por defecto
    token2 = auth.create_invitation(db, "otro@ue.example")
    auth_user = auth.accept_invitation(db, token2, "contraseña-larga-2")
    auth_user.countries = ["AR"]
    auth_user.onboarded = True
    db.commit()
    r = client.post("/auth/login", data={"email": "otro@ue.example",
                                         "password": "contraseña-larga-2"},
                    follow_redirects=False)
    r = client.get("/dashboard", cookies=r.cookies)
    assert "Balacera" not in r.text
    # …pero puede ampliar puntualmente a todo el perímetro (§6.0)
    r2 = client.get("/dashboard?scope=all", cookies=r.cookies)
    assert r2.status_code == 200

    # 6. export briefing HTML
    r = client.get("/dashboard/briefing", cookies=user_cookie)
    assert r.status_code == 200 and "Atalaya" in r.text

    # 7. CSRF: un POST sin token es rechazado
    r = client.post("/account", data={"countries": "GT", "lang": "en"},
                    cookies=user_cookie)
    assert r.status_code == 403
