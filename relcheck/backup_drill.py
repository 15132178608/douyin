"""Automated SQLite backup and restore drill."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from src import maintenance
from src.db import SCHEMA_SQL


SNAPSHOT_TABLES: dict[str, tuple[str, list[str]]] = {
    "favorites": ("id", ["title", "user_note", "category_id", "favorited_at", "video_created_at", "is_removed"]),
    "likes": ("id", ["title", "user_note", "category_id", "liked_at", "video_created_at", "is_removed"]),
    "categories": ("id", ["name", "item_count"]),
    "like_categories": ("id", ["name", "item_count"]),
}


def _value(value: Any) -> Any:
    return None if value is None else str(value)


def create_drill_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
        for table in ("favorites", "likes"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "category_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN category_id INTEGER")
        conn.execute(
            "INSERT OR REPLACE INTO users (id, display_name, created_at) VALUES ('default', '本地默认用户', '2026-07-06')"
        )
        fav_cat = 1
        like_cat = 1
        conn.execute(
            """
            INSERT OR REPLACE INTO categories (id, account_id, name, auto_name, item_count, created_at, updated_at)
            VALUES (?, 'default', '收藏分类', '收藏分类', 1, '2026-07-06', '2026-07-06')
            """,
            (fav_cat,),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO like_categories (id, account_id, name, auto_name, item_count, created_at, updated_at)
            VALUES (?, 'default', '喜欢分类', '喜欢分类', 1, '2026-07-06', '2026-07-06')
            """,
            (like_cat,),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO favorites (
                user_id, id, title, user_note, category_id, favorited_at,
                video_created_at, first_seen_at, last_seen_at, is_removed
            ) VALUES (
                'default', 'fav-1', '收藏标题', '收藏备注', ?,
                '2026-01-02 03:04:05', '2025-12-01 00:00:00',
                '2026-07-06', '2026-07-06', 0
            )
            """,
            (fav_cat,),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO likes (
                user_id, id, title, user_note, category_id, liked_at,
                video_created_at, first_seen_at, last_seen_at, is_removed
            ) VALUES (
                'default', 'like-1', '喜欢标题', '喜欢备注', ?,
                '2026-02-03 04:05:06', '2025-11-01 00:00:00',
                '2026-07-06', '2026-07-06', 0
            )
            """,
            (like_cat,),
        )
        conn.commit()
    finally:
        conn.close()


def snapshot_database(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot: dict[str, dict[str, dict[str, Any]]] = {}
        for table, (key_column, fields) in SNAPSHOT_TABLES.items():
            rows = conn.execute(
                f"SELECT {key_column}, {', '.join(fields)} FROM {table} ORDER BY {key_column}"
            ).fetchall()
            snapshot[table] = {
                str(row[key_column]): {field: _value(row[field]) for field in fields}
                for row in rows
            }
        return snapshot
    finally:
        conn.close()


def compare_snapshots(before: dict, after: dict) -> list[dict]:
    mismatches: list[dict] = []
    for table in sorted(set(before) | set(after)):
        before_rows = before.get(table, {})
        after_rows = after.get(table, {})
        for key in sorted(set(before_rows) | set(after_rows)):
            if key not in before_rows:
                mismatches.append({"table": table, "key": key, "field": "<row>", "before": None, "after": after_rows[key]})
                continue
            if key not in after_rows:
                mismatches.append({"table": table, "key": key, "field": "<row>", "before": before_rows[key], "after": None})
                continue
            for field in sorted(set(before_rows[key]) | set(after_rows[key])):
                if before_rows[key].get(field) != after_rows[key].get(field):
                    mismatches.append(
                        {
                            "table": table,
                            "key": key,
                            "field": field,
                            "before": before_rows[key].get(field),
                            "after": after_rows[key].get(field),
                        }
                    )
    return mismatches


def snapshot_counts(snapshot: dict[str, dict[str, dict[str, Any]]]) -> dict[str, int]:
    return {table: len(rows) for table, rows in snapshot.items()}


def format_mismatches(mismatches: list[dict]) -> list[str]:
    return [
        f"{item['table']}[{item['key']}].{item['field']}: {item['before']} -> {item['after']}"
        for item in mismatches
    ]


def _copy_sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source)
    dest_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()


def run_backup_restore_drill(work_dir: Path | str) -> dict:
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    current_db = root / "drill-current.db"
    backup_db = root / "recall-backup-drill.db"
    restore_safety_dir = root / "restore-safety"

    create_drill_database(current_db)
    before = snapshot_database(current_db)
    _copy_sqlite_backup(current_db, backup_db)

    damaged_conn = sqlite3.connect(current_db)
    try:
        damaged_conn.execute("UPDATE favorites SET title = 'damaged' WHERE id = 'fav-1'")
        damaged_conn.execute("DELETE FROM likes WHERE id = 'like-1'")
        damaged_conn.commit()
    finally:
        damaged_conn.close()
    damaged = snapshot_database(current_db)
    damage_mismatches = compare_snapshots(before, damaged)

    result = maintenance.restore_sqlite_backup(
        backup_db,
        db_path=current_db,
        backup_dir=restore_safety_dir,
        close_connection=lambda: None,
    )
    after = snapshot_database(current_db)
    mismatches = compare_snapshots(before, after)

    return {
        "ok": bool(damage_mismatches) and not mismatches,
        "backup_path": str(backup_db),
        "restored_path": str(result.restored_path),
        "safety_backup_path": str(result.safety_backup_path),
        "compared_tables": sorted(SNAPSHOT_TABLES),
        "counts": {
            "before": snapshot_counts(before),
            "damaged": snapshot_counts(damaged),
            "after": snapshot_counts(after),
        },
        "damage": {
            "detected": bool(damage_mismatches),
            "mismatches": damage_mismatches,
            "messages": format_mismatches(damage_mismatches),
        },
        "before": before,
        "damaged": damaged,
        "after": after,
        "mismatches": mismatches,
        "failure_messages": format_mismatches(mismatches),
    }
