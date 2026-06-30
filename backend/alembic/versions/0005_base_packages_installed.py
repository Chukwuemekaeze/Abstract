"""add base_packages_installed to servers

Gives install_base_packages a persistent flag like the other hardening operations,
so its card reflects that it ran (including as part of Quick harden) after a refetch.

Revision ID: 0005_base_packages_installed
Revises: 0004_hardening_state
Create Date: 2026-06-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_base_packages_installed"
down_revision: Union[str, None] = "0004_hardening_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "base_packages_installed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "base_packages_installed")
