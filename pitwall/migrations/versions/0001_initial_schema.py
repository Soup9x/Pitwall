"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-15 14:18:35.644017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "api_cache",
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("url"),
    )
    op.create_table(
        "circuit",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("locality", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("tz_name", sa.String(length=64), nullable=True),
        sa.Column("url", sa.String(length=300), nullable=True),
        sa.Column("length_km", sa.Float(), nullable=True),
        sa.Column("laps", sa.Integer(), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=True),
        sa.Column("drs_zones", sa.Integer(), nullable=True),
        sa.Column("first_gp_year", sa.Integer(), nullable=True),
        sa.Column("lap_record_driver", sa.String(length=100), nullable=True),
        sa.Column("lap_record_time", sa.String(length=20), nullable=True),
        sa.Column("lap_record_year", sa.Integer(), nullable=True),
        sa.Column("map_image", sa.String(length=300), nullable=True),
        sa.Column("facts_verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "constructor",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("nationality", sa.String(length=50), nullable=True),
        sa.Column("url", sa.String(length=300), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "driver",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=3), nullable=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("given_name", sa.String(length=100), nullable=False),
        sa.Column("family_name", sa.String(length=100), nullable=False),
        sa.Column("dob", sa.Date(), nullable=True),
        sa.Column("nationality", sa.String(length=50), nullable=True),
        sa.Column("url", sa.String(length=300), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "notification_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_notification_log_payload_hash"), ["payload_hash"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_notification_log_sent_at"), ["sent_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_notification_log_type"), ["type"], unique=False)

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("channels", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("spoiler", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("hold_reason", sa.String(length=20), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_outbox_payload_hash"), ["payload_hash"], unique=False)
        batch_op.create_index(batch_op.f("ix_outbox_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_outbox_type"), ["type"], unique=False)

    op.create_table(
        "season",
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("backfilled", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("year"),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "source_status",
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("source"),
    )
    op.create_table(
        "standings_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=12), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("points", sa.Float(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("season", "round", "type", "entity_id"),
    )
    with op.batch_alter_table("standings_snapshot", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_standings_snapshot_season"), ["season"], unique=False)

    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("circuit_id", sa.String(length=64), nullable=False),
        sa.Column("is_sprint_weekend", sa.Boolean(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("url", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(
            ["circuit_id"],
            ["circuit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["season"],
            ["season.year"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    with op.batch_alter_table("event", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_event_circuit_id"), ["circuit_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_event_round"), ["round"], unique=False)
        batch_op.create_index(batch_op.f("ix_event_season"), ["season"], unique=False)

    op.create_table(
        "followed_driver",
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["driver_id"],
            ["driver.id"],
        ),
        sa.PrimaryKeyConstraint("driver_id"),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("start_utc", sa.DateTime(), nullable=True),
        sa.Column("end_utc_est", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(), nullable=False),
        sa.Column("results_fetched_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "type"),
    )
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_session_event_id"), ["event_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_session_start_utc"), ["start_utc"], unique=False)

    op.create_table(
        "spoiler_state",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("watched", sa.Boolean(), nullable=False),
        sa.Column("watched_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_table(
        "result",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.Column("constructor_id", sa.String(length=64), nullable=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("grid", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("position_text", sa.String(length=5), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("points", sa.Float(), nullable=False),
        sa.Column("laps", sa.Integer(), nullable=True),
        sa.Column("time_text", sa.String(length=30), nullable=True),
        sa.Column("fastest_lap_rank", sa.Integer(), nullable=True),
        sa.Column("fastest_lap_time", sa.String(length=20), nullable=True),
        sa.Column("q1", sa.String(length=20), nullable=True),
        sa.Column("q2", sa.String(length=20), nullable=True),
        sa.Column("q3", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(
            ["constructor_id"],
            ["constructor.id"],
        ),
        sa.ForeignKeyConstraint(
            ["driver_id"],
            ["driver.id"],
        ),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "driver_id"),
    )
    with op.batch_alter_table("result", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_result_driver_id"), ["driver_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_result_session_id"), ["session_id"], unique=False)


def downgrade() -> None:

    with op.batch_alter_table("result", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_result_session_id"))
        batch_op.drop_index(batch_op.f("ix_result_driver_id"))

    op.drop_table("result")
    op.drop_table("spoiler_state")
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_session_start_utc"))
        batch_op.drop_index(batch_op.f("ix_session_event_id"))

    op.drop_table("session")
    op.drop_table("followed_driver")
    with op.batch_alter_table("event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_event_season"))
        batch_op.drop_index(batch_op.f("ix_event_round"))
        batch_op.drop_index(batch_op.f("ix_event_circuit_id"))

    op.drop_table("event")
    with op.batch_alter_table("standings_snapshot", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_standings_snapshot_season"))

    op.drop_table("standings_snapshot")
    op.drop_table("source_status")
    op.drop_table("setting")
    op.drop_table("season")
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_outbox_type"))
        batch_op.drop_index(batch_op.f("ix_outbox_status"))
        batch_op.drop_index(batch_op.f("ix_outbox_payload_hash"))

    op.drop_table("outbox")
    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notification_log_type"))
        batch_op.drop_index(batch_op.f("ix_notification_log_sent_at"))
        batch_op.drop_index(batch_op.f("ix_notification_log_payload_hash"))

    op.drop_table("notification_log")
    op.drop_table("driver")
    op.drop_table("constructor")
    op.drop_table("circuit")
    op.drop_table("api_cache")
