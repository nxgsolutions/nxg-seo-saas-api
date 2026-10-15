"""AI overviews — a 3-4 sentence plain-language summary of a page / section.

How a summary is made (never a paid SEO-data call; the AI rewrite is a tiny gpt-4o-mini call):
  1. Facts: a compact dict computed from data we already store (a keyword
     search, a keyword's metrics / SERP, the action plan).
  2. Rules: a deterministic draft written from those facts. It is always
     correct and is the fallback.
  3. AI insight (optional): OpenAI gpt-4o-mini (settings.AI_TEXT_MODEL; the
     local Ollama model as a fallback) turns the facts into 3-5 strategic
     takeaways — what to prioritise, trade-offs, next steps — NOT a restatement
     of the numbers already on the page. Its reply is rejected, and the draft is
     used instead, if it contains any number that isn't in the facts, so the AI
     can't invent data.
  4. Cache: stored in `ai_overviews` per (workspace, scope, key) with a hash of
     the facts; regenerated only when the facts change or on refresh.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.ai_overview import AiOverview
from app.models.workspace import Workspace
from app.services import openai_ai

SCOPES = ("keyword_search", "action_plan")
# Bump when the facts/rules/prompt change so cached summaries regenerate.
VERSION = "5"  # 5: strategic takeaways, not a restatement of on-page numbers
AI_TIMEOUT_S = 60.0
RULES_RETRY = timedelta(hours=6)
EASY_KD = 35

CONTENT_FOR_INTENT = {
    "informational": "guides, notes and practice / PYQ pages fit best",
    "commercial": "comparison and “best …” pages fit best",
    "transactional": "course, sign-up and download pages fit best",
    "navigational": "people want a specific site, so these are hard to win unless it's your brand",
}

SYSTEM_PROMPT = (
    "You are a senior SEO strategist advising expert."
    "The reader can already SEE the raw numbers, keyword list and table on the page, "
    "so NEVER just restate them — a bullet that only repeats an on-screen number is useless. "
    "Each bullet must add judgement they cannot read off the table: what to prioritise and why, "
    "a risk or trade-off, an opportunity the data implies, or the concrete next step to take. "
    "Write 3 to 5 bullets, each one a standalone takeaway, under 18 words, plain words, no jargon, "
    "no markdown, no leading dash or bullet character. "
    "You may cite a number ONLY to justify a recommendation, and ONLY numbers that appear in the facts; "
    "keep each number exactly as written next to the same unit words (searches a month, visits, "
    "difficulty, authority, per click, rank #). Never turn searches into visits or visits into searches. "
    "Never invent numbers, names or claims that are not in the facts. "
    'Reply with JSON only: {"bullets": ["...", "...", "..."]}'
)


# ── Facts + rule-based drafts ───────────────────────────────────────────────

def _q(s: str) -> str:
    return f"“{s}”"


def _kw(kw: str, vol: int | None, kd: float | None) -> str:
    bits = []
    if vol:
        bits.append(f"{vol:,} searches/mo")
    if kd is not None:
        bits.append(f"difficulty {round(kd)}")
    return f"{_q(kw)} ({', '.join(bits)})" if bits else _q(kw)


def keyword_search(detail: dict) -> tuple[dict, str]:
    """Overview of one keyword search (Keyword Analysis results)."""
    seed = detail.get("seed_keyword") or ""
    rows = []
    for r in detail.get("results") or []:
        m = r.get("metric") or {}
        rows.append((r["keyword"], m.get("search_volume"), m.get("difficulty"), (m.get("intent") or "unknown")))
    n = len(rows)
    if n == 0:
        return {"empty": True, "seed": seed}, f"No keyword ideas were saved for {_q(seed)} yet — run the search to collect them."

    with_vol = sorted([r for r in rows if r[1]], key=lambda r: -r[1])
    zero = n - len(with_vol)
    intents: dict[str, int] = {}
    for r in rows:
        if r[3] != "unknown":
            intents[r[3]] = intents.get(r[3], 0) + 1
    top = with_vol[:2]
    easy = [r for r in with_vol if r[2] is not None and r[2] < EASY_KD and r not in top][:2]
    facts = {
        "seed": seed, "country": detail.get("country_code"), "ideas": n,
        "with_volume": len(with_vol), "total_volume": sum(r[1] for r in with_vol), "zero_volume": zero,
        "top": [[r[0], r[1], None if r[2] is None else round(r[2])] for r in top],
        "easy": [[r[0], r[1], round(r[2])] for r in easy],
        "intents": intents,
    }

    out: list[str] = []
    if not with_vol:
        out.append(f"{_q(seed)} returned {n} keyword ideas, but none have search volume yet — "
                   "connect Google Ads Keyword Planner to see which ones people actually search.")
    else:
        out.append(f"{_q(seed)} returned {n} keyword ideas; {len(with_vol)} have search volume "
                   f"(about {facts['total_volume']:,} searches a month combined).")
        out.append("Biggest demand: " + " and ".join(_kw(*r[:3]) for r in top) + ".")
        if easy:
            out.append("Easier wins: " + " and ".join(_kw(*r[:3]) for r in easy)
                       + " — real searches with lower difficulty.")
        else:
            out.append(f"Most ideas with volume are difficulty {EASY_KD}+, so start with longer, more specific variants.")
    if intents:
        known = sum(intents.values())
        dom = max(intents, key=intents.get)
        pct = round(intents[dom] / known * 100)
        out.append(f"Intent is mostly {dom} ({pct}%), so {CONTENT_FOR_INTENT.get(dom, 'match the page to that intent')}.")
    elif with_vol and zero >= max(3, n // 4):
        out.append(f"{zero} ideas show zero volume — you can skip those.")
    return facts, "\n".join(out[:4])


def action_plan(plan: dict) -> tuple[dict, str]:
    """Overview of the Opportunity Research action plan."""
    counts = plan.get("counts") or {}
    items = ((plan.get("top") or []) + (plan.get("improve") or []) + (plan.get("gaps") or [])
             + (plan.get("targets") or []))
    if not items:
        return {"empty": True}, ("No actions yet — connect Search Console and add competitors "
                                 "so the plan can find pages to improve and gaps to close.")

    hard = plan.get("hard_kd") or 101

    def first(kind: str, lst: list[dict]) -> dict | None:
        """The plan's own top pick of this kind (priority order), skipping very hard keywords."""
        for i in (plan.get("top") or []) + lst:
            if i.get("type") == kind and (i.get("difficulty") or 0) < hard:
                return i
        return None

    imp = first("improve", plan.get("improve") or [])
    gap = first("gap", plan.get("gaps") or [])
    tgt = first("target", plan.get("targets") or [])
    gain = plan.get("total_gain") or 0
    facts = {"counts": counts, "total_gain": gain, "hard_kd": plan.get("hard_kd"),
             "improve": imp and [imp["keyword"], imp.get("position"), imp.get("gain")],
             "gap": gap and [gap["keyword"], gap.get("competitor"), gap.get("competitor_position"), gap.get("gain")],
             "target": tgt and [tgt["keyword"], tgt.get("volume"), tgt.get("difficulty")]}
    out = [f"Your plan has {counts.get('improve', 0)} pages to improve, {counts.get('gaps', 0)} competitor gaps "
           f"and {counts.get('targets', 0)} new targets; the top 10 actions are worth about +{gain:,} visits a month "
           "if they reach #3."]
    if imp:
        out.append(f"Start with {_q(imp['keyword'])}: you rank #{imp.get('position')}, and reaching #3 adds "
                   f"about +{(imp.get('gain') or 0):,} visits.")
    if gap:
        out.append(f"Biggest competitor gap: {_q(gap['keyword'])} ({gap.get('competitor')} ranks "
                   f"#{gap.get('competitor_position')}, about +{(gap.get('gain') or 0):,} visits).")
    if tgt:
        out.append(f"Then create a page for {_kw(tgt['keyword'], tgt.get('volume'), tgt.get('difficulty'))}.")
    return facts, "\n".join(out[:4])


