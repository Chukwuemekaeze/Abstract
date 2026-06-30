"""add hardening state columns to servers

Revision ID: 0004_hardening_state
Revises: 0003_drop_rls
Create Date: 2026-06-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_hardening_state"
down_revision: Union[str, None] = "0003_drop_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The non-root sudo user created on the box, if any. Once set, the app operates
    # as this user instead of root.
    op.add_column("servers", sa.Column("sudo_user_name", sa.Text(), nullable=True))
    # Hardening flags. Booleans default to false so existing rows are correct.
    op.add_column(
        "servers",
        sa.Column(
            "root_login_disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "servers",
        sa.Column(
            "firewall_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "servers",
        sa.Column(
            "docker_installed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "servers",
        sa.Column(
            "swap_configured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Timestamp of the last successful system update. Useful recurring signal for
    # how stale the box's packages are.
    op.add_column(
        "servers",
        sa.Column("last_system_update_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servers", "last_system_update_at")
    op.drop_column("servers", "swap_configured")
    op.drop_column("servers", "docker_installed")
    op.drop_column("servers", "firewall_enabled")
    op.drop_column("servers", "root_login_disabled")
    op.drop_column("servers", "sudo_user_name")
