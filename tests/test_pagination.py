"""Server-side pagination: the helper and the paged list endpoints."""

from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.keyword_analysis import KeywordMetric, KeywordResearch, KeywordResearchResult
from app.models.ranked_keyword import DomainRankedKeyword, DomainRankedKeywordRun
from app.services import keyword_analysis_service as kas
from app.services import ranked_keywords_service as rks
from app.services.pagination import PageQuery, paginate

PASSWORD = "SecurePass123"


def test_paginate_sorts_none_last_and_clamps_page():
    rows = [{"v": 3}, {"v": None}, {"v": 9}, {"v": 1}]
    page, info = paginate(rows, page=1, page_size=2, sort_key="v", descending=True)
    assert [r["v"] for r in page] == [9, 3]
    assert info == {"page": 1, "page_size": 2, "total": 4, "total_pages": 2}
    last, info = paginate(rows, page=99, page_size=2, sort_key="v", descending=False)
    assert [r["v"] for r in last] == [9, None] and info["page"] == 2   # None stays last ascending too


def _login(client: TestClient) -> str:
    email = f"pg-{uuid4().hex}@example.com"
    client.post("/api/auth/register", json={"first_name": "P", "last_name": "G", "email": email, "password": PASSWORD})
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return client.get("/api/workspaces").json()[0]["id"]


def test_keyword_search_results_are_paged_filtered_and_sorted(client: TestClient, db_session: Session):
    wid = _login(client)
    seed = kas._upsert_keyword(db_session, f"zzpg seed {uuid4().hex[:6]}")
    research = KeywordResearch(workspace_id=wid, seed_keyword_id=seed.id, kind="keyword",
                               status="completed", country_code="in", language_code="en", result_count=30)
    db_session.add(research)
    db_session.flush()
    for i in range(30):
        kw = kas._upsert_keyword(db_session, f"zzpg idea {uuid4().hex[:6]} n{i}x")
        db_session.add(KeywordResearchResult(research_id=research.id, keyword_id=kw.id, rank=i + 1))
        db_session.add(KeywordMetric(keyword_id=kw.id, country_code="in", language_code="en", search_engine="google",
                                     device="desktop", metric_date=date.today(), search_volume=(i + 1) * 10,
                                     difficulty=20))
    db_session.flush()
    base = f"/api/workspaces/{wid}/keyword-search/researches/{research.id}"

    meta = client.get(base, params={"include_results": "false"}).json()
    assert meta["results"] == [] and meta["result_count"] >= 30

    page2 = client.get(f"{base}/results", params={"page": 2, "page_size": 10, "sort": "search_volume"}).json()
    assert page2["pagination"]["total"] == 30 and page2["pagination"]["total_pages"] == 3
    assert [r["metric"]["search_volume"] for r in page2["rows"]][:2] == [200, 190]   # desc by volume

    found = client.get(f"{base}/results", params={"q": "N7X"}).json()     # case-insensitive
    assert [r["keyword"].split()[-1] for r in found["rows"]] == ["n7x"]

    points = client.get(f"{base}/points").json()
    assert len(points["points"]) == 30 and set(points["points"][0]) >= {"id", "keyword", "volume", "difficulty"}


def test_ranked_keywords_payload_returns_one_page(db_session: Session):
    domain = Domain(workspace_id=None, name="x", domain="pytest-pg-ours.example")
    run = DomainRankedKeywordRun(environment="live", target="pytest-pg-rival.example", location_code=2356,
                                 language_code="en", requested_limit=100, items_count=40, meta={})
    db_session.add(run)
    db_session.flush()
    db_session.add_all([DomainRankedKeyword(run_id=run.id, keyword=f"kw {i}", rank_absolute=i + 1, etv=float(i),
                                            url=f"https://pytest-pg-rival.example/{i % 5}") for i in range(40)])
    db_session.flush()
    domain.id = uuid4()  # not persisted: payload only reads its host + id

    out = rks.payload(db_session, domain, "pytest-pg-rival.example",
                      listing=PageQuery(page=2, page_size=15, sort="position", descending=False, q=None),
                      bucket="all")
    assert len(out["rows"]) == 15 and out["pagination"]["total"] == 40
    assert [r["position"] for r in out["rows"]][:2] == [16, 17]
    assert out["summary"]["top10"] == 10            # summary still covers every row

    top3 = rks.payload(db_session, domain, "pytest-pg-rival.example", bucket="top3")
    assert top3["pagination"]["total"] == 3