# ── AI polish with a number guard ───────────────────────────────────────────

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUM.findall(text or ""):
        n = n.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


def _polish(facts: dict, draft: str) -> tuple[str, str] | None:
    """AI rewrite of the draft into bullets → (text, model); None when unavailable
    or unsafe. Bullets are stored newline-joined in the one `summary` column."""
    facts_text = json.dumps(facts, ensure_ascii=False, default=str)
    user = (
        f"Facts (the only data you may use):\n{facts_text}\n\n"
        "Computed highlights (these are ALREADY shown on the page — do not simply repeat them, "
        f"build advice on top of them):\n{draft}\n\n"
        "Write 3-5 strategic takeaways that help the owner decide what to do next."
    )
    try:
        data, model = openai_ai.write_json(
            SYSTEM_PROMPT, user,
            max_tokens=320, temperature=0.45, timeout=AI_TIMEOUT_S)
    except openai_ai.OpenAiError:
        return None
    raw = data.get("bullets")
    if not isinstance(raw, list):
        return None
    bullets = []
    for b in raw:
        b = " ".join(str(b).split()).lstrip("-•*").strip()
        if b:
            bullets.append(b)
    bullets = bullets[:5]
    if not (2 <= len(bullets) <= 5):
        return None
    text = "\n".join(bullets)
    if not (40 <= len(text) <= 700):
        return None
    # Every number in the rewrite must come from the facts or the draft.
    if not _numbers(text) <= (_numbers(facts_text) | _numbers(draft)):
        return None
    return text, model


