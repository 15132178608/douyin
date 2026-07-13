"""Route-free content item queries and template transformations."""
from __future__ import annotations

from typing import Optional

from src.categorize import cluster as cluster_mod
from src.content.kinds import get_content_kind
from src.tenancy import DEFAULT_USER_ID, normalize_user_id
from src.web.authors import cached_author_avatar_url_from_raw_json
from src.web.helpers import get_connection


def row_get(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def category_options(
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[dict]:
    return [
        {"id": category["id"], "name": category["name"], "item_count": category.get("item_count", 0)}
        for category in cluster_mod.list_categories(
            account_id=user_id,
            content_kind=content_kind,
        )
    ]


def row_to_item(row, content_kind: str = "favorites") -> dict:
    """Convert a SQLite row into the template item shape used by all content routes."""
    kind = get_content_kind(content_kind)
    item_prefix = "/favorites" if kind.key == "favorites" else f"/{kind.key}"
    kind_prefix = "" if kind.key == "favorites" else f"/{kind.key}"
    return {
        "id": row["id"],
        "title": row_get(row, "title"),
        "author": row_get(row, "author"),
        "author_avatar_url": cached_author_avatar_url_from_raw_json(row_get(row, "raw_json")),
        "video_url": row_get(row, "video_url"),
        "cover_url": row_get(row, "cover_url"),
        "user_note": row_get(row, "user_note"),
        "category_id": row_get(row, "category_id"),
        "favorited_at": str(row_get(row, "favorited_at")) if row_get(row, "favorited_at") else None,
        "first_seen_at": str(row_get(row, "first_seen_at")) if row_get(row, "first_seen_at") else None,
        "video_created_at": (
            str(row_get(row, "video_created_at")) if row_get(row, "video_created_at") else None
        ),
        "last_recalled_at": (
            str(row_get(row, "last_recalled_at")) if row_get(row, "last_recalled_at") else None
        ),
        "content_kind": kind.key,
        "content_label": kind.label,
        "note_url_prefix": item_prefix,
        "track_url": f"{kind_prefix}/track/open/{row['id']}",
    }


def fetch_item(
    item_id: str,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> Optional[dict]:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    row = get_connection().execute(
        f"""
        SELECT id, title, author, video_url, cover_url, user_note,
               raw_json, category_id,
               CAST({kind.time_column} AS TEXT) AS favorited_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(video_created_at AS TEXT) AS video_created_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at
        FROM {kind.table}
        WHERE user_id = ? AND id = ?
        """,
        (user_id, item_id),
    ).fetchone()
    return row_to_item(row, kind.key) if row else None
