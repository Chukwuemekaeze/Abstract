"""add nginx_installed to servers

Tracks the install_nginx hardening operation. Nginx runs as a system service and
will route HTTPS traffic to per-project services in a future feature.

Revision ID: 0007_nginx_installed
Revises: 0006_projects
Create Date: 2026-07-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_nginx_installed"
down_revision: Union[str, None] = "0006_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "nginx_installed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "nginx_installed")
