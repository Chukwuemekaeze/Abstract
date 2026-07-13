"""Env file aggregate: named dotenv files per project plus their variables.

path is relative to the project's clone_path (e.g. ".env", "backend/.env").
Variable values are Fernet ciphertext via the KeyProvider and must never be
decrypted for API responses; only the run service decrypts them, just before
writing files to the VPS. updated_at is touched explicitly by the service on
every mutation; there is deliberately no onupdate auto-touch.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProjectEnvFile(Base):
    __tablename__ = "project_env_files"
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_project_env_files_project_id_path"),
        Index("ix_project_env_files_project_id", "project_id"),
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
    path: Mapped[str] = mapped_column(String, nullable=False)
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


class ProjectEnvVar(Base):
    __tablename__ = "project_env_vars"
    __table_args__ = (
        UniqueConstraint("env_file_id", "key", name="uq_project_env_vars_env_file_id_key"),
        Index("ix_project_env_vars_env_file_id", "env_file_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    env_file_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("project_env_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=text("'env-v1'"),
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
