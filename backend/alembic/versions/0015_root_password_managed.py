"""server root_password_is_managed flag

Adds servers.root_password_is_managed. Set true when Abstract set the current root
password itself via a provider-forced password change (the box's root password is a
generated value the user does not know). Server deletion reads it to decide whether to
reset the root password to a fresh, revealed-once value before re-enabling password
login, so the user is not handed back a box whose password nobody holds. Defaults false.

Revision ID: 0015_root_password_managed
Revises: 0014_reregistration_state
Create Date: 2026-08-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_root_password_managed"
down_revision: Union[str, None] = "0014_reregistration_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "root_password_is_managed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "root_password_is_managed")
