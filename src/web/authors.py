"""
Helpers for author listing views.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote


def _first_http_url(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    urls = value.get("url_list") or []
    if not isinstance(urls, list):
        return None
    for url in urls:
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            return url
    return None


def author_avatar_url_from_raw_json(raw_json: str | None) -> Optional[str]:
    """
    Extract an author's remote avatar URL from a stored Douyin aweme JSON blob.

    The UI displays this URL directly; this helper does not download or cache files.
    """
    if not raw_json:
        return None
    try:
        item = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(item, dict):
        return None

    author = item.get("author")
    if not isinstance(author, dict):
        return None

    for key in ("avatar_thumb", "avatar_medium", "avatar_larger", "avatar_large"):
        url = _first_http_url(author.get(key))
        if url:
            return url
    return None


def cached_author_avatar_url_from_raw_json(raw_json: str | None) -> Optional[str]:
    url = author_avatar_url_from_raw_json(raw_json)
    if not url:
        return None
    return f"/avatar-cache?u={quote(url, safe='')}"
