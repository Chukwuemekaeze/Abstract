"""Project deploy key model.

Every deploy key is scoped to exactly one project: one key on GitHub, one
private key file on the VPS, one ~/.ssh/config block. UNIQUE(project_id) is
deliberate for v1 so accidental double-inserts fail loudly; if key rotation is
added later, drop the constraint and add is_active.

github_deploy_key_id is GitHub's numeric key ID, needed to delete the key via
the API later. The encrypted private key is Fernet ciphertext produced by the
existing KeyProvider and is never serialized in responses.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProjectDeployKey(Base):
    __tablename__ = "project_deploy_keys"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_deploy_keys_project_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    github_deploy_key_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deploy_key_public_key: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_deploy_key_private_key: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False
    )
    deploy_key_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    key_type: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'ssh-ed25519'")
    )
    encryption_key_id: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'env-v1'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
