"""Database safety audit for destructive-regression checks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator

from src import accounts
from src import category_import
from src.crawler import sync
from src.db import SCHEMA_SQL
from src.models import Favorite
from relcheck import backup_drill


PROTECTED_FIELDS = {
    "favorites": ["user_note", "category_id", "favorited_at", "video_created_at", "is_removed"],
    "likes": ["user_note", "category_id", "liked_at", "video_created_at", "is_removed"],
}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    for table in ("favorites", "likes"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "category_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN category_id INTEGER")


def _seed_safety_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    _init_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO users (
            id, display_name, douyin_nickname, douyin_unique_id,
            douyin_sec_uid, douyin_avatar_url, created_at
        ) VALUES (
            'default', '本地默认用户', '主账号', 'main_dy',
            'SEC_MAIN', 'https://example.test/main.jpg', '2026-07-06 00:00:00'
        )
        """
    )
    conn.execute("INSERT OR REPLACE INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-07-06 00:00:00')")
    conn.execute("INSERT OR REPLACE INTO users (id, display_name, created_at) VALUES ('bob', 'Bob', '2026-07-06 00:00:00')")
    fav_cat = conn.execute(
        """
        INSERT INTO categories (account_id, name, auto_name, item_count, created_at, updated_at)
        VALUES ('default', '收藏分类', '收藏分类', 1, '2026-07-06', '2026-07-06')
        """
    ).lastrowid
    like_cat = conn.execute(
        """
        INSERT INTO like_categories (account_id, name, auto_name, item_count, created_at, updated_at)
        VALUES ('default', '喜欢分类', '喜欢分类', 1, '2026-07-06', '2026-07-06')
        """
    ).lastrowid
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
    return conn


def snapshot(conn: sqlite3.Connection) -> dict:
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("favorites", "likes", "categories", "like_categories")
    }
    fields: dict[str, dict[str, dict[str, str | None]]] = {}
    for table, columns in PROTECTED_FIELDS.items():
        rows = conn.execute(
            f"SELECT id, {', '.join(columns)} FROM {table} ORDER BY id"
        ).fetchall()
        fields[table] = {
            row["id"]: {column: None if row[column] is None else str(row[column]) for column in columns}
            for row in rows
        }
    return {"counts": counts, "fields": fields}


def compare_snapshots(before: dict, after: dict) -> list[dict]:
    mismatches: list[dict] = []
    for table, before_count in before["counts"].items():
        after_count = after["counts"].get(table)
        if before_count != after_count:
            mismatches.append(
                {"table": table, "key": "<count>", "field": "count", "before": before_count, "after": after_count}
            )
    for table, before_rows in before["fields"].items():
        after_rows = after["fields"].get(table, {})
        for key, before_fields in before_rows.items():
            if key not in after_rows:
                mismatches.append({"table": table, "key": key, "field": "<row>", "before": before_fields, "after": None})
                continue
            for field, before_value in before_fields.items():
                after_value = after_rows[key].get(field)
                if before_value != after_value:
                    mismatches.append(
                        {
                            "table": table,
                            "key": key,
                            "field": field,
                            "before": before_value,
                            "after": after_value,
                        }
                    )
    return mismatches


@contextmanager
def _patched_connections(conn: sqlite3.Connection) -> Iterator[None]:
    original_accounts_get_connection = accounts.get_connection
    original_sync_get_connection = sync.get_connection
    original_sync_transaction = sync.transaction
    original_category_get_connection = category_import.get_connection

    @contextmanager
    def tx():
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    accounts.get_connection = lambda: conn
    sync.get_connection = lambda: conn
    sync.transaction = tx
    category_import.get_connection = lambda: conn
    try:
        yield
    finally:
        accounts.get_connection = original_accounts_get_connection
        sync.get_connection = original_sync_get_connection
        sync.transaction = original_sync_transaction
        category_import.get_connection = original_category_get_connection


def _check(conn: sqlite3.Connection, name: str, operation) -> dict:
    before = snapshot(conn)
    operation()
    after = snapshot(conn)
    return {
        "name": name,
        "before": before,
        "after": after,
        "mismatches": compare_snapshots(before, after),
    }


def _create_source_category_db(path: Path) -> None:
    conn = _connect(path)
    try:
        _init_schema(conn)
        conn.execute("INSERT OR REPLACE INTO users (id, display_name, created_at) VALUES ('default', '旧用户', '2026-07-01 00:00:00')")
        cat_id = conn.execute(
            """
            INSERT INTO categories (account_id, name, auto_name, item_count, created_at, updated_at)
            VALUES ('default', '旧分类', '旧分类', 1, '2026-07-01', '2026-07-01')
            """
        ).lastrowid
        conn.execute(
            """
            INSERT OR REPLACE INTO favorites (
                user_id, id, title, category_id, first_seen_at, last_seen_at, is_removed
            ) VALUES ('default', 'fav-1', '旧收藏', ?, '2026-07-01', '2026-07-01', 0)
            """,
            (cat_id,),
        )
    finally:
        conn.close()


def run_database_safety_audit(work_dir: Path | str) -> dict:
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "database-safety.db"
    conn = _seed_safety_database(db_path)
    checks: list[dict] = []
    try:
        with _patched_connections(conn):
            checks.append(
                _check(
                    conn,
                    "sync",
                    lambda: (
                        sync.apply_crawl(
                            [
                                Favorite(
                                    id="fav-1",
                                    title="同步后标题",
                                    video_created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                                )
                            ],
                            is_first_crawl=False,
                        ),
                        sync.apply_like_crawl(
                            [
                                Favorite(
                                    id="like-1",
                                    title="同步后喜欢",
                                    video_created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                                )
                            ],
                            is_first_crawl=False,
                        ),
                    ),
                )
            )
            checks.append(_check(conn, "logout", lambda: accounts.clear_douyin_profile("default")))
            checks.append(
                _check(
                    conn,
                    "switch_account",
                    lambda: (
                        accounts.create_session("alice"),
                        accounts.create_session("bob"),
                    ),
                )
            )
            source_path = root / "old-recall.db"
            _create_source_category_db(source_path)
            checks.append(
                _check(
                    conn,
                    "category_import",
                    lambda: category_import.import_categories_from_database(
                        source_path,
                        current_conn=conn,
                        current_db_path=db_path,
                        content_kind="favorites",
                    ),
                )
            )
    finally:
        conn.close()

    drill = backup_drill.run_backup_restore_drill(root / "backup-restore")
    checks.append(
        {
            "name": "backup_restore",
            "before": {
                "counts": {table: len(rows) for table, rows in drill["before"].items()},
                "fields": drill["before"],
            },
            "after": {
                "counts": {table: len(rows) for table, rows in drill["after"].items()},
                "fields": drill["after"],
            },
            "mismatches": drill["mismatches"],
        }
    )

    return {
        "ok": all(not check["mismatches"] for check in checks),
        "protected_fields": PROTECTED_FIELDS,
        "checks": checks,
    }
