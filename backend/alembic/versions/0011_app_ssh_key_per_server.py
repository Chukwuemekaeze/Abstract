"""app ssh keys become one per server instead of one per user

Blast radius isolation: each server gets its own freshly generated keypair, so a
compromised key only exposes the single server it was installed on. This is a
breaking schema change with no data migration. Abstract has no real users beyond
the developer, so all server, project, and key state is truncated. Users are
Clerk-mirrored and deliberately preserved to avoid a needless re-authentication.

Revision ID: 0011_app_ssh_key_per_server
Revises: 0010_project_runs
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_app_ssh_key_per_server"
down_revision: Union[str, None] = "0010_project_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Wipe all downstream state. CASCADE makes order irrelevant, but the tables are
    # listed explicitly for clarity. users is intentionally omitted.
    op.execute(
        "TRUNCATE TABLE "
        "project_runs, project_env_vars, project_env_files, project_deploy_keys, "
        "projects, app_ssh_keys, servers "
        "CASCADE"
    )

    op.drop_constraint(
        "app_ssh_keys_user_id_fkey", "app_ssh_keys", type_="foreignkey"
    )
    op.drop_column("app_ssh_keys", "user_id")

    op.add_column(
        "app_ssh_keys",
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_foreign_key(
        "app_ssh_keys_server_id_fkey",
        "app_ssh_keys",
        "servers",
        ["server_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_app_ssh_keys_server_id", "app_ssh_keys", ["server_id"]
    )


def downgrade() -> None:
    # Schema only. The upgrade truncated all key rows, so downgrade cannot and does
    # not restore any data. It merely reverts the columns to the per-user shape.
    op.drop_constraint(
        "uq_app_ssh_keys_server_id", "app_ssh_keys", type_="unique"
    )
    op.drop_constraint(
        "app_ssh_keys_server_id_fkey", "app_ssh_keys", type_="foreignkey"
    )
    op.drop_column("app_ssh_keys", "server_id")

    op.add_column(
        "app_ssh_keys",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_foreign_key(
        "app_ssh_keys_user_id_fkey",
        "app_ssh_keys",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
