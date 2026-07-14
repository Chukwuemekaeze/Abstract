"""Project model. A GitHub repo cloned onto a specific verified, hardened server.

github_repo_id is GitHub's numeric repo ID: the canonical identity for dedup
(UNIQUE with server_id) because it survives repo renames. The full name is kept
for shell commands and display. updated_at is set explicitly on creation and,
once pulls are implemented, on each successful pull; there is deliberately no
onupdate auto-touch.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_projects_user_id_slug"),
        UniqueConstraint(
            "server_id", "github_repo_id", name="uq_projects_server_id_github_repo_id"
        ),
        Index("ix_projects_user_id_created_at", "user_id", text("created_at desc")),
        Index("ix_projects_server_id", "server_id"),
        CheckConstraint(
            "runtime_status IN ('never_started', 'running', 'failed')",
            name="ck_projects_runtime_status",
        ),
        Index(
            "uq_projects_server_id_domain",
            "server_id",
            "domain",
            unique=True,
            postgresql_where=text("domain IS NOT NULL"),
        ),
        Index(
            "uq_projects_server_id_internal_port",
            "server_id",
            "internal_port",
            unique=True,
            postgresql_where=text("internal_port IS NOT NULL"),
        ),
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
    server_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # URL-safe derivation of name; used in VPS file paths (~/.ssh/{slug}-deploy),
    # SSH host aliases (github-{slug}), and unique per user.
    slug: Mapped[str] = mapped_column(String, nullable=False)
    github_repo_full_name: Mapped[str] = mapped_column(String, nullable=False)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    clone_path: Mapped[str] = mapped_column(String, nullable=False)
    cloned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last known docker compose state. 'never_started' until the first start
    # attempt; refresh_status may flip running <-> failed afterwards.
    runtime_status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=text("'never_started'"),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Advanced override for non-default compose file names, relative to clone_path.
    compose_file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    internal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True only for the duration of a delete: set before any external teardown
    # runs (so concurrent mutations are rejected 409) and cleared again if a
    # step fails so the user can retry. The row is hard-deleted on success.
    is_deleting: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
