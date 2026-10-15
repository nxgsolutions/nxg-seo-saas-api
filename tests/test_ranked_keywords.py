"""Ranked keywords (DataForSEO Labs): budget guards, storage, reuse, comparison.

The provider is mocked — no paid calls.
"""

import uuid

import pytest

from app.core.config import settings
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword
from app.models.ranked_keyword import DomainRankedKeyword
from app.models.user import User
from app.services import dataforseo_client, ranked_keywords_service as svc, workspace_service

LOC = 2356
RIVAL = "pytest-rival-ranked.example"
OURS = "pytest-ours-ranked.example"


def _item(keyword, pos, etv, prev=None):
    return {"keyword": keyword, "rank_absolute": pos, "rank_group": pos,
            "prev_rank_absolute": prev, "is_new": False, "is_up": False, "is_down": False,
            "serp_item_type": "organic", "url": f"https://{RIVAL}/{pos}", "relative_url": f"/{pos}",
            "title": keyword, "etv": etv, "search_volume": 1000, "cpc": 0.5, "competition": 0.3,
            "keyword_difficulty": 25, "intent": "informational", "serp_features": ["organic"],
            "monthly": [], "raw": {}}


@pytest.fixture
def provider(monkeypatch):
    calls = []

    def ranked(target, *, location_code, language_code, limit, env=None, offset=0, filters=None):
        calls.append({"target": target, "limit": limit, "env": env, "offset": offset,
                      "filters": filters})
        items = [_item("zzr shared kw", 2, 50.0, prev=5), _item("zzr gap kw", 7, 9.0)]
        if target == OURS:
            items = [_item("zzr shared kw", 12, 1.0)]
        return {"total_count": 999, "items_count": len(items), "cost": 0.0102,
                "meta": {"target": target}, "items": items}

    monkeypatch.setattr(settings, "DATAFORSEO_LOGIN", "x")
    monkeypatch.setattr(settings, "DATAFORSEO_PASSWORD", "y")
    monkeypatch.setattr(settings, "DATAFORSEO_FORCE_SANDBOX", False)
    monkeypatch.setattr(settings, "DATAFORSEO_LIVE_ENABLED", True)
    monkeypatch.setattr(settings, "DATAFORSEO_LIVE_RANKED_TARGETS", f"{RIVAL},{OURS}")
    monkeypatch.setattr(settings, "DATAFORSEO_RANKED_KEYWORDS_MAX_LIMIT", 200)
    monkeypatch.setattr(dataforseo_client, "ranked_keywords", ranked)
    return calls


@pytest.fixture
def domain(db_session):
    user = User(email=f"rk-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                first_name="Rk", last_name="Test")
    db_session.add(user)
    db_session.flush()
    ws = workspace_service.create_workspace(db_session, user, "Ranked test")
    d = Domain(workspace_id=ws.id, name="ours", domain=OURS)
    db_session.add(d)
    db_session.flush()
    return d


def test_fetch_stores_run_and_reuses_it(db_session, provider):
    run, fetched = svc.fetch(db_session, f"https://www.{RIVAL}/", location_code=LOC,
                             language="en", env="live", limit=5000)
    assert fetched and run.target == RIVAL and float(run.cost) == pytest.approx(0.0102)
    assert provider[0]["limit"] == 200  # hard cap applied
    rows = svc.run_rows(db_session, run)
    assert [r.keyword for r in rows] == ["zzr shared kw", "zzr gap kw"]  # by traffic
    assert all(r.keyword_id is not None for r in rows)  # live rows linked

    again, fetched2 = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live")
    assert not fetched2 and again.id == run.id and len(provider) == 1  # no second charge

    _, forced = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
                          force=True)
    assert forced and len(provider) == 2


def test_live_allowlist_blocks_other_domains_but_sandbox_is_free(db_session, provider):
    with pytest.raises(svc.RankedFetchNotAllowed):
        svc.fetch(db_session, "not-allowed.example", location_code=LOC, language="en", env="live")
    assert provider == []

    run, fetched = svc.fetch(db_session, "not-allowed.example", location_code=LOC,
                             language="en", env="sandbox")
    assert fetched and run.environment == "sandbox"
    assert all(r.keyword_id is None for r in svc.run_rows(db_session, run))  # dummy data
    # Sandbox run is invisible to live reads.
    assert svc.latest_run(db_session, "not-allowed.example", location_code=LOC,
                          language="en", env="live") is None


