"""'Find competitors again' preview + merge must never overwrite the list.

The re-find flow runs discovery in PREVIEW mode: it returns candidates (flagged
with whether each is already tracked) WITHOUT changing the tracked list, so the
user can confirm which new ones to merge. Confirming calls add_competitors_bulk,
which adds only the new domains and keeps everything already there.

The expensive LLM/scrape step (competitor_discovery_service.discover) is mocked
so the behavior is tested deterministically, with no network / paid calls.
"""

import uuid

from app.models.competitor import TrackedCompetitor
from app.models.domain import Domain
from app.models.user import User
from app.services import competitor_discovery_service as disc
from app.services import competitor_service
from app.services import workspace_service


def _ws(db, label):
    user = User(email=f"{label}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name=label, last_name="Test")
    db.add(user)
    db.flush()
    return workspace_service.create_workspace(db, user, f"{label} ws")


def _domain_with_list(db):
    ws = _ws(db, "refind")
    domain = Domain(workspace_id=ws.id, name="mine", domain="mysite.com")
    db.add(domain)
    db.flush()
    # Start with two curated competitors: one manual, one previously AI-found.
    db.add(TrackedCompetitor(domain_id=domain.id, competitor="rival-a.com", source="manual"))
    db.add(TrackedCompetitor(domain_id=domain.id, competitor="rival-ai.com", source="ai"))
    db.commit()
    return domain


def _mock_discover(monkeypatch, candidates):
    """Mock the LLM/scrape discovery and capture the kwargs it was called with
    (so tests can assert what was passed as `exclude_domains`)."""
    calls: dict = {}

    def fake_discover(target, **kwargs):
        calls["target"] = target
        calls.update(kwargs)
        return {
            "business": {"business_description": "d", "industry": "i"},
            "market": "India",
            "competitors": candidates,
        }
    monkeypatch.setattr(disc, "discover", fake_discover)
    return calls


def test_preview_does_not_change_the_list(db_session, monkeypatch):
    domain = _domain_with_list(db_session)
    _mock_discover(monkeypatch, [
        {"name": "Rival A", "domain": "rival-a.com", "reason": "r",
         "confidence_level": "High", "match_score": 90},          # already tracked
        {"name": "New One", "domain": "newcomp1.com", "reason": "r1",
         "confidence_level": "High", "match_score": 88},
        {"name": "New Two", "domain": "newcomp2.com", "reason": "r2",
         "confidence_level": "Medium", "match_score": 70},
    ])

    result = competitor_service.discover_and_store(db_session, domain, preview=True)

    # The tracked list is untouched (still the original two).
    hosts = {r["competitor"] for r in competitor_service.list_competitors(db_session, domain)["rows"]}
    assert hosts == {"rival-a.com", "rival-ai.com"}

    # Proposals carry every candidate, with already_tracked set correctly, and
    # `count` is the number of NEW ones only.
    proposed = {p["domain"]: p for p in result["proposed"]}
    assert set(proposed) == {"rival-a.com", "newcomp1.com", "newcomp2.com"}
    assert proposed["rival-a.com"]["already_tracked"] is True
    assert proposed["newcomp1.com"]["already_tracked"] is False
    assert proposed["newcomp2.com"]["already_tracked"] is False
    assert result["count"] == 2


def test_preview_passes_existing_list_as_exclusions(db_session, monkeypatch):
    """A preview run tells discovery to exclude the competitors already tracked,
    so the whole limit is spent on genuinely new ones."""
    domain = _domain_with_list(db_session)
    calls = _mock_discover(monkeypatch, [])

    competitor_service.discover_and_store(db_session, domain, preview=True)

    excluded = set(calls.get("exclude_domains") or [])
    assert {"rival-a.com", "rival-ai.com"} <= excluded


def test_preview_with_no_new_returns_empty(db_session, monkeypatch):
    """When discovery genuinely finds nothing new, the preview returns no
    proposals and count 0 (the UI then shows 'No new competitors found')."""
    domain = _domain_with_list(db_session)
    _mock_discover(monkeypatch, [])

    result = competitor_service.discover_and_store(db_session, domain, preview=True)
    assert result["proposed"] == []
    assert result["count"] == 0
    # List unchanged.
    hosts = {r["competitor"] for r in competitor_service.list_competitors(db_session, domain)["rows"]}
    assert hosts == {"rival-a.com", "rival-ai.com"}


def test_bulk_add_merges_and_is_idempotent(db_session):
    domain = _domain_with_list(db_session)

    new = [
        {"domain": "newcomp1.com", "label": "New One", "reason": "r1",
         "confidence": "High", "match_score": 88},
        {"domain": "newcomp2.com", "label": "New Two", "reason": "r2",
         "confidence": "Medium", "match_score": 70},
        {"domain": "rival-a.com"},   # already tracked — must be skipped
    ]

    out = competitor_service.add_competitors_bulk(db_session, domain, new)
    assert out["added"] == 2
    hosts = {r["competitor"] for r in out["rows"]}
    assert hosts == {"rival-a.com", "rival-ai.com", "newcomp1.com", "newcomp2.com"}

    # Re-adding the same set adds nothing (idempotent) and keeps the list intact.
    again = competitor_service.add_competitors_bulk(db_session, domain, new)
    assert again["added"] == 0
    assert again["count"] == 4


def test_list_ordered_by_match_quality(db_session):
    """The list is returned strongest-match-first: confidence tier, then score.
    Rows with no match signal (manual adds) sort last, regardless of add order."""
    ws = _ws(db_session, "order")
    domain = Domain(workspace_id=ws.id, name="mine", domain="mysite.com")
    db_session.add(domain)
    db_session.flush()
    # Added in a deliberately "wrong" order: a manual + a Medium first, a Very
    # High last — so insertion order alone would bury the best match.
    db_session.add(TrackedCompetitor(domain_id=domain.id, competitor="manual.com", source="manual"))
    db_session.add(TrackedCompetitor(domain_id=domain.id, competitor="medium.com", source="ai",
                                     confidence="Medium", match_score=60))
    db_session.add(TrackedCompetitor(domain_id=domain.id, competitor="high.com", source="ai",
                                     confidence="High", match_score=80))
    db_session.add(TrackedCompetitor(domain_id=domain.id, competitor="veryhigh.com", source="ai",
                                     confidence="Very High", match_score=95))
    db_session.commit()

    order = [r["competitor"] for r in competitor_service.list_competitors(db_session, domain)["rows"]]
    assert order == ["veryhigh.com", "high.com", "medium.com", "manual.com"]


def test_manual_add_is_verified_and_scored(db_session, monkeypatch):
    """A manually added competitor is verified against the cached business
    profile and stored with a match score + confidence (so the UI shows a match
    badge instead of 'added by you')."""
    ws = _ws(db_session, "manual")
    domain = Domain(workspace_id=ws.id, name="mine", domain="mysite.com",
                    business_profile={"products": ["brass idols"], "business_type": "products"})
    db_session.add(domain)
    db_session.flush()

    monkeypatch.setattr(
        disc, "verify_single",
        lambda biz, dom, name=None: {
            "match_score": 82, "confidence_level": "High", "reason": "Offers brass idols",
        },
    )

    row = competitor_service.add_competitor(db_session, domain, "rival.com")
    assert row["source"] == "manual"
    assert row["confidence"] == "High"
    assert row["match_score"] == 82
    assert row["reason"] == "Offers brass idols"


def test_manual_add_without_profile_stays_unscored(db_session):
    """No cached business profile → nothing to match against, so the add still
    succeeds but carries no confidence (no network/LLM call is made)."""
    ws = _ws(db_session, "noprofile")
    domain = Domain(workspace_id=ws.id, name="mine", domain="mysite.com")
    db_session.add(domain)
    db_session.flush()

    row = competitor_service.add_competitor(db_session, domain, "rival.com")
    assert row["source"] == "manual"
    assert row["confidence"] is None
    assert row["match_score"] is None


def test_bulk_add_skips_own_domain(db_session):
    domain = _domain_with_list(db_session)
    out = competitor_service.add_competitors_bulk(
        db_session, domain, [{"domain": "mysite.com"}, {"domain": "newcomp9.com"}]
    )
    assert out["added"] == 1
    hosts = {r["competitor"] for r in out["rows"]}
    assert "mysite.com" not in hosts
    assert "newcomp9.com" in hosts
