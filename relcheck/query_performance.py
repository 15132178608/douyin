"""SQLite query performance audit for large local collections."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

from src.db import SCHEMA_SQL


PERFORMANCE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_fav_active_order "
    "ON favorites(user_id, is_removed, COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC)",
    "CREATE INDEX IF NOT EXISTS idx_fav_active_category_order "
    "ON favorites(user_id, is_removed, category_id, COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC)",
    "CREATE INDEX IF NOT EXISTS idx_fav_active_author_order "
    "ON favorites(user_id, is_removed, author, COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC)",
    "CREATE INDEX IF NOT EXISTS idx_like_active_order "
    "ON likes(user_id, is_removed, COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC)",
    "CREATE INDEX IF NOT EXISTS idx_like_active_category_order "
    "ON likes(user_id, is_removed, category_id, COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC)",
    "CREATE INDEX IF NOT EXISTS idx_like_active_author_order "
    "ON likes(user_id, is_removed, author, COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC)",
]


QUERY_SPECS = [
    {
        "name": "home_list",
        "content_kind": "favorites",
        "surface": "home",
        "table": "favorites",
        "expected_index": "idx_fav_active_order",
        "sql": """
            SELECT id, title, author
            FROM favorites
            WHERE user_id = ? AND is_removed = 0
            ORDER BY COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC
            LIMIT 32
        """,
        "params": ("default",),
    },
    {
        "name": "likes_home_list",
        "content_kind": "likes",
        "surface": "home",
        "table": "likes",
        "expected_index": "idx_like_active_order",
        "sql": """
            SELECT id, title, author
            FROM likes
            WHERE user_id = ? AND is_removed = 0
            ORDER BY COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC
            LIMIT 32
        """,
        "params": ("default",),
    },
    {
        "name": "category_list",
        "content_kind": "favorites",
        "surface": "category",
        "table": "favorites",
        "expected_index": "idx_fav_active_category_order",
        "sql": """
            SELECT id, title, author
            FROM favorites
            WHERE user_id = ? AND is_removed = 0 AND category_id = ?
            ORDER BY COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC
            LIMIT 32
        """,
        "params": ("default", 3),
    },
    {
        "name": "likes_category_list",
        "content_kind": "likes",
        "surface": "category",
        "table": "likes",
        "expected_index": "idx_like_active_category_order",
        "sql": """
            SELECT id, title, author
            FROM likes
            WHERE user_id = ? AND is_removed = 0 AND category_id = ?
            ORDER BY COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC
            LIMIT 32
        """,
        "params": ("default", 3),
    },
    {
        "name": "author_page",
        "content_kind": "favorites",
        "surface": "author_page",
        "table": "favorites",
        "expected_index": "idx_fav_active_author_order",
        "sql": """
            WITH active AS (
                SELECT
                    author,
                    raw_json,
                    COUNT(*) OVER (PARTITION BY author) AS count,
                    ROW_NUMBER() OVER (
                        PARTITION BY author
                        ORDER BY COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC
                    ) AS rn
                FROM favorites
                WHERE user_id = ? AND is_removed = 0 AND author IS NOT NULL AND TRIM(author) <> ''
            )
            SELECT author, count, raw_json
            FROM active
            WHERE rn = 1
            ORDER BY count DESC, author ASC
            LIMIT 80
        """,
        "params": ("default",),
    },
    {
        "name": "likes_author_page",
        "content_kind": "likes",
        "surface": "author_page",
        "table": "likes",
        "expected_index": "idx_like_active_author_order",
        "sql": """
            WITH active AS (
                SELECT
                    author,
                    raw_json,
                    COUNT(*) OVER (PARTITION BY author) AS count,
                    ROW_NUMBER() OVER (
                        PARTITION BY author
                        ORDER BY COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC
                    ) AS rn
                FROM likes
                WHERE user_id = ? AND is_removed = 0 AND author IS NOT NULL AND TRIM(author) <> ''
            )
            SELECT author, count, raw_json
            FROM active
            WHERE rn = 1
            ORDER BY count DESC, author ASC
            LIMIT 80
        """,
        "params": ("default",),
    },
    {
        "name": "search_favorite",
        "content_kind": "favorites",
        "surface": "search",
        "table": "favorites",
        "expected_index": "idx_fav_active_order",
        "sql": """
            SELECT id, title, author
            FROM favorites
            WHERE user_id = ? AND is_removed = 0
              AND (title LIKE ? OR author LIKE ? OR user_note LIKE ?)
            ORDER BY COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC
            LIMIT 32
        """,
        "params": ("default", "%标题 1%", "%标题 1%", "%标题 1%"),
    },
    {
        "name": "search_like",
        "content_kind": "likes",
        "surface": "search",
        "table": "likes",
        "expected_index": "idx_like_active_order",
        "sql": """
            SELECT id, title, author
            FROM likes
            WHERE user_id = ? AND is_removed = 0
              AND (title LIKE ? OR author LIKE ? OR user_note LIKE ?)
            ORDER BY COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC
            LIMIT 32
        """,
        "params": ("default", "%标题 1%", "%标题 1%", "%标题 1%"),
    },
]


def _setup_conn(row_count: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")
    conn.execute("INSERT INTO users (id, display_name, created_at) VALUES ('default', '本地默认用户', '2026-07-06')")
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    for index in range(row_count):
        stamp = (now - timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, author, raw_json, category_id,
                favorited_at, first_seen_at, last_seen_at, is_removed, discovery_index
            ) VALUES ('default', ?, ?, ?, '{}', ?, ?, ?, ?, 0, ?)
            """,
            (
                f"fav-{index}",
                f"收藏标题 {index}",
                f"作者 {index % 60}",
                (index % 12) + 1,
                stamp,
                stamp,
                stamp,
                index,
            ),
        )
        conn.execute(
            """
            INSERT INTO likes (
                user_id, id, title, author, user_note, raw_json, category_id,
                liked_at, first_seen_at, last_seen_at, is_removed, discovery_index
            ) VALUES ('default', ?, ?, ?, ?, '{}', ?, ?, ?, ?, 0, ?)
            """,
            (
                f"like-{index}",
                f"喜欢标题 {index}",
                f"作者 {index % 60}",
                f"备注 {index}",
                (index % 12) + 1,
                stamp,
                stamp,
                stamp,
                index,
            ),
        )
    return conn


