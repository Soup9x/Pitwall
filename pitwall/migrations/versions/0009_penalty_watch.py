"""penalty watch: FIA documents, power unit usage, grid penalties, licence points

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _provenance() -> list[sa.Column]:
    return [
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_by", sa.Integer(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "fia_document",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_fia_document_event_id", "fia_document", ["event_id"])
    op.create_table(
        "pu_allocation",
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("element", sa.String(length=10), nullable=False),
        sa.Column("allowed", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("season", "element"),
    )
    op.create_table(
        "pu_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.Column("element", sa.String(length=10), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        *_provenance(),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["driver_id"], ["driver.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["fia_document.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "driver_id", "element", "origin"),
    )
    op.create_index("ix_pu_usage_event_id", "pu_usage", ["event_id"])
    op.create_index("ix_pu_usage_driver_id", "pu_usage", ["driver_id"])
    op.create_table(
        "grid_penalty",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.Column("places", sa.Integer(), nullable=True),
        sa.Column("back_of_grid", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        *_provenance(),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["driver_id"], ["driver.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["fia_document.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_grid_penalty_event_id", "grid_penalty", ["event_id"])
    op.create_index("ix_grid_penalty_driver_id", "grid_penalty", ["driver_id"])
    op.create_table(
        "licence_points",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("awarded_on", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        *_provenance(),
        sa.ForeignKeyConstraint(["driver_id"], ["driver.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["fia_document.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_licence_points_driver_id", "licence_points", ["driver_id"])
    op.create_index("ix_licence_points_event_id", "licence_points", ["event_id"])


def downgrade() -> None:
    op.drop_table("licence_points")
    op.drop_table("grid_penalty")
    op.drop_table("pu_usage")
    op.drop_table("pu_allocation")
    op.drop_table("fia_document")