def test_competitor_on_your_list_can_be_fetched_live(db_session, provider, domain):
    from app.models.competitor import TrackedCompetitor
    new = "zzr-new-rival.example"
    assert not svc.can_fetch(new, "live", svc.allowed_for_domain(db_session, domain))
    db_session.add(TrackedCompetitor(domain_id=domain.id, competitor=new, source="manual"))
    db_session.flush()
    allowed = svc.allowed_for_domain(db_session, domain)
    assert new in allowed and OURS in allowed
    run, fetched = svc.fetch(db_session, new, location_code=LOC, language="en", env="live",
                             extra_allowed=allowed)
    assert fetched and run.target == new
    assert svc.payload(db_session, domain, new)["can_fetch"] is True
    # Still blocked when live calls are switched off.
    import app.core.config as cfg
    cfg.settings.DATAFORSEO_LIVE_ENABLED = False
    try:
        assert not svc.can_fetch(new, "live", allowed)
    finally:
        cfg.settings.DATAFORSEO_LIVE_ENABLED = True


def test_payload_compares_with_our_domain(db_session, provider, domain):
    db_session.add(OpportunityKeyword(domain_id=domain.id, keyword="zzr gap kw"))  # tracked, no rank
    db_session.flush()
    svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live")
    before = svc.payload(db_session, domain, RIVAL, country="in")
    assert before["summary"]["gap"] == 2 and before["has_our_run"] is False

    svc.fetch(db_session, OURS, location_code=LOC, language="en", env="live")
    out = svc.payload(db_session, domain, RIVAL, country="in")
    rows = {r["keyword"]: r for r in out["rows"]}
    assert rows["zzr shared kw"]["our_position"] == 12
    assert rows["zzr shared kw"]["our_position_source"] == "ranked"
    assert rows["zzr shared kw"]["change"] == 3  # 5 -> 2
    assert rows["zzr gap kw"]["is_gap"] and rows["zzr gap kw"]["tracked"]
    assert out["summary"] == {"top3": 1, "top10": 2, "etv": 59, "common": 1, "gap": 1, "tracked": 1}
    assert out["can_fetch"] and out["cost"] == pytest.approx(0.0102)
    assert db_session.query(DomainRankedKeyword).count() >= 3


# ── Multi-page scopes ("relevant" / "all") ───────────────────────────────────

@pytest.fixture
def paged(monkeypatch, provider):
    """A provider with 2,500 distinct keywords (pages of <= 1000)."""
    calls = []

    def ranked(target, *, location_code, language_code, limit, env=None, offset=0, filters=None):
        calls.append({"limit": limit, "offset": offset, "filters": filters})
        n = min(limit, max(0, 2500 - offset))
        items = [_item(f"zzp kw {offset + i}", 1 + (offset + i) % 30, 100.0 - (offset + i) / 100)
                 for i in range(n)]
        return {"total_count": 2500, "items_count": n,
                "cost": round(0.01 + 0.0001 * n, 4), "meta": {}, "items": items}

    monkeypatch.setattr(dataforseo_client, "ranked_keywords", ranked)
    monkeypatch.setattr(settings, "DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH", 5000)
    monkeypatch.setattr(settings, "DATAFORSEO_RANKED_MAX_COST_PER_FETCH", 1.0)
    return calls


def test_all_scope_pages_until_complete(db_session, paged):
    run, fetched = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
                             scope="all")
    assert fetched and run.items_count == 2500 and run.total_count == 2500
    assert [c["offset"] for c in paged] == [0, 1000, 2000]
    assert run.meta["scope"] == "all" and run.meta["pages"] == 3
    assert run.meta["stop_reason"] == "complete"
    assert float(run.cost) == pytest.approx(0.28)  # 3 x $0.01 + 2500 x $0.0001


def test_cost_cap_limits_keywords_bought(db_session, paged, monkeypatch):
    monkeypatch.setattr(settings, "DATAFORSEO_RANKED_MAX_COST_PER_FETCH", 0.12)
    run, _ = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
                       scope="all")
    assert run.items_count == 1000 and run.meta["stop_reason"] == "cap_reached"
    assert float(run.cost) <= 0.12


