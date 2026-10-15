"""Relevance rules used to keep competitor/local keywords out of our list.

Brand terms are now PER-DOMAIN (no global list is applied by default), so these
tests pass the domain's configured brand_terms — here the UPSC seed set — rather
than relying on a hardcoded constant.
"""

from app.services.keyword_relevance import KNOWN_BRAND_TERMS, irrelevance_reason

OWN = "iaspirants.com"
COMPS = ["drishtiias.com", "superkalam.com", "visionias.in"]
BRANDS = KNOWN_BRAND_TERMS  # a UPSC domain would configure these as its brand_terms


def test_competitor_brands_are_flagged():
    for kw in ["drishti ias", "drishti ias hindi", "www drishtiias com hindi", "drishtiias.com",
               "vision ias test series", "byjus upsc", "unacademy upsc online coaching",
               "prepp ias", "superkalam"]:
        assert irrelevance_reason(kw, own_host=OWN, competitor_hosts=COMPS, brand_terms=BRANDS), kw


def test_local_intent_is_flagged():
    assert irrelevance_reason("upsc coaching near me", own_host=OWN, competitor_hosts=COMPS)


def test_local_intent_kept_for_local_business():
    # A local business opts in with is_local_ok -> "near me" is a prime keyword.
    assert irrelevance_reason("dentist near me", own_host="demo-clinic.example",
                              is_local_ok=True) is None


def test_generic_and_own_brand_keywords_are_kept():
    for kw in ["upsc syllabus", "ias salary", "vision document upsc", "aspirants",
               "aspirant ias academy", "upsc online coaching", "insights on economy upsc",
               "next steps after prelims"]:
        assert irrelevance_reason(kw, own_host=OWN, competitor_hosts=COMPS, brand_terms=BRANDS) is None, kw


def test_no_brand_terms_means_no_brand_filtering():
    # A generic domain with no competitor hosts and no brand_terms keeps everything
    # that isn't local — the UPSC brand list is NOT applied globally any more.
    assert irrelevance_reason("byjus upsc", own_host="example.com") is None
