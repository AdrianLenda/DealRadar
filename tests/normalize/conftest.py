"""Local fixtures for tests/normalize/ only. Does not modify tests/conftest.py (owner: A0, per prompty/README.md).

Defines `normalize_db` under a name distinct from the root `db` fixture, on a fresh in-memory schema. Small
helper functions (loading JSON fixtures, seeding sources/raw_offer rows) live directly in the test modules
that use them rather than here: pytest's default (no `__init__.py`) rootless import mode makes importing
plain functions from a file literally named `conftest.py` collide with tests/conftest.py's own module name,
so duplicating ~15 lines per test module that needs them is simpler than fighting that.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db


@pytest.fixture()
def normalize_db() -> Iterator[Session]:
    """Yields a Session on a fresh in-memory SQLite database with the full A0 schema already migrated."""
    engine = dealradar_db.build_engine("sqlite:///:memory:")
    dealradar_db.run_migrations(engine)
    session = dealradar_db.make_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
