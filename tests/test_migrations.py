"""The Alembic migrations produce exactly the schema the ORM models describe."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from pitwall.db import Base


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    # APScheduler keeps its own table; it isn't part of Pitwall's models.
    diff = [d for d in diff if "apscheduler" not in repr(d)]
    assert diff == []
