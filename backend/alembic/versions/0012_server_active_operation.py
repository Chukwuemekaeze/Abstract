"""server operation lock

Adds servers.active_operation, the server-level counterpart to the per-project
lock added in 0010. Server deletion sets it to 'deleting' before it starts
tearing down projects and the VPS, so concurrent server mutations are rejected
409. Null when the server is idle.

Revision ID: 0012_server_active_operation
Revises: 0011_app_ssh_key_per_server
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_server_active_operation"
down_revision: Union[str, None] = "0011_app_ssh_key_per_server"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("active_operation", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servers", "active_operation")
