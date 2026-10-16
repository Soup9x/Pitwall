"""OpenF1 post-session data: race control messages and measured conditions

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_data_fetch",
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("openf1_session_key", sa.Integer(), nullable=True),
        sa.Column("race_control_count", sa.Integer(), nullable=True),
        sa.Column("weather_samples", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "race_control_message",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("lap", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=True),
        sa.Column("flag", sa.String(length=30), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=True),
        sa.Column("sector", sa.Integer(), nullable=True),
        sa.Column("driver_number", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_race_control_message_session_id", "race_control_message", ["session_id"])
    op.create_table(
        "session_conditions",
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("track_min", sa.Float(), nullable=True),
        sa.Column("track_max", sa.Float(), nullable=True),
        sa.Column("track_avg", sa.Float(), nullable=True),
        sa.Column("air_min", sa.Float(), nullable=True),
        sa.Column("air_max", sa.Float(), nullable=True),
        sa.Column("air_avg", sa.Float(), nullable=True),
        sa.Column("humidity_min", sa.Float(), nullable=True),
        sa.Column("humidity_max", sa.Float(), nullable=True),
        sa.Column("humidity_avg", sa.Float(), nullable=True),
        sa.Column("rain", sa.Boolean(), nullable=True),
        sa.Column("wind_max_ms", sa.Float(), nullable=True),
        sa.Column("series_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )


def downgrade() -> None:
    op.drop_table("session_conditions")
    op.drop_table("race_control_message")
    op.drop_table("session_data_fetch")
