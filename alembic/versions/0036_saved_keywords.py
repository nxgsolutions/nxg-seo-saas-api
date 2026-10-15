"""saved_keywords — the "My Keywords" list per website

A user's saved keywords for one website ("+" menu → Save to My Keywords).
Private per website; metrics are joined from the shared keyword_metrics when
the list is read (not copied).

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE saved_keywords (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
            keyword varchar(200) NOT NULL,
            normalized_keyword varchar(200) NOT NULL,
            country_code varchar(8) NOT NULL DEFAULT 'in',
            language_code varchar(10) NOT NULL DEFAULT 'en',
            source varchar(40),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_saved_keyword UNIQUE (domain_id, normalized_keyword, country_code)
        );
        CREATE INDEX ix_saved_keywords_domain_id ON saved_keywords (domain_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS saved_keywords")
