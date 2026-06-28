"""add clerk_user_id to users and drop the dev user stub

Revision ID: 0002_clerk_user_id
Revises: 0001_initial
Create Date: 2026-06-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_clerk_user_id"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add the column nullable so existing rows survive the first step.
    op.add_column("users", sa.Column("clerk_user_id", sa.Text(), nullable=True))

    # 2. Remove the seeded dev user stub. Any row without a Clerk id predates real
    #    auth and has no way to be claimed by a Clerk user, so it is dropped.
    op.execute("DELETE FROM users WHERE clerk_user_id IS NULL")

    # 3. Now that no nulls remain, make the column required.
    op.alter_column("users", "clerk_user_id", nullable=False)

    # 4. Unique index. It both enforces uniqueness and serves the auth lookup.
    op.create_index(
        "ix_users_clerk_user_id", "users", ["clerk_user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_users_clerk_user_id", table_name="users")
    op.drop_column("users", "clerk_user_id")
