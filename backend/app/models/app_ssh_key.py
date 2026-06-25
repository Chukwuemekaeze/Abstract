"""App SSH key model.

v1 application logic generates and uses one app managed keypair per user. The schema
deliberately does NOT enforce that with a unique constraint: future versions will
generate a distinct keypair per server for blast radius isolation (a compromised
server must not expose the key used on other servers).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AppSshKey(Base):
    __tablename__ = "app_ssh_keys"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    public_key: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_private_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
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
