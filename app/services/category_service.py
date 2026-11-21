"""Category CRUD + startup seed.

Categories are workspace-global, editable industry labels. Seeding is idempotent
so a data wipe can never leave the picker empty.
"""

import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.category import Category

DEFAULT_CATEGORIES: list[str] = [
    "Education",
    "Technology",
    "Business",
    "Health",
    "Finance",
    "Travel",
    "Ecommerce",
    "Other",
]


class CategoryAlreadyExistsError(Exception):
    """A category with the same name/slug already exists."""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "category"


def seed_default_categories(db: Session) -> int:
    """Insert any missing default categories. Idempotent — safe on startup.

    Returns the number inserted. Matched by slug, so existing rows are untouched.
    """
    # Include soft-deleted rows so a default that was removed is reactivated
    # rather than re-inserted (which would trip uq_categories_slug) — keeping the
    # picker's defaults always present.
    existing = {
        c.slug: c
        for c in db.execute(
            select(Category).execution_options(include_deleted=True)
        ).scalars()
    }
    inserted = 0
    for name in DEFAULT_CATEGORIES:
        slug = slugify(name)
        current = existing.get(slug)
        if current is not None:
            if current.deleted_at is not None:
                current.deleted_at = None  # restore a removed default
                inserted += 1
            continue
        db.add(Category(name=name, slug=slug))
        inserted += 1
    if inserted:
        db.commit()
    return inserted


def list_categories(db: Session) -> list[Category]:
    return list(
        db.execute(select(Category).order_by(Category.name.asc())).scalars()
    )


def get_category(db: Session, category_id: str) -> Category | None:
    return db.get(Category, category_id)


def create_category(db: Session, name: str) -> Category:
    name = name.strip()
    slug = slugify(name)
    # Reactivate a soft-deleted category with the same slug instead of hitting
    # uq_categories_slug; its domain/keyword assignments survived (SET NULL only
    # fires on hard delete, which no longer happens).
    existing = db.execute(
        select(Category)
        .where(Category.slug == slug)
        .execution_options(include_deleted=True)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is None:
            raise CategoryAlreadyExistsError(name)
        existing.deleted_at = None
        existing.name = name
        db.commit()
        db.refresh(existing)
        return existing
    category = Category(name=name, slug=slug)
    db.add(category)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CategoryAlreadyExistsError(name) from exc
    db.refresh(category)
    return category


def get_or_create_category(db: Session, name: str) -> Category:
    """Return the category matching `name` (by slug), creating it if absent.

    Used by auto-detection: the model's label maps to an existing category when
    one shares its slug, otherwise a new category is created and returned. Match
    is on `slug` (so "Real Estate"/"real estate" collapse to the same row).
    Race-safe: a concurrent insert that trips `uq_categories_slug` is recovered
    by re-selecting. Raises ValueError on an empty name."""
    name = (name or "").strip()[:100].strip()
    if not name:
        raise ValueError("Category name is required.")
    slug = slugify(name)
    existing = db.execute(
        select(Category).where(Category.slug == slug)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    try:
        return create_category(db, name)
    except CategoryAlreadyExistsError:
        found = db.execute(
            select(Category).where(Category.slug == slug)
        ).scalar_one_or_none()
        if found is not None:
            return found
        raise


def update_category(db: Session, category: Category, *, name: str) -> Category:
    name = name.strip()
    category.name = name
    category.slug = slugify(name)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CategoryAlreadyExistsError(name) from exc
    db.refresh(category)
    return category


def delete_category(db: Session, category: Category) -> None:
    """Soft-delete a category. Domains/keywords keep their category_id (the row is
    preserved); the category just disappears from lists and the picker. Restorable."""
    category.deleted_at = func.now()
    db.commit()


def restore_category(db: Session, category_id: str) -> Category | None:
    """Restore a soft-deleted category. Returns the row, or None if not found or
    not deleted."""
    category = db.get(
        Category, category_id, execution_options={"include_deleted": True}
    )
    if category is None or category.deleted_at is None:
        return None
    category.deleted_at = None
    db.commit()
    db.refresh(category)
    return category
