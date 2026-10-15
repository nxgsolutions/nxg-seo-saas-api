"""Tests for the page optimization plan builder."""

from app.services import audit_service, optimization_service
from app.services.audit_service import AuditResult, Check


def _fake_audit(*_args, **_kwargs) -> AuditResult:
    return AuditResult(
        url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        load_time_ms=500,
        page_size_bytes=1000,
        score=60,
        checks=[
            Check("status", "Returns HTTP 200", "pass", "ok", 2),
            Check("title", "Title tag", "fail", "No <title> tag found.", 2),
            Check("description", "Meta description length", "warn", "Too short.", 2),
            Check("schema", "Structured data", "warn", "No JSON-LD.", 1),
        ],
        metrics={},
    )


def test_optimize_builds_prioritized_plan(monkeypatch) -> None:
    monkeypatch.setattr(audit_service, "run_audit", _fake_audit)
    plan = optimization_service.optimize("example.com", "desktop")

    assert plan["score"] == 60
    # 3 non-pass checks become recommendations, 1 pass becomes a win.
    assert plan["summary"]["total_issues"] == 3
    assert plan["summary"]["passed"] == 1

    recs = plan["recommendations"]
    # Highest impact first: the failed weight-2 title check.
    assert recs[0]["id"] == "title"
    assert recs[0]["impact"] == "high"
    assert recs[0]["severity"] == "fail"
    assert "title" in recs[0]["fix"].lower()
    # Every recommendation carries category + effort + fix.
    for r in recs:
        assert r["category"]
        assert r["effort"] in ("quick", "moderate", "high")
        assert r["fix"]


def test_impact_levels() -> None:
    assert optimization_service._impact(Check("x", "x", "fail", "", 2)) == "high"
    assert optimization_service._impact(Check("x", "x", "fail", "", 1)) == "medium"
    assert optimization_service._impact(Check("x", "x", "warn", "", 2)) == "medium"
    assert optimization_service._impact(Check("x", "x", "warn", "", 1)) == "low"
