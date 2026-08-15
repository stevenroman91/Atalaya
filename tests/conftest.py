import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="atalaya-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["ATALAYA_INSECURE_COOKIES"] = "1"
os.environ.setdefault("ATALAYA_TRANSLATE", "none")

import pytest  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from atalaya.db.base import SessionLocal, engine  # noqa: E402
from atalaya.db.models import Base  # noqa: E402

Base.metadata.create_all(engine)


@pytest.fixture()
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.rollback()
        # limpieza total entre tests
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()


@pytest.fixture(scope="session")
def fixture_base():
    from tests.fixture_server import start_fixture_server
    server, base = start_fixture_server()
    yield base
    server.shutdown()
