"""Unit tests for _resolve_serp_target — unwrapping Google-redirect SERP items.

Pure, no network. Fixtures mirror the shape of real DataForSEO organic items
(google.com/goto wrapper + breadcrumb / website_name carrying the real target).
"""

from app.services.dataforseo_client import _resolve_serp_target

GOTO = "https://google.com/goto?url=CAESqgEB6zswFWV6vfVAFoHU4zt6jvXQ"


def _wrapped(**over) -> dict:
    item = {"url": GOTO, "domain": "google.com", "breadcrumb": None, "website_name": None}
    item.update(over)
    return item


def test_full_breadcrumb_rebuilds_host_and_path():
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="https://theadvitya.com › collections › durga-idols")
    )
    assert domain == "theadvitya.com"
    assert url == "https://theadvitya.com/collections/durga-idols"


def test_breadcrumb_strips_www_in_host():
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="https://www.exoticindiaart.com › sculptures › hindu")
    )
    assert domain == "exoticindiaart.com"
    assert url == "https://exoticindiaart.com/sculptures/hindu"


def test_truncated_crumb_stops_the_path():
    # A truncated crumb ("d…") ends the path; the host is still exact.
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="https://www.exoticindiaart.com › sculptures › hindu › d…")
    )
    assert domain == "exoticindiaart.com"
    assert url == "https://exoticindiaart.com/sculptures/hindu"


def test_crumb_with_spaces_is_slugified():
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="https://www.gettyimages.in › Stock photos")
    )
    assert domain == "gettyimages.in"
    assert url == "https://gettyimages.in/stock-photos"


def test_host_only_breadcrumb_uses_homepage():
    url, domain = _resolve_serp_target(_wrapped(breadcrumb="https://giri.in"))
    assert domain == "giri.in"
    assert url == "https://giri.in/"


def test_bare_domain_website_name_fallback():
    # No URL breadcrumb, but website_name is itself a domain.
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="150+ likes · 1 year ago", website_name="artycraftz.com")
    )
    assert domain == "artycraftz.com"
    assert url == "https://artycraftz.com/"


def test_platform_website_name_maps_to_host():
    # Social/video results: engagement-text breadcrumb, platform in website_name.
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="1.3Cr+ followers", website_name="YouTube  ›  The Viral Fever")
    )
    assert domain == "youtube.com"
    assert url == "https://youtube.com/"


def test_unresolvable_stays_wrapped():
    # Non-URL breadcrumb and an unknown brand website_name → unchanged.
    url, domain = _resolve_serp_target(
        _wrapped(breadcrumb="150+ likes · 1 year ago", website_name="Some Shop")
    )
    assert domain == "google.com"
    assert url == GOTO


def test_normal_organic_passes_through():
    item = {
        "url": "https://nestasia.in/products/buddha-statue",
        "domain": "nestasia.in",
        "breadcrumb": "https://nestasia.in › products",
        "website_name": "Nestasia",
    }
    url, domain = _resolve_serp_target(item)
    assert domain == "nestasia.in"
    assert url == "https://nestasia.in/products/buddha-statue"  # untouched


def test_passthrough_normalizes_www_host():
    url, domain = _resolve_serp_target(
        {"url": "https://www.buddho.org/statues", "domain": "www.buddho.org"}
    )
    assert domain == "buddho.org"
    assert url == "https://www.buddho.org/statues"
