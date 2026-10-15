"""Shared page metadata for server-side paginated lists."""

from pydantic import BaseModel


class PageInfo(BaseModel):
    page: int
    page_size: int
    total: int          # rows matching the filters (all pages)
    total_pages: int
