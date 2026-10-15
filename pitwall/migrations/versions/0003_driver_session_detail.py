"""per-driver session detail from OpenF1

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "driver_session_detail",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.Column("openf1_session_key", sa.Integer(), nullable=True),
        sa.Column("best_lap_s", sa.Float(), nullable=True),
        sa.Column("pit_count", sa.Integer(), nullable=True),
        sa.Column("stints_json", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["driver_id"], ["driver.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "driver_id"),
    )
    with op.batch_alter_table("driver_session_detail") as batch_op:
        batch_op.create_index("ix_driver_session_detail_session_id", ["session_id"])
        batch_op.create_index("ix_driver_session_detail_driver_id", ["driver_id"])


def downgrade() -> None:
    op.drop_table("driver_session_detail")