# ── Cache ───────────────────────────────────────────────────────────────────

def _hash(facts: dict) -> str:
    return hashlib.sha256((VERSION + json.dumps(facts, sort_keys=True, default=str)).encode()).hexdigest()


def get_or_create(db: Session, workspace: Workspace, scope: str, key: str, facts: dict, draft: str,
                  *, refresh: bool = False) -> dict:
    h = _hash(facts)
    row = db.execute(select(AiOverview).where(
        AiOverview.workspace_id == workspace.id, AiOverview.scope == scope, AiOverview.scope_key == key,
    )).scalar_one_or_none()
    empty = bool(facts.get("empty"))
    # Reuse unless the data changed, a refresh was asked for, or it was a
    # rules-only fallback older than RULES_RETRY (the AI may work now — retried
    # at most that often so a page view doesn't call the AI every time).
    fresh_rules = row is not None and row.source == "rules" and         datetime.now(timezone.utc) - row.created_at < RULES_RETRY
    if row is not None and row.facts_hash == h and not refresh and (row.source == "ai" or empty or fresh_rules):
        return _out(row, cached=True)

    polished = None if empty else _polish(facts, draft)
    if polished is None and row is not None and row.facts_hash == h and row.source == "rules" and not refresh:
        return _out(row, cached=True)
    summary, source, model = (polished[0], "ai", polished[1]) if polished else (draft, "rules", None)
    values = {"workspace_id": workspace.id, "scope": scope, "scope_key": key, "facts_hash": h,
              "summary": summary, "source": source, "model": model,
              "created_at": datetime.now(timezone.utc)}
    stmt = insert(AiOverview).values(**values).on_conflict_do_update(
        constraint="uq_ai_overview_scope",
        set_={k: values[k] for k in ("facts_hash", "summary", "source", "model", "created_at")},
    ).returning(AiOverview)
    # populate_existing: the row may already be in the session from the select
    # above; without it SQLAlchemy would hand back the stale in-memory copy.
    row = db.execute(stmt, execution_options={"populate_existing": True}).scalar_one()
    db.commit()
    return _out(row, cached=False)


def _out(row: AiOverview, *, cached: bool) -> dict:
    # `summary` stores the bullets newline-joined; expose them as a list too.
    bullets = [b.strip() for b in (row.summary or "").split("\n") if b.strip()]
    return {"scope": row.scope, "key": row.scope_key, "summary": row.summary, "bullets": bullets,
            "source": row.source, "model": row.model,
            "generated_at": row.created_at.isoformat(), "cached": cached}