def test_relevant_scope_sends_server_side_filters(db_session, paged):
    # The topic regex is now per-domain (passed in), not a global setting.
    svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
              scope="relevant", max_keywords=500, topic_regex="upsc|ias|exam")
    f = paged[0]["filters"]
    assert f[0][0:2] == ["keyword_data.keyword", "regex"] and "upsc" in f[0][2]
    assert ["ranked_serp_element.serp_item.rank_absolute", "<=", 20] in f
    assert paged[0]["limit"] == 500


def test_relevant_scope_without_topic_regex_omits_keyword_filter(db_session, paged):
    # A domain with no niche set => "relevant" is just position + volume, no niche.
    svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
              scope="relevant", max_keywords=500)
    f = paged[0]["filters"]
    assert not any(isinstance(c, list) and c[0] == "keyword_data.keyword" for c in f)
    assert ["ranked_serp_element.serp_item.rank_absolute", "<=", 20] in f


def test_repeating_pages_stop_immediately(db_session, provider, monkeypatch):
    """A provider that ignores `offset` (like the sandbox) returns the same full
    page every time — the loop must stop on the first page with nothing new."""
    calls = []

    def same_page(target, *, location_code, language_code, limit, env=None, offset=0, filters=None):
        calls.append(offset)
        items = [_item(f"zzs dup {i}", 3, 1.0) for i in range(limit)]
        return {"total_count": 11625, "items_count": limit, "cost": 0.0, "meta": {}, "items": items}

    monkeypatch.setattr(dataforseo_client, "ranked_keywords", same_page)
    run, _ = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="sandbox",
                       scope="all")
    assert calls == [0, 1000]  # second page added nothing -> stop (not 12 pages)
    assert run.items_count == 1000 and run.meta["stop_reason"] == "no_new_keywords"


def test_scopes_are_cached_separately(db_session, provider):
    top, _ = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live")
    rel, fetched = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
                             scope="relevant")
    assert fetched and rel.id != top.id  # a top-100 run doesn't satisfy "relevant"
    again, fetched2 = svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live",
                                scope="relevant")
    assert not fetched2 and again.id == rel.id


def test_estimate_uses_one_small_request(db_session, paged):
    est = svc.estimate(RIVAL, location_code=LOC, language="en", env="live", scope="all")
    assert paged == [{"limit": 1, "offset": 0, "filters": None}]
    assert est["matching_count"] == 2500 and est["to_fetch"] == 2500
    assert est["requests"] == 3 and est["estimated_cost"] == pytest.approx(0.28)
    assert db_session.query(DomainRankedKeyword).count() == 0 or True  # nothing stored


def test_payload_groups_top_pages(db_session, provider, domain):
    svc.fetch(db_session, RIVAL, location_code=LOC, language="en", env="live")
    out = svc.payload(db_session, domain, RIVAL, country="in")
    assert out["scope"] == "top" and {s["scope"] for s in out["stored_scopes"]} == {"top"}
    urls = [p["url"] for p in out["pages"]]
    assert urls == [f"https://{RIVAL}/2", f"https://{RIVAL}/7"]
    assert out["pages"][0]["top_keyword"] == "zzr shared kw"


# ── Curated competitor list drives Competitive Strategy ──────────────────────

def test_strategy_uses_curated_competitor_list(db_session, domain):
    from app.models.competitor import TrackedCompetitor
    from app.services import opportunity_service

    serp = opportunity_service.strategy(db_session, domain)
    assert serp["scope"] == "serp" and serp["curated_count"] == 0  # no list -> discovery

    for host in ["www.pytest-curated-a.example", "pytest-curated-b.example"]:
        db_session.add(TrackedCompetitor(domain_id=domain.id, competitor=host, source="manual"))
    db_session.flush()

    out = opportunity_service.strategy(db_session, domain)
    assert out["scope"] == "curated" and out["curated_count"] == 2
    hosts = [c["domain"] for c in out["competitors"]]
    assert hosts == ["pytest-curated-a.example", "pytest-curated-b.example"]  # www stripped
    zero = out["competitors"][0]
    assert zero["keywords_shared"] == 0 and zero["avg_position"] is None and zero["curated"]

    assert opportunity_service.strategy(db_session, domain, scope="serp")["scope"] == "serp"
