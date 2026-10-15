from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

import pitwall.models  # noqa: F401  (registers tables)
from pitwall.config import get_env
from pitwall.db import Base

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # APScheduler manages its own job table.
    return not (type_ == "table" and name == "apscheduler_jobs")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or get_env().db_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        section = config.get_section(config.config_ini_section) or {}
        section.setdefault("sqlalchemy.url", get_env().db_url)
        engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with engine.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
