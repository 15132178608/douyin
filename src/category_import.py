"""Import local category assignments from another Douyin Recall database."""
from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import ascii_uppercase

from loguru import logger

from src.config import PROJECT_ROOT, settings
from src.content.kinds import get_content_kind
from src.db import get_connection
from src.tenancy import normalize_user_id


@dataclass(frozen=True)
class CategoryImportCandidate:
    path: Path
    category_count: int
    match_count: int
    source_item_count: int
    content_kind: str
    updated_at: float


@dataclass(frozen=True)
class CategoryImportResult:
    imported: bool
    reason: str
    category_count: int = 0
    assigned_item_count: int = 0
    source_path: Path | None = None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve().samefile(right.resolve())
    except Exception:
        return str(left.resolve()).lower() == str(right.resolve()).lower()


def _current_active_ids(conn: sqlite3.Connection, table: str, user_id: str) -> set[str]:
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE user_id = ? AND is_removed = 0",
        (user_id,),
    ).fetchall()
    return {str(row["id"]) for row in rows}


def _category_count(conn: sqlite3.Connection, table: str, account_id: str) -> int:
    if not _table_exists(conn, table):
        return 0
    columns = _table_columns(conn, table)
    if "account_id" in columns:
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (account_id,)).fetchone()
    else:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0] if row else 0)


