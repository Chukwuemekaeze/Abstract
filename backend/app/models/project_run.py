"""Project run history. One row per Run (start) or rollback attempt.

A row is inserted with status='running' only after the containers verify as up.
Starting a new run supersedes the previous running row (status='superseded').
A failed attempt inserts status='failed' and leaves the previous running row
untouched, so the recorded history always reflects what is actually deployed.

build_output is the captured build transcript, truncated to 200KB like the Run
flow; it is stored so a past run's output can be inspected, and it is never
logged on the backend.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PROJECT_RUN_STATUSES = ("running", "superseded", "failed")


class ProjectRun(Base):
    __tablename__ = "project_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'superseded', 'failed')",
            name="ck_project_runs_status",
        ),
        Index(
            "ix_project_runs_project_id_created_at",
            "project_id",
            text("created_at desc"),
        ),
        # At most one running row per project: enforces the supersede invariant
        # even if two runs commit concurrently.
        Index(
            "uq_project_runs_one_running",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
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
    # The 40-char commit the run built from.
    git_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    # Branch or tag name for display; null when HEAD was detached at run time.
    git_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    build_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
