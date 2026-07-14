"""project is_deleting flag

Adds a boolean is_deleting flag to projects. Set true for the duration of a
delete so concurrent mutations are rejected (409) while the ordered teardown of
external state runs without a DB transaction held open. Cleared again if a
deletion step fails so the user can retry; the row is hard-deleted on success.

Revision ID: 0009_project_is_deleting
Revises: 0008_env_run_publish
Create Date: 2026-07-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_project_is_deleting"
down_revision: Union[str, None] = "0008_env_run_publish"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "is_deleting",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "is_deleting")
