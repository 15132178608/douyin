"""User-scoped JSON, Markdown, and SQLite backup exports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from src.content.kinds import get_content_kind
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


@dataclass(frozen=True)
class ExportResult:
    path: Path
    count: int


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _ensure_output_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _fetch_items(user_id: str, content_kind: str) -> list[dict]:
    kind = get_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id, title, description, author, author_id, video_url, cover_url,
               duration_ms, CAST({kind.time_column} AS TEXT) AS saved_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(last_seen_at AS TEXT) AS last_seen_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at,
               user_note, raw_json, discovery_index, video_tags,
               CAST(video_created_at AS TEXT) AS video_created_at,
               digg_count, category_id
        FROM {kind.table}
        WHERE user_id = ? AND is_removed = 0
        ORDER BY COALESCE({kind.time_column}, first_seen_at) DESC, discovery_index DESC
        """,
        (uid,),
    ).fetchall()
    return [dict(row) for row in rows]


def export_json(
    output_dir: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    content_kind: str = "favorites",
) -> ExportResult:
    kind = get_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    output_dir = _ensure_output_dir(output_dir)
    items = _fetch_items(uid, kind.key)
    path = output_dir / f"douyin-recall-{uid}-{kind.key}-{_timestamp()}.json"
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_id": uid,
        "content_kind": kind.key,
        "content_label": kind.label,
        "count": len(items),
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return ExportResult(path=path, count=len(items))


def export_markdown(
    output_dir: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    content_kind: str = "favorites",
) -> ExportResult:
    kind = get_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    output_dir = _ensure_output_dir(output_dir)
    items = _fetch_items(uid, kind.key)
    path = output_dir / f"douyin-recall-{uid}-{kind.key}-{_timestamp()}.md"
    lines = [
        f"# 抖音{kind.label}导出",
        "",
        f"- 用户：{uid}",
        f"- 数量：{len(items)}",
        f"- 导出时间：{datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for item in items:
        title = (item.get("title") or "(无标题)").strip()
        lines.extend(
            [
                f"## {title}",
                "",
                f"- 作者：@{item.get('author') or '?'}",
                f"- 链接：{item.get('video_url') or ''}",
            ]
        )
        if item.get("saved_at"):
            lines.append(f"- {'喜欢' if kind.key == 'likes' else '收藏'}时间：{item['saved_at']}")
        if item.get("video_created_at"):
            lines.append(f"- 发布时间：{item['video_created_at']}")
        if item.get("user_note"):
            lines.extend(["", f"> {item['user_note']}"])
        if item.get("description"):
            lines.extend(["", str(item["description"])])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return ExportResult(path=path, count=len(items))


def backup_sqlite(output_dir: Path) -> ExportResult:
    output_dir = _ensure_output_dir(output_dir)
    path = output_dir / f"recall-backup-{_timestamp()}.db"
    source = get_connection()
    destination = sqlite3.connect(path)
    try:
        source.backup(destination)
    finally:
        destination.close()
    count = 0
    for table in ("favorites", "likes"):
        try:
            count += int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            pass
    return ExportResult(path=path, count=count)
