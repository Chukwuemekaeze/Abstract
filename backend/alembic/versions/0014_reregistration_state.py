"""password-based re-registration state

Adds the columns the unified password-based re-registration flow needs:

  - servers.reregistration_state: persisted state machine so a retried /complete
    resumes rather than restarts. Constrained to the known states, defaults 'none'.
  - servers.pending_host_key / pending_host_key_type / pending_fingerprint_sha256:
    the rebuilt host key captured at probe, held apart from the trusted host_key
    until a fresh key-based smoke test promotes it.
  - servers.bootstrap_password: Fernet-encrypted replacement root password used only
    during an in-flight forced change, written ahead and cleared at the end.
  - app_ssh_keys.is_active: false while a freshly generated re-registration keypair is
    pending verification, true once promoted. Existing keys default active.

All additive and reversible; no data is truncated.

Revision ID: 0014_reregistration_state
Revises: 0013_server_key_installed
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_reregistration_state"
down_revision: Union[str, None] = "0013_server_key_installed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "pending_host_key", sa.LargeBinary(), nullable=True
        ),
    )
    op.add_column(
        "servers",
        sa.Column("pending_host_key_type", sa.String(), nullable=True),
    )
    op.add_column(
        "servers",
        sa.Column("pending_fingerprint_sha256", sa.String(), nullable=True),
    )
    op.add_column(
        "servers",
        sa.Column(
            "reregistration_state",
            sa.String(),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
    )
    op.add_column(
        "servers",
        sa.Column("bootstrap_password", sa.LargeBinary(), nullable=True),
    )
    op.create_check_constraint(
        "ck_servers_reregistration_state",
        "servers",
        "reregistration_state in ('none', 'awaiting_confirmation', 'probing', "
        "'exchanging', 'verifying', 'installing_key', 'done')",
    )
    op.add_column(
        "app_ssh_keys",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("app_ssh_keys", "is_active")
    op.drop_constraint(
        "ck_servers_reregistration_state", "servers", type_="check"
    )
    op.drop_column("servers", "bootstrap_password")
    op.drop_column("servers", "reregistration_state")
    op.drop_column("servers", "pending_fingerprint_sha256")
    op.drop_column("servers", "pending_host_key_type")
    op.drop_column("servers", "pending_host_key")
