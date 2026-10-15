"""Server-side list paging: filter → sort → slice, plus page metadata.

Lists are built in Python where rows need merging (e.g. a competitor's ranked
keywords joined with our positions), then only ONE page goes over the wire.

    rows, info = paginate(rows, page=2, page_size=25, sort_key="etv", descending=True)
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import Query

T = TypeVar("T")
MAX_PAGE_SIZE = 1000          # export uses large pages; the UI asks for 10-50


@dataclass(frozen=True)
class PageQuery:
    page: int
    page_size: int
    sort: str | None
    descending: bool
    q: str | None

    def search(self) -> str:
        return (self.q or "").strip().lower()


def page_query(
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(25, ge=1, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None, max_length=40, description="Field to sort by"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    q: str | None = Query(None, max_length=200, description="Text search"),
) -> PageQuery:
    """FastAPI dependency: the standard paging query params for list endpoints."""
    return PageQuery(page=page, page_size=page_size, sort=sort, descending=order == "desc", q=q)


def paginate(
    items: Sequence[T], *, page: int, page_size: int,
    sort_key: str | Callable[[T], Any] | None = None, descending: bool = True,
) -> tuple[list[T], dict]:
    """Sort (None values always last) and slice one page; returns (rows, page info)."""
    rows = list(items)
    if sort_key is not None:
        get = sort_key if callable(sort_key) else (lambda r, k=sort_key: r.get(k))  # type: ignore[union-attr]
        present = [r for r in rows if get(r) is not None]
        missing = [r for r in rows if get(r) is None]
        present.sort(key=get, reverse=descending)
        rows = present + missing
    total = len(rows)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return rows[start:start + page_size], {
        "page": page, "page_size": page_size, "total": total, "total_pages": total_pages,
    }
