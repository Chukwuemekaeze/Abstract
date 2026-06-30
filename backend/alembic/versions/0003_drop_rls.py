"""drop row level security from all tables

An earlier, out-of-repo migration (0003_enable_rls) had enabled and FORCED row
level security on users, servers, and app_ssh_keys with owner-isolation policies
keyed on current_setting('app.clerk_user_id'). That approach was abandoned: the
backend is the sole database owner and the single source of access truth, scoping
every query by current_user.id in Python (see app/routes and the _from_client
convention), so forced RLS keyed on a session variable the app does not set is pure
downside. This migration removes it.

Idempotent: it uses IF EXISTS / unconditional DISABLE so it is a harmless no-op on a
fresh database that never had RLS, and a real cleanup on the Neon database that does.

Revision ID: 0003_drop_rls
Revises: 0002_clerk_user_id
Create Date: 2026-06-30
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_drop_rls"
down_revision: Union[str, None] = "0002_clerk_user_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("users", "servers", "app_ssh_keys")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_owner_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Intentionally not reversible. Row level security was deliberately removed and is
    # not part of the application's security model, so there is nothing to restore.
    pass
