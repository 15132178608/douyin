"""
Registry for local content modules.

The app started with Douyin favorites only. Likes use the same product surface
but must keep independent persistence, indexing, and categorization state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


DEFAULT_CONTENT_KIND = "favorites"


@dataclass(frozen=True)
class ContentKind:
    key: str
    label: str
    table: str
    vector_table: str
    fts_table: str
    category_table: str
    crawl_runs_table: str
    time_column: str


_CONTENT_KINDS: dict[str, ContentKind] = {
    "favorites": ContentKind(
        key="favorites",
        label="收藏",
        table="favorites",
        vector_table="favorites_vec",
        fts_table="favorites_fts",
        category_table="categories",
        crawl_runs_table="crawl_runs",
        time_column="favorited_at",
    ),
    "likes": ContentKind(
        key="likes",
        label="喜欢",
        table="likes",
        vector_table="likes_vec",
        fts_table="likes_fts",
        category_table="like_categories",
        crawl_runs_table="like_crawl_runs",
        time_column="liked_at",
    ),
}


def list_content_kinds() -> list[ContentKind]:
    return list(_CONTENT_KINDS.values())


def get_content_kind(kind: Optional[str]) -> ContentKind:
    return _CONTENT_KINDS.get(kind or DEFAULT_CONTENT_KIND, _CONTENT_KINDS[DEFAULT_CONTENT_KIND])
