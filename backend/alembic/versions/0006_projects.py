"""projects and project_deploy_keys

A project is a GitHub repo cloned onto a verified, hardened server with a
per-project deploy key. github_repo_id (GitHub's numeric ID) is the canonical
repo identity for dedup; the full name is kept for shell commands and display.
project_deploy_keys is UNIQUE(project_id) for v1: one active key per project,
double-inserts fail loudly. If key rotation lands later, drop that constraint
and add is_active.

Revision ID: 0006_projects
Revises: 0005_base_packages_installed
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_projects"
down_revision: Union[str, None] = "0005_base_packages_installed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("github_repo_full_name", sa.String(), nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("clone_path", sa.String(), nullable=False),
        sa.Column("cloned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "slug", name="uq_projects_user_id_slug"),
        sa.UniqueConstraint(
            "server_id", "github_repo_id", name="uq_projects_server_id_github_repo_id"
        ),
    )
    op.create_index(
        "ix_projects_user_id_created_at",
        "projects",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_projects_server_id", "projects", ["server_id"])

    op.create_table(
        "project_deploy_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_deploy_key_id", sa.BigInteger(), nullable=False),
        sa.Column("deploy_key_public_key", sa.String(), nullable=False),
        sa.Column(
            "encrypted_deploy_key_private_key", sa.LargeBinary(), nullable=False
        ),
        sa.Column("deploy_key_fingerprint", sa.String(), nullable=False),
        sa.Column(
            "key_type",
            sa.String(),
            server_default=sa.text("'ssh-ed25519'"),
            nullable=False,
        ),
        sa.Column(
            "encryption_key_id",
            sa.String(),
            server_default=sa.text("'env-v1'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_project_deploy_keys_project_id"),
    )


def downgrade() -> None:
    op.drop_table("project_deploy_keys")
    op.drop_index("ix_projects_server_id", table_name="projects")
    op.drop_index("ix_projects_user_id_created_at", table_name="projects")
    op.drop_table("projects")
