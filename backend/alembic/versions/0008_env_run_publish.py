"""env files, run state, publish state

Adds runtime and publish columns to projects, plus the env file aggregate:
project_env_files (named dotenv files, path relative to clone_path) and
project_env_vars (values are Fernet ciphertext, never stored plaintext).
Partial unique indexes keep domains and internal ports unique per server
without constraining the NULL (unpublished) majority.

Revision ID: 0008_env_run_publish
Revises: 0007_nginx_installed
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_env_run_publish"
down_revision: Union[str, None] = "0007_nginx_installed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "runtime_status",
            sa.String(),
            server_default=sa.text("'never_started'"),
            nullable=False,
        ),
    )
    op.add_column(
        "projects", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("projects", sa.Column("compose_file_path", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("domain", sa.String(), nullable=True))
    op.add_column("projects", sa.Column("internal_port", sa.Integer(), nullable=True))
    op.add_column(
        "projects", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_projects_runtime_status",
        "projects",
        "runtime_status IN ('never_started', 'running', 'failed')",
    )
    op.create_index(
        "uq_projects_server_id_domain",
        "projects",
        ["server_id", "domain"],
        unique=True,
        postgresql_where=sa.text("domain IS NOT NULL"),
    )
    op.create_index(
        "uq_projects_server_id_internal_port",
        "projects",
        ["server_id", "internal_port"],
        unique=True,
        postgresql_where=sa.text("internal_port IS NOT NULL"),
    )

    op.create_table(
        "project_env_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "path", name="uq_project_env_files_project_id_path"
        ),
    )
    op.create_index(
        "ix_project_env_files_project_id", "project_env_files", ["project_id"]
    )

    op.create_table(
        "project_env_vars",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("env_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["env_file_id"], ["project_env_files.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "env_file_id", "key", name="uq_project_env_vars_env_file_id_key"
        ),
    )
    op.create_index(
        "ix_project_env_vars_env_file_id", "project_env_vars", ["env_file_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_env_vars_env_file_id", table_name="project_env_vars")
    op.drop_table("project_env_vars")
    op.drop_index("ix_project_env_files_project_id", table_name="project_env_files")
    op.drop_table("project_env_files")
    op.drop_index("uq_projects_server_id_internal_port", table_name="projects")
    op.drop_index("uq_projects_server_id_domain", table_name="projects")
    op.drop_constraint("ck_projects_runtime_status", "projects", type_="check")
    op.drop_column("projects", "published_at")
    op.drop_column("projects", "internal_port")
    op.drop_column("projects", "domain")
    op.drop_column("projects", "compose_file_path")
    op.drop_column("projects", "started_at")
    op.drop_column("projects", "runtime_status")
