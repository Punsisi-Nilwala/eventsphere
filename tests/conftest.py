import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SEED_DEMO"] = "0"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402

import app.agents  # noqa: E402,F401
from app.db import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = SessionLocal()
    yield s
    s.close()
