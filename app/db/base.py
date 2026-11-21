"""Declarative base + shared mixins for all SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base. All models inherit from this."""

    pass


class SoftDeleteMixin:
    """Adds a reversible soft-delete flag to a model.

    A row is "deleted" when ``deleted_at`` is set; it is never physically
    removed. A global session filter (see ``app/db/session.py``) excludes
    soft-deleted rows from every ORM read, unless the statement opts in with
    ``.execution_options(include_deleted=True)`` (used by restore / admin /
    reactivate-on-re-add paths).

    Only user-facing *data* entities carry this (domains, tracked_competitors,
    saved_keywords, tracked_keywords, categories). Internal housekeeping deletes
    and account/credential unlinks stay hard deletes.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True, default=None
    )

    @hybrid_property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @is_deleted.expression  # type: ignore[no-redef]
    def is_deleted(cls):  # noqa: N805
        return cls.deleted_at.is_not(None)
