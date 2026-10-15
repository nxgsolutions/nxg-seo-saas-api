"""drop DataForSEO provider tables (provider_cache, keyword_rank_history)

Removes the paid DataForSEO provider subsystem. The first-party keyword schema
(migration 0013) and manual rank tracking (0006) are unaffected. Downgrade
recreates both tables as they were in 0009 / 0011 (structure only — the cached
provider payloads and rank points are not restored).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS keyword_rank_history;")
    op.execute("DROP TABLE IF EXISTS provider_cache;")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_cache (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cache_key  VARCHAR(400) NOT NULL,
            payload    JSONB NOT NULL,
            source     VARCHAR(50) NOT NULL DEFAULT 'dataforseo',
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_cache_key UNIQUE (cache_key)
        );
        CREATE INDEX ix_provider_cache_cache_key ON provider_cache (cache_key);

        CREATE TABLE keyword_rank_history (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            keyword    VARCHAR(200) NOT NULL,
            location   VARCHAR(100) NOT NULL,
            domain     VARCHAR(255) NOT NULL,
            position   INTEGER,
            checked_on DATE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_rank_point UNIQUE (keyword, location, domain, checked_on)
        );
        CREATE INDEX ix_keyword_rank_history_domain ON keyword_rank_history (domain);
        CREATE INDEX ix_keyword_rank_history_keyword ON keyword_rank_history (keyword);
        """
    )
