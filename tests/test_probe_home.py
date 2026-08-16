"""El diagnóstico de una fuente, desde el panel de administración.

Existe porque la lectura de portada se programó sin poder verla nunca: el
entorno de desarrollo no tiene salida a internet. El botón mide sobre la
página real —cuántos enlaces, cuántos con forma de artículo, cuántos
pertinentes— y no escribe nada: ni artículos, ni salud de la fuente.
"""
import os

from fastapi.testclient import TestClient
from sqlalchemy import select

from atalaya.db.models import Article, SourceRecord
from atalaya.web import auth

PORTADA = """
<html><body>
  <nav><a href="/">Inicio</a></nav>
  <a href="/nacionales/2026/08/16/balacera-en-el-centro-de-asuncion/">
     Balacera deja dos heridos en el centro de Asunción</a>
  <a href="/opinion/2026/08/16/la-columna-del-domingo/">
     La columna del domingo sobre el futuro del país</a>
</body></html>
"""


class _Resp:
    def __init__(self, text, url):
        self.text, self.url, self.content = text, url, text.encode()


def _admin_client(db) -> tuple[TestClient, dict]:
    os.environ["ATALAYA_ADMIN_EMAIL"] = "admin@example.org"
    os.environ["ATALAYA_ADMIN_PASSWORD"] = "admin-password-123"
    auth.create_admin_from_env(db)
    from atalaya.web.app import app
    client = TestClient(app)
    r = client.post("/auth/login", data={"email": "admin@example.org",
                                         "password": "admin-password-123"},
                    follow_redirects=False)
    return client, r.cookies


def _probe(db, monkeypatch, page: _Resp | None, domain="abc.com.py") -> str:
    from atalaya.collect.fetcher import PoliteFetcher
    monkeypatch.setattr(PoliteFetcher, "get", lambda self, url, **kw: page)
    client, cookie = _admin_client(db)
    csrf = client.get("/admin", cookies=cookie).text.split(
        'name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/admin/probe-home",
                    data={"domain": domain, "csrf_token": csrf},
                    cookies=cookie, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def test_portada_util_devuelve_las_cifras(db, monkeypatch):
    loc = _probe(db, monkeypatch, _Resp(PORTADA, "https://www.abc.com.py/"))

    assert "probe=abc.com.py" in loc
    # 2 artículos detectados, 1 pertinente (la columna de opinión no cuenta)
    assert "2+con+forma+de+art" in loc
    assert "1+pertinentes" in loc


def test_portada_renderizada_por_javascript_se_declara_inutil(db, monkeypatch):
    loc = _probe(db, monkeypatch,
                 _Resp("<html><body><div id=app></div></body></html>",
                       "https://www.milenio.com/"),
                 domain="milenio.com")

    assert "JavaScript" in loc


def test_portada_inalcanzable_lo_dice(db, monkeypatch):
    loc = _probe(db, monkeypatch, None)

    assert "inalcanzable" in loc


def test_el_diagnostico_no_escribe_nada(db, monkeypatch):
    """Es una consulta, no una colecta: ni artículos ni salud de fuente."""
    _probe(db, monkeypatch, _Resp(PORTADA, "https://www.abc.com.py/"))

    assert not list(db.scalars(select(Article)))
    assert not list(db.scalars(select(SourceRecord)))
