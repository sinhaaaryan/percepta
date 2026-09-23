import os

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL",
                   "postgresql+psycopg://nightingale:nightingale@localhost:5432/nightingale_test"))

import pytest  # noqa: E402

from app import services  # noqa: E402
from app.db import SessionLocal, drop_all, init_db  # noqa: E402
from app.models import Period  # noqa: E402
from app.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def seeded_db():
    drop_all()
    init_db()
    with SessionLocal() as s:
        seed(s, period_days=14)
        s.commit()
    yield


@pytest.fixture(scope="session")
def solved(seeded_db):
    """(instance, schedule) of an optimizer-produced schedule, shared by tests."""
    with SessionLocal() as s:
        p = s.query(Period).first()
        v = services.run_solver(s, p, time_limit=10)
        s.commit()
        return v.id, v.instance_json, services.schedule_of(v)
