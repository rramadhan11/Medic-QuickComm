"""Integration tests run against a real Postgres (row locks and constraints are part of
what's under test). Start it with `docker compose up -d db`; the `medic_test` database
is created and migrated automatically."""

import os

TEST_DATABASE_URL = os.environ.setdefault(
    "TEST_DATABASE_URL", "postgresql+psycopg://medic:medic@localhost:5432/medic_test"
)
# Must be set before anything imports app.db, which binds the engine at import time.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from datetime import UTC, datetime, timedelta  # noqa: E402
from itertools import count  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, make_url, text  # noqa: E402


@pytest.fixture(scope="session")
def database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def session(database):
    from app.db import SessionLocal, engine
    from app.seed import seed

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE allocations, order_lines, orders, warehouse_stock, "
                "delivery_records, warehouses, products CASCADE"
            )
        )
    with SessionLocal() as s:
        seed(s)
        yield s


# Tuesday 11:00 WIB (weekday off-peak): ETAs depend on the clock, so pin it.
# It ticks one second per read so records created in sequence stay ordered.
API_NOW = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import get_now

    ticks = count()
    app.dependency_overrides[get_now] = lambda: API_NOW + timedelta(seconds=next(ticks))
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
