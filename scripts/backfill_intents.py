"""Backfill search intent for keywords the OLD rules left as 'unknown'/NULL.

Re-classifies every keyword that has an unknown/NULL intent on any keyword_metrics
row, in batches, with the business/category-aware AI classifier, and updates all of
that keyword's unknown/NULL rows in place. Idempotent: already-classified rows are
untouched, so it is safe to re-run (e.g. to pick up ones a batch missed).

Provider: --local forces the free local Ollama model (default here). --openai uses
the production order (OpenAI gpt-4o-mini -> local fallback).

Run from backend/ with the venv:
    PYTHONPATH=. .venv/Scripts/python scripts/backfill_intents.py --local
    PYTHONPATH=. .venv/Scripts/python scripts/backfill_intents.py --local --limit 60   # smoke test
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import text

from app.core.logging_config import configure_logging
from app.db.session import SessionLocal
from app.services import intent_service

logger = logging.getLogger("scripts.backfill_intents")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0, help="0 = all; else cap keywords (smoke test)")
    ap.add_argument("--openai", action="store_true", help="use production order (OpenAI first)")
    ap.add_argument("--local", action="store_true", help="force local Ollama only (default)")
    args = ap.parse_args()
    use_local = not args.openai  # default to local unless --openai

    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("app.ai").setLevel(logging.WARNING)

    db = SessionLocal()
    try:
        rows = db.execute(text(
            "SELECT k.id, k.keyword FROM keywords k "
            "WHERE EXISTS (SELECT 1 FROM keyword_metrics m "
            "              WHERE m.keyword_id = k.id AND (m.intent IS NULL OR m.intent = 'unknown')) "
            "ORDER BY k.keyword"
        )).all()
        if args.limit:
            rows = rows[: args.limit]
        total = len(rows)
        logger.info("Backfill: %d keywords to classify (provider=%s, batch=%d)",
                    total, "ollama" if use_local else "openai-first", args.batch_size)

        done = 0
        resolved = 0
        updated_rows = 0
        t0 = time.time()
        for start in range(0, total, args.batch_size):
            chunk = rows[start:start + args.batch_size]
            id_by_norm = {intent_service.keyword_scoring.normalize_keyword(kw): kid for kid, kw in chunk}
            texts = [kw for _kid, kw in chunk]
            result = intent_service.classify_batch(
                texts, use_local=use_local, max_items=len(texts),
            )
            for norm, res in result.items():
                kid = id_by_norm.get(norm)
                if kid is None:
                    continue
                r = db.execute(text(
                    "UPDATE keyword_metrics SET intent = :i "
                    "WHERE keyword_id = :kid AND (intent IS NULL OR intent = 'unknown')"
                ), {"i": res.intent, "kid": kid})
                updated_rows += r.rowcount or 0
                resolved += 1
            db.commit()
            done += len(chunk)
            rate = done / max(1e-6, time.time() - t0)
            logger.info("  %d/%d keywords  (resolved so far %d, metric rows updated %d, %.1f kw/s)",
                        done, total, resolved, updated_rows, rate)

        logger.info("DONE: classified %d/%d keywords, updated %d metric rows in %.0fs",
                    resolved, total, updated_rows, time.time() - t0)
        # Remaining unknowns after the run (truly ambiguous or model-missed).
        remaining = db.execute(text(
            "SELECT count(DISTINCT keyword_id) FROM keyword_metrics "
            "WHERE intent IS NULL OR intent = 'unknown'"
        )).scalar_one()
        logger.info("Keywords still unknown/NULL after backfill: %d", remaining)
    finally:
        db.close()


if __name__ == "__main__":
    main()
