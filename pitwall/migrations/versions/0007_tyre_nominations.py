"""Pirelli tyre nominations per event

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tyre_nomination",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("hard", sa.String(length=2), nullable=False),
        sa.Column("medium", sa.String(length=2), nullable=False),
        sa.Column("soft", sa.String(length=2), nullable=False),
        sa.Column("source_title", sa.String(length=300), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("source_published_at", sa.DateTime(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "origin"),
    )
    op.create_index("ix_tyre_nomination_event_id", "tyre_nomination", ["event_id"])


def downgrade() -> None:
    op.drop_table("tyre_nomination")