def _time_query(conn: sqlite3.Connection, sql: str, params: tuple, repeats: int) -> float:
    safe_repeats = max(1, int(repeats or 1))
    started = time.perf_counter()
    for _ in range(safe_repeats):
        conn.execute(sql, params).fetchall()
    return ((time.perf_counter() - started) * 1000) / safe_repeats


def _query_plan(conn: sqlite3.Connection, sql: str, params: tuple) -> list[str]:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return [str(row[3]) for row in rows]


def _performance_index_names() -> list[str]:
    names: list[str] = []
    for statement in PERFORMANCE_INDEX_SQL:
        marker = "IF NOT EXISTS "
        if marker not in statement:
            continue
        names.append(statement.split(marker, 1)[1].split(None, 1)[0])
    return names


def _available_index_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _plan_uses_index(plan: list[str], index_name: str) -> bool:
    return any(index_name in line for line in plan)


def _plan_scans_main_table(plan: list[str], table: str) -> bool:
    prefix = f"SCAN {table}".upper()
    return any(line.strip().upper().startswith(prefix) for line in plan)


def _plan_uses_temp_btree(plan: list[str]) -> bool:
    return any("USE TEMP B-TREE" in line.upper() for line in plan)


def _build_summary(report: dict) -> dict:
    queries = report["queries"]
    expected_indexes = sorted({item["expected_index"] for item in queries if item.get("expected_index")})
    return {
        "query_count": len(queries),
        "content_kinds": sorted({item["content_kind"] for item in queries}),
        "surfaces": sorted({item["surface"] for item in queries}),
        "expected_indexes": expected_indexes,
        "applied_indexes": _performance_index_names(),
        "available_indexes_after": report["available_indexes_after"],
        "all_expected_indexes_present_after": all(
            index_name in report["available_indexes_after"] for index_name in expected_indexes
        ),
        "all_expected_indexes_used_after": all(
            bool(item.get("expected_index_used_after")) for item in queries if item.get("expected_index")
        ),
        "main_table_scan_after_count": sum(1 for item in queries if item["main_table_scan_after"]),
        "temp_btree_after_count": sum(1 for item in queries if item["temp_btree_after"]),
        "improved_query_count": sum(1 for item in queries if item["improved"]),
        "slow_queries_before": [item["name"] for item in queries if item["slow_before"]],
        "slow_queries_after": [item["name"] for item in queries if item["slow_after"]],
    }


