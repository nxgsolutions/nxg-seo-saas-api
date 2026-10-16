"""Category CRUD + startup seed.

Categories are workspace-global, editable industry labels. Seeding is idempotent
so a data wipe can never leave the picker empty.
"""

import re

from sqlalchemy import select
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
    existing = {row[0] for row in db.execute(select(Category.slug)).all()}
    inserted = 0
    for name in DEFAULT_CATEGORIES:
        slug = slugify(name)
        if slug in existing:
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
    category = Category(name=name, slug=slugify(name))
    db.add(category)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CategoryAlreadyExistsError(name) from exc
    db.refresh(category)
    return category


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
    # FK is ON DELETE SET NULL, so domains/keywords keep working, uncategorized.
    db.delete(category)
    db.commit()
