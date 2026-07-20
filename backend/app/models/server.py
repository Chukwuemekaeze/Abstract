"""Server model. A registered VPS the user wants to deploy to."""

from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.app_ssh_key import AppSshKey
    from app.models.project import Project

SERVER_STATUSES = ("pending_verification", "verified", "key_mismatch")


class Server(Base):
    __tablename__ = "servers"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending_verification', 'verified', 'key_mismatch')",
            name="ck_servers_status",
        ),
        Index("ix_servers_user_id_created_at", "user_id", text("created_at desc")),
    )

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
    name: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("22"))
    username: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'root'")
    )

    # Populated once the host key is captured during probe. Sensitive, never serialized.
    host_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    host_key_type: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint_sha256: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, nullable=False)
    password_auth_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    verification_source: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'tofu'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Hardening state. Set by the hardening operations once each one succeeds.
    # sudo_user_name is the non-root user the app switches to; once set, username
    # above is updated to match it.
    sudo_user_name: Mapped[str | None] = mapped_column(String, nullable=True)
    root_login_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    firewall_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    docker_installed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    base_packages_installed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    nginx_installed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    swap_configured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    last_system_update_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The single in-flight server-level operation, currently only 'deleting', or
    # null when idle. Mirrors projects.active_operation: set in a short transaction
    # before server deletion runs external side effects so concurrent server
    # mutations are rejected 409, and the row is gone once deletion completes.
    active_operation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The app managed keypair scoped to this server. Deleting the server cascades to
    # the key row (see AppSshKey for the authorized_keys removal ordering constraint).
    app_ssh_key: Mapped[Optional["AppSshKey"]] = relationship(
        back_populates="server", uselist=False, cascade="all, delete-orphan"
    )
    # Projects hosted on this server. Loaded eagerly by server deletion to iterate
    # per-project teardown. Row removal is handled by the FK ondelete=CASCADE
    # (passive_deletes); by the time the server row is deleted the loop has already
    # removed every project.
    projects: Mapped[list["Project"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )
