"""Database engine and session management."""

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria

from app.core.config import settings
from app.db.base import SoftDeleteMixin

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


@event.listens_for(Session, "do_orm_execute")
def _filter_soft_deleted(orm_execute_state) -> None:
    """Globally exclude soft-deleted rows from every ORM read.

    Applies ``deleted_at IS NULL`` to any SELECT touching a ``SoftDeleteMixin``
    model (including joined/related loads), so lists, dropdowns, the
    domain-resolution gate, and ORM aggregations never see deleted rows.

    Opt out per-statement with ``.execution_options(include_deleted=True)`` —
    used by restore, reactivate-on-re-add, and admin/history reads. Writes
    (UPDATE/DELETE) are untouched so soft-delete and restore can target the row.
    """
    if (
        not orm_execute_state.is_select
        or orm_execute_state.is_column_load
        or orm_execute_state.is_relationship_load
        or orm_execute_state.execution_options.get("include_deleted", False)
    ):
        return
    orm_execute_state.statement = orm_execute_state.statement.options(
        with_loader_criteria(
            SoftDeleteMixin,
            lambda cls: cls.deleted_at.is_(None),
            include_aliases=True,
        )
    )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
