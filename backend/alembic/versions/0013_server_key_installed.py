"""server key_installed flag

Adds servers.key_installed. Set true as soon as Abstract's public key is appended
to the VPS authorized_keys during install_key (including when a later install
sub-step fails), so a subsequent cancel of a still-pending registration knows it
must strip the key off the box before deleting the row. Defaults false.

Revision ID: 0013_server_key_installed
Revises: 0012_server_active_operation
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_server_key_installed"
down_revision: Union[str, None] = "0012_server_active_operation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "key_installed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "key_installed")
