"""project run history and per-project operation lock

Adds the project_runs table (one row per successful or failed run, newest
kept forever) and replaces the boolean projects.is_deleting flag with a single
projects.active_operation text column that serializes start, rollback, publish,
and delete on the same project.

A partial unique index guarantees at most one 'running' row per project, which
enforces the supersede-on-new-run invariant even under concurrent requests.

The old is_deleting flag maps directly onto active_operation='deleting'.

Revision ID: 0010_project_runs
Revises: 0009_project_is_deleting
Create Date: 2026-07-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_project_runs"
down_revision: Union[str, None] = "0009_project_is_deleting"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("git_commit_sha", sa.Text(), nullable=False),
        sa.Column("git_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("build_output", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'superseded', 'failed')",
            name="ck_project_runs_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_runs_project_id_created_at",
        "project_runs",
        ["project_id", sa.text("created_at desc")],
    )
    # At most one running row per project: the supersede invariant, enforced by
    # the database so two concurrent runs cannot both land as 'running'.
    op.create_index(
        "uq_project_runs_one_running",
        "project_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    # -- Replace is_deleting with the general operation lock ------------------
    op.add_column(
        "projects",
        sa.Column("active_operation", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE projects SET active_operation = 'deleting' WHERE is_deleting = true"
    )
    op.drop_column("projects", "is_deleting")


def downgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "is_deleting",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE projects SET is_deleting = true WHERE active_operation = 'deleting'"
    )
    op.drop_column("projects", "active_operation")

    op.drop_index("uq_project_runs_one_running", table_name="project_runs")
    op.drop_index("ix_project_runs_project_id_created_at", table_name="project_runs")
    op.drop_table("project_runs")
