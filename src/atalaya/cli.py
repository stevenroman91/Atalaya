"""CLI de Atalaya.

  atalaya init-db                  aplica las migraciones Alembic
  atalaya create-admin             crea el admin inicial (ADMIN_EMAIL/ADMIN_PASSWORD)
  atalaya invite EMAIL [--role r]  crea una invitación y muestra el enlace
  atalaya collect-daily [--country XX] [--fixture-base URL]
  atalaya collect-weekly [--country XX] [--fixture-base URL]
  atalaya monthly [--month YYYY-MM]
  atalaya translate                regenera traducciones pendientes
  atalaya serve [--host H] [--port P]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def _project_root():
    """Directorio que contiene alembic.ini y migrations/.

    Con el paquete instalado por pip, __file__ está en site-packages y no
    sirve como referencia: se prueba ATALAYA_ROOT, luego el cwd (en Docker,
    /app) y por último la raíz del repo (modo desarrollo).
    """
    from pathlib import Path
    candidates = []
    if os.environ.get("ATALAYA_ROOT"):
        candidates.append(Path(os.environ["ATALAYA_ROOT"]))
    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parents[2])
    for root in candidates:
        if (root / "alembic.ini").is_file() and (root / "migrations").is_dir():
            return root
    raise SystemExit(
        "No se encuentra alembic.ini/migrations. Define ATALAYA_ROOT o ejecuta "
        f"desde la raíz del proyecto (probado: {', '.join(str(c) for c in candidates)})."
    )


def _init_db() -> None:
    from alembic import command
    from alembic.config import Config
    root = _project_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    print("Base de datos migrada.")


def _create_admin() -> None:
    from atalaya.db import SessionLocal
    from atalaya.web.auth import create_admin_from_env
    with SessionLocal() as db:
        user, created = create_admin_from_env(db)
        print(f"Admin {'creado' if created else 'ya existente'}: {user.email}")


def _invite(email: str, role: str) -> None:
    from atalaya.db import SessionLocal
    from atalaya.web.auth import create_invitation
    with SessionLocal() as db:
        token = create_invitation(db, email=email, role=role, created_by=None)
        base = os.environ.get("ATALAYA_BASE_URL", "http://localhost:8000")
        print(f"Invitación para {email} (rol {role}):\n{base}/auth/invite/{token}")


def _probe_home(domains: list[str]) -> None:
    """Mide qué daría leer la portada de un medio, SIN escribir en base.

    Existe porque la lectura de portada se programó a ciegas: el sandbox de
    desarrollo no tiene salida a internet, así que nadie había visto una
    sola de esas páginas. Esto las mira de verdad y da los números —
    cuántos enlaces hay, cuántos parecen artículos, cuántos sobreviven al
    filtro de sección — antes de encender nada en producción.
    """
    from atalaya.collect.collector import Collector
    from atalaya.collect.fetcher import PoliteFetcher
    from atalaya.collect.whitelist import norm_domain, off_topic_section

    fetcher = PoliteFetcher()
    for domain in domains:
        d = norm_domain(domain)
        home = f"https://{d}/"
        resp = fetcher.get(home)
        if not resp:
            print(f"{d}: portada inalcanzable (robots.txt o error HTTP) — "
                  f"no se insiste")
            continue
        base = str(getattr(resp, "url", "") or home)
        html = resp.text
        anchors = len(Collector._LINK_RE.findall(html))
        links = Collector._article_links_from_html(base, html, norm_domain(base))
        useful = [(u, t) for u, t in links if not off_topic_section(u)]
        print(f"\n{d} → {base}  ({len(html)//1024} kB)")
        print(f"  enlaces en la página : {anchors}")
        print(f"  con pinta de artículo: {len(links)}")
        print(f"  fuera de secciones ajenas: {len(useful)}")
        for u, t in useful[:5]:
            print(f"    · {t[:80]}\n      {u}")
        if not links and anchors > 50:
            print("  → la página tiene enlaces pero ninguno pasa el filtro: "
                  "revisar la forma de sus URL antes de encender home_scrape")
        elif anchors < 10:
            print("  → casi sin enlaces en el HTML: portada renderizada por "
                  "JavaScript; leerla no aportaría nada")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("ATALAYA_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="atalaya")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")
    sub.add_parser("create-admin")
    p = sub.add_parser("invite")
    p.add_argument("email")
    p.add_argument("--role", default="analista", choices=["admin", "analista"])
    for name in ("collect-daily", "collect-weekly"):
        p = sub.add_parser(name)
        p.add_argument("--country", action="append", dest="countries")
        p.add_argument("--fixture-base", help="URL base para reescribir peticiones (tests)")
    p = sub.add_parser("monthly")
    p.add_argument("--month", help="YYYY-MM (por defecto, el mes anterior)")
    sub.add_parser("translate")
    p = sub.add_parser("probe-home",
                       help="¿La portada de un medio sin flujo trae artículos?")
    p.add_argument("domain", nargs="+")
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))

    args = parser.parse_args(argv)

    if args.cmd == "init-db":
        _init_db()
    elif args.cmd == "create-admin":
        _create_admin()
    elif args.cmd == "invite":
        _invite(args.email, args.role)
    elif args.cmd in ("collect-daily", "collect-weekly"):
        from atalaya.collect.fetcher import PoliteFetcher
        from atalaya.db import SessionLocal
        from atalaya.jobs.runner import run_daily, run_weekly
        fetcher = PoliteFetcher(base_url_override=args.fixture_base) if args.fixture_base else None
        with SessionLocal() as db:
            fn = run_daily if args.cmd == "collect-daily" else run_weekly
            run = fn(db, countries=args.countries, fetcher=fetcher)
            print(json.dumps(run.stats, indent=2, ensure_ascii=False))
    elif args.cmd == "monthly":
        from atalaya.db import SessionLocal
        from atalaya.jobs.runner import run_monthly
        with SessionLocal() as db:
            run = run_monthly(db, month=args.month)
            print(json.dumps(run.stats, indent=2, ensure_ascii=False))
    elif args.cmd == "translate":
        from atalaya.db import SessionLocal
        from atalaya.process.translate import translate_pending
        with SessionLocal() as db:
            print(json.dumps(translate_pending(db), indent=2))
    elif args.cmd == "probe-home":
        _probe_home(args.domain)
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("atalaya.web.app:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
