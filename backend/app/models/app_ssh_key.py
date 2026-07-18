"""App SSH key model.

One app managed keypair per server, generated fresh when the server is registered.
The unique constraint on server_id enforces exactly one key per server. Scoping the
key to a single server limits the blast radius of a key compromise: it only exposes
the one server it was installed on, never the user's other servers.

Future implementer note (server deletion, not implemented yet): the app public key
line must be removed from the VPS ~/.ssh/authorized_keys BEFORE the server row is
deleted. The FK cascade drops this key row, and the private key is needed to
authenticate the SSH session that performs the removal. The removal must be surgical
(grep -vF on the base64 blob, the middle field of the ssh-ed25519 line) and must
NEVER overwrite the whole authorized_keys file, since the user has their own keys in
it and a full overwrite would lock them out.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.server import Server


class AppSshKey(Base):
    __tablename__ = "app_ssh_keys"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    server_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
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

    server: Mapped["Server"] = relationship(back_populates="app_ssh_key")
