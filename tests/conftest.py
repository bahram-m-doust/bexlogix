"""Shared pytest fixtures.

Each test gets a fresh in-memory SQLite database. We set the env var before
importing the application modules so the engine binds to the test database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so 'server' and 'client' resolve.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Bind the application to an isolated in-memory SQLite DB for every test run.
# Never inherit a production/container URL: db_session drops all tables by
# design, so setdefault() would be unsafe when BEXLOGIX_DATABASE_URL exists.
os.environ["BEXLOGIX_DATABASE_URL"] = "sqlite:///:memory:"


@pytest.fixture
def db_session():
    """Yield a clean SQLAlchemy session backed by a fresh schema.

    Uses a file-less in-memory database that is reset between tests by
    dropping and recreating all tables. This keeps tests fast and isolated.
    """
    from server.db.base import Base
    from server.db.create_tables import create_tables
    from server.db.database import SessionLocal, engine

    Base.metadata.drop_all(bind=engine)
    create_tables()

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