def _source_rows_by_category(
    conn: sqlite3.Connection,
    *,
    content_kind: str,
    account_id: str,
    current_ids: set[str],
) -> dict[int, dict]:
    kind = get_content_kind(content_kind)
    if not _table_exists(conn, kind.table) or not _table_exists(conn, kind.category_table):
        return {}
    item_columns = _table_columns(conn, kind.table)
    category_columns = _table_columns(conn, kind.category_table)
    if "category_id" not in item_columns:
        return {}

    item_user_clause = "f.user_id = ?" if "user_id" in item_columns else "1 = 1"
    cat_user_clause = "c.account_id = ?" if "account_id" in category_columns else "1 = 1"
    params: list[object] = []
    if "user_id" in item_columns:
        params.append(account_id)
    if "account_id" in category_columns:
        params.append(account_id)

    rows = conn.execute(
        f"""
        SELECT
            c.id AS category_id,
            c.name,
            c.auto_name,
            c.keywords_json,
            c.centroid_blob,
            c.algo,
            c.created_at,
            c.updated_at,
            f.id AS item_id
        FROM {kind.category_table} c
        JOIN {kind.table} f
          ON f.category_id = c.id
        WHERE {item_user_clause}
          AND {cat_user_clause}
          AND COALESCE(f.is_removed, 0) = 0
          AND f.category_id IS NOT NULL
        """,
        params,
    ).fetchall()

    grouped: dict[int, dict] = {}
    for row in rows:
        item_id = str(row["item_id"])
        cid = int(row["category_id"])
        entry = grouped.setdefault(
            cid,
            {
                "source_id": cid,
                "name": row["name"],
                "auto_name": row["auto_name"] or row["name"],
                "keywords_json": row["keywords_json"],
                "centroid_blob": row["centroid_blob"],
                "algo": row["algo"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "source_item_ids": set(),
                "matched_item_ids": set(),
            },
        )
        entry["source_item_ids"].add(item_id)
        if item_id in current_ids:
            entry["matched_item_ids"].add(item_id)
    return grouped


def summarize_category_source(
    source_path: Path,
    *,
    current_conn: sqlite3.Connection | None = None,
    current_db_path: Path | None = None,
    content_kind: str = "favorites",
    user_id: str = "default",
) -> CategoryImportCandidate | None:
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(user_id)
    source_path = Path(source_path)
    current_db_path = Path(current_db_path or settings.db_path)
    if not source_path.exists() or _same_path(source_path, current_db_path):
        return None

    current_conn = current_conn or get_connection()
    current_ids = _current_active_ids(current_conn, kind.table, account_id)
    if not current_ids:
        return None

    source = sqlite3.connect(source_path, detect_types=sqlite3.PARSE_DECLTYPES)
    source.row_factory = sqlite3.Row
    try:
        grouped = _source_rows_by_category(
            source,
            content_kind=kind.key,
            account_id=account_id,
            current_ids=current_ids,
        )
    finally:
        source.close()
    matched = [entry for entry in grouped.values() if entry["matched_item_ids"]]
    if not matched:
        return None
    return CategoryImportCandidate(
        path=source_path,
        category_count=len(matched),
        match_count=sum(len(entry["matched_item_ids"]) for entry in matched),
        source_item_count=sum(len(entry["source_item_ids"]) for entry in matched),
        content_kind=kind.key,
        updated_at=source_path.stat().st_mtime,
    )


def _candidate_patterns() -> list[str]:
    patterns: list[str] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        patterns.append(str(Path(local_app_data) / "Programs" / "DouyinRecall" / "data" / "recall.db"))

    roots = {PROJECT_ROOT, PROJECT_ROOT.parent}
    for root in roots:
        patterns.append(str(root / "data" / "recall.db"))
        patterns.append(str(root / "DouyinRecall" / "data" / "recall.db"))

    for letter in ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        patterns.extend(
            [
                str(drive / "DouyinRecall" / "data" / "recall.db"),
                str(drive / "douyinclaude" / "data" / "recall.db"),
                str(drive / "*" / "data" / "recall.db"),
                str(drive / "*" / "DouyinRecall" / "data" / "recall.db"),
                str(drive / "*" / "*" / "data" / "recall.db"),
                str(drive / "*" / "*" / "DouyinRecall" / "data" / "recall.db"),
            ]
        )
    return patterns


def default_category_source_paths(current_db_path: Path | None = None) -> list[Path]:
    current_db_path = Path(current_db_path or settings.db_path)
    paths: list[Path] = []
    seen: set[str] = set()
    for pattern in _candidate_patterns():
        for raw in glob.glob(pattern):
            path = Path(raw)
            key = str(path.resolve()).lower()
            if key in seen or not path.exists() or _same_path(path, current_db_path):
                continue
            seen.add(key)
            paths.append(path)
    return paths


def find_category_import_candidates(
    *,
    current_conn: sqlite3.Connection | None = None,
    current_db_path: Path | None = None,
    search_paths: list[Path] | None = None,
    content_kind: str = "favorites",
    user_id: str = "default",
) -> list[CategoryImportCandidate]:
    current_conn = current_conn or get_connection()
    current_db_path = Path(current_db_path or settings.db_path)
    paths = search_paths if search_paths is not None else default_category_source_paths(current_db_path)
    candidates: list[CategoryImportCandidate] = []
    for path in paths:
        try:
            candidate = summarize_category_source(
                Path(path),
                current_conn=current_conn,
                current_db_path=current_db_path,
                content_kind=content_kind,
                user_id=user_id,
            )
            if candidate:
                candidates.append(candidate)
        except sqlite3.DatabaseError as e:
            logger.debug("Skipping category import candidate {}: {}", path, e)
    candidates.sort(key=lambda c: (c.match_count, c.category_count, c.updated_at), reverse=True)
    return candidates


def import_categories_from_database(
    source_path: Path,
    *,
    current_conn: sqlite3.Connection | None = None,
    current_db_path: Path | None = None,
    content_kind: str = "favorites",
    user_id: str = "default",
) -> CategoryImportResult:
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(user_id)
    source_path = Path(source_path)
    current_db_path = Path(current_db_path or settings.db_path)
    if not source_path.exists():
        return CategoryImportResult(False, "source_missing", source_path=source_path)
    if _same_path(source_path, current_db_path):
        return CategoryImportResult(False, "same_database", source_path=source_path)

    current_conn = current_conn or get_connection()
    if _category_count(current_conn, kind.category_table, account_id) > 0:
        return CategoryImportResult(False, "current_has_categories", source_path=source_path)

    current_ids = _current_active_ids(current_conn, kind.table, account_id)
    if not current_ids:
        return CategoryImportResult(False, "current_has_no_items", source_path=source_path)

    source = sqlite3.connect(source_path, detect_types=sqlite3.PARSE_DECLTYPES)
    source.row_factory = sqlite3.Row
    try:
        grouped = _source_rows_by_category(
            source,
            content_kind=kind.key,
            account_id=account_id,
            current_ids=current_ids,
        )
    finally:
        source.close()

    matched = [entry for entry in grouped.values() if entry["matched_item_ids"]]
    if not matched:
        return CategoryImportResult(False, "no_matching_categories", source_path=source_path)

    now = datetime.now(timezone.utc)
    assigned = 0
    category_count = 0
    current_conn.execute("BEGIN")
    try:
        for entry in matched:
            matched_ids = sorted(entry["matched_item_ids"])
            cur = current_conn.execute(
                f"""
                INSERT INTO {kind.category_table} (
                    account_id, name, auto_name, keywords_json, item_count,
                    centroid_blob, algo, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    entry["name"],
                    entry["auto_name"],
                    entry["keywords_json"],
                    len(matched_ids),
                    entry["centroid_blob"],
                    entry["algo"],
                    entry["created_at"] or now,
                    entry["updated_at"] or now,
                ),
            )
            new_category_id = int(cur.lastrowid)
            placeholders = ",".join("?" for _ in matched_ids)
            current_conn.execute(
                f"""
                UPDATE {kind.table}
                SET category_id = ?
                WHERE user_id = ?
                  AND is_removed = 0
                  AND id IN ({placeholders})
                """,
                (new_category_id, account_id, *matched_ids),
            )
            assigned += len(matched_ids)
            category_count += 1
        current_conn.execute("COMMIT")
    except Exception:
        current_conn.execute("ROLLBACK")
        raise

    return CategoryImportResult(
        True,
        "imported",
        category_count=category_count,
        assigned_item_count=assigned,
        source_path=source_path,
    )
