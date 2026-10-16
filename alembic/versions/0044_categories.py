"""categories — reusable domain category + category-scoped keyword links

A first-class, editable Category concept so competitor suggestions (and the
tracked list / Refresh / auto-seed that share the same query) stay within a
domain's industry instead of pooling every niche in the same market together.

  - categories: the editable list (id, name, slug). Seeded with sensible
    defaults so the picker isn't empty; fully CRUD-able at runtime.
  - domains.category_id: the category a domain belongs to (nullable).
  - domain_keywords.category_id: the category inherited from the domain that
    targeted the keyword, so SERP results can be filtered by category.

Additive + nullable => non-breaking. A domain with no category keeps the old
workspace-wide suggestion behavior (fallback), so nothing changes until a
category is assigned.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-15
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEFAULT_CATEGORIES = [
    ("Education", "education"),
    ("Technology", "technology"),
    ("Business", "business"),
    ("Health", "health"),
    ("Finance", "finance"),
    ("Travel", "travel"),
    ("Ecommerce", "ecommerce"),
    ("Other", "other"),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar(100) NOT NULL,
            slug varchar(100) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_categories_name UNIQUE (name),
            CONSTRAINT uq_categories_slug UNIQUE (slug)
        );
        """
    )
    for name, slug in _DEFAULT_CATEGORIES:
        op.execute(
            f"""
            INSERT INTO categories (name, slug)
            VALUES ('{name}', '{slug}')
            ON CONFLICT (slug) DO NOTHING;
            """
        )
    op.execute(
        """
        ALTER TABLE domains
            ADD COLUMN category_id uuid NULL
                REFERENCES categories(id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS ix_domains_category_id ON domains (category_id);

        ALTER TABLE domain_keywords
            ADD COLUMN category_id uuid NULL
                REFERENCES categories(id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS ix_domain_keywords_category
            ON domain_keywords (category_id, keyword_id, country_code, language_code);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS ix_domain_keywords_category;
        ALTER TABLE domain_keywords DROP COLUMN IF EXISTS category_id;
        DROP INDEX IF EXISTS ix_domains_category_id;
        ALTER TABLE domains DROP COLUMN IF EXISTS category_id;
        DROP TABLE IF EXISTS categories;
        """
    )
