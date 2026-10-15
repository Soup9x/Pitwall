"""multi-user: users, invites, per-user settings and ownership columns

Existing single-user data (followed drivers, channels, watched flags, queued/sent alerts and
personal settings) is assigned to user 1, the owner account "admin". Its password is set from
PITWALL_ADMIN_PASSWORD on the next start (see pitwall.users.bootstrap).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONAL_KEYS = (
    "timezone",
    "reminder_offsets",
    "routing",
    "quiet_enabled",
    "quiet_start",
    "quiet_end",
    "spoiler_enabled",
    "spoiler_auto_release_hours",
    "odd_start",
    "odd_end",
    "digest_enabled",
    "digest_day",
    "digest_time",
    "digest_types",
    "trivia_recent",
)


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=40), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("ics_token", sa.String(length=80), nullable=True),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("ics_token"),
    )
    op.create_table(
        "invite",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("make_admin", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "user_setting",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "key"),
    )

    user = sa.table(
        "user",
        sa.column("id", sa.Integer),
        sa.column("username", sa.String),
        sa.column("password_hash", sa.String),
        sa.column("is_admin", sa.Boolean),
        sa.column("active", sa.Boolean),
        sa.column("session_version", sa.Integer),
        sa.column("failed_logins", sa.Integer),
        sa.column("created_at", sa.DateTime),
    )
    op.bulk_insert(
        user,
        [
            {
                "id": 1,
                "username": "admin",
                "password_hash": "!",
                "is_admin": True,
                "active": True,
                "session_version": 1,
                "failed_logins": 0,
                "created_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
    )

    # followed_driver and spoiler_state get a user in their primary key: rebuild them.
    op.create_table(
        "followed_driver_new",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["driver_id"], ["driver.id"]),
        sa.PrimaryKeyConstraint("user_id", "driver_id"),
    )
    op.execute(
        "INSERT INTO followed_driver_new (user_id, driver_id) "
        "SELECT 1, driver_id FROM followed_driver"
    )
    op.drop_table("followed_driver")
    op.rename_table("followed_driver_new", "followed_driver")

    op.create_table(
        "spoiler_state_new",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("watched", sa.Boolean(), nullable=False),
        sa.Column("watched_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "event_id"),
    )
    op.execute(
        "INSERT INTO spoiler_state_new (user_id, event_id, watched, watched_at) "
        "SELECT 1, event_id, watched, watched_at FROM spoiler_state"
    )
    op.drop_table("spoiler_state")
    op.rename_table("spoiler_state_new", "spoiler_state")

    with op.batch_alter_table("notification_target") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=False, server_default="1"))
        batch.create_index("ix_notification_target_user_id", ["user_id"])
        batch.create_foreign_key(
            "fk_notification_target_user", "user", ["user_id"], ["id"], ondelete="CASCADE"
        )
    with op.batch_alter_table("outbox") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=False, server_default="1"))
        batch.create_index("ix_outbox_user_id", ["user_id"])
    with op.batch_alter_table("notification_log") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch.create_index("ix_notification_log_user_id", ["user_id"])
    op.execute("UPDATE notification_log SET user_id = 1")

    keys = ", ".join(f"'{k}'" for k in PERSONAL_KEYS)
    op.execute(
        f"INSERT INTO user_setting (user_id, key, value) "
        f"SELECT 1, key, value FROM setting WHERE key IN ({keys})"
    )
    op.execute(f"DELETE FROM setting WHERE key IN ({keys})")


def downgrade() -> None:
    raise NotImplementedError("Downgrading below multi-user support isn't supported")