def run_query_performance_audit(
    *,
    row_count: int = 10_000,
    repeats: int = 3,
    slow_threshold_ms: float = 2.0,
) -> dict:
    conn = _setup_conn(row_count)
    try:
        before: dict[str, tuple[float, list[str]]] = {}
        for spec in QUERY_SPECS:
            before_ms = _time_query(conn, spec["sql"], spec["params"], repeats)
            before_plan = _query_plan(conn, spec["sql"], spec["params"])
            before[spec["name"]] = (before_ms, before_plan)
        for statement in PERFORMANCE_INDEX_SQL:
            conn.execute(statement)
        results = []
        for spec in QUERY_SPECS:
            before_ms, before_plan = before[spec["name"]]
            after_ms = _time_query(conn, spec["sql"], spec["params"], repeats)
            after_plan = _query_plan(conn, spec["sql"], spec["params"])
            expected_index = str(spec.get("expected_index") or "")
            table = str(spec["table"])
            improvement_ratio = ((before_ms - after_ms) / before_ms) if before_ms > 0 else None
            results.append(
                {
                    "name": spec["name"],
                    "content_kind": spec["content_kind"],
                    "surface": spec["surface"],
                    "table": table,
                    "expected_index": expected_index,
                    "before_ms": before_ms,
                    "after_ms": after_ms,
                    "delta_ms": after_ms - before_ms,
                    "improvement_ratio": improvement_ratio,
                    "improved": after_ms <= before_ms,
                    "slow_before": before_ms >= float(slow_threshold_ms),
                    "slow_after": after_ms >= float(slow_threshold_ms),
                    "expected_index_used_before": _plan_uses_index(before_plan, expected_index),
                    "expected_index_used_after": _plan_uses_index(after_plan, expected_index),
                    "main_table_scan_before": _plan_scans_main_table(before_plan, table),
                    "main_table_scan_after": _plan_scans_main_table(after_plan, table),
                    "temp_btree_before": _plan_uses_temp_btree(before_plan),
                    "temp_btree_after": _plan_uses_temp_btree(after_plan),
                    "plan_before": before_plan,
                    "plan_after": after_plan,
                }
            )
        report = {
            "row_count": row_count,
            "dataset": {"favorites_rows": row_count, "likes_rows": row_count},
            "repeats": max(1, int(repeats or 1)),
            "slow_threshold_ms": float(slow_threshold_ms),
            "available_indexes_after": _available_index_names(conn),
            "queries": results,
        }
        report["summary"] = _build_summary(report)
        return report
    finally:
        conn.close()


def write_query_performance_report(report: dict, output_path) -> None:
    lines = [
        "# Douyin Recall Query Performance Audit",
        "",
        f"- row_count: `{report['row_count']}`",
        f"- repeats: `{report['repeats']}`",
        f"- slow_threshold_ms: `{report.get('slow_threshold_ms', 2.0)}`",
        f"- query_count: `{report.get('summary', {}).get('query_count', len(report['queries']))}`",
        f"- all_expected_indexes_used_after: `{report.get('summary', {}).get('all_expected_indexes_used_after')}`",
        f"- slow_queries_before: `{', '.join(report.get('summary', {}).get('slow_queries_before', [])) or 'none'}`",
        f"- slow_queries_after: `{', '.join(report.get('summary', {}).get('slow_queries_after', [])) or 'none'}`",
        "",
        "| query | kind | surface | expected_index | index_after | before_ms | after_ms | delta_ms | improved |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for item in report["queries"]:
        lines.append(
            "| {name} | {content_kind} | {surface} | {expected_index} | {expected_index_used_after} | "
            "{before_ms:.2f} | {after_ms:.2f} | {delta_ms:.2f} | {improved} |".format(**item)
        )
    lines.append("")
    for item in report["queries"]:
        lines.append(f"## {item['name']}")
        lines.append("")
        lines.append("Before index plan:")
        lines.extend(f"- `{line}`" for line in item["plan_before"])
        lines.append("")
        lines.append("After index plan:")
        lines.extend(f"- `{line}`" for line in item["plan_after"])
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_query_performance_json(report: dict, output_path) -> None:
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
