"""per-user kiosk token (read-only wall display at /kiosk)

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Plain ALTER TABLE, not batch mode: rebuilding "user" would cascade-delete everyone's data.
def upgrade() -> None:
    op.add_column("user", sa.Column("kiosk_token", sa.String(length=80), nullable=True))
    op.create_index("ix_user_kiosk_token", "user", ["kiosk_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_kiosk_token", table_name="user")
    op.drop_column("user", "kiosk_token")
