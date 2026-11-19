"""HTML sanitising for Shopify content shown in the editor and published back.

Allows normal rich content (headings, lists, tables, images, links, embedded
video iframes, inline style/class so store formatting survives) and strips
scripts, event handlers and javascript: URLs. Used on import (before the HTML
reaches the browser editor) and again on publish (never trust the client).
"""

from __future__ import annotations

import nh3
from bs4 import BeautifulSoup

_TAGS = {
    "p", "br", "hr", "div", "span", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "sub", "sup", "small", "mark", "code", "pre",
    "blockquote", "q", "cite",
    "ul", "ol", "li", "dl", "dt", "dd",
    "a", "img", "picture", "source", "figure", "figcaption", "video", "iframe",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "details", "summary",
}
_COMMON = {"class", "style", "id", "title", "lang", "dir"}
_ATTRS = {
    "*": _COMMON,
    "a": {"href", "target", "rel"},
    "img": {"src", "srcset", "sizes", "alt", "width", "height", "loading"},
    "source": {"src", "srcset", "type", "media", "sizes"},
    "video": {"src", "poster", "controls", "width", "height", "autoplay", "muted", "loop", "playsinline"},
    "iframe": {"src", "width", "height", "allow", "allowfullscreen", "frameborder", "loading"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "type"},
}


def sanitize(html: str | None) -> str:
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=_TAGS,
        attributes={k: set(v) for k, v in _ATTRS.items()},
        url_schemes={"http", "https", "mailto", "tel"},
        link_rel=None,
        strip_comments=True,
    )


def word_count(html: str | None) -> int:
    if not html:
        return 0
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return len(text.split())
