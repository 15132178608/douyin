"""
Recall/digest content-kind tests.

Run:
    python tests/test_recall_content_kinds.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from src.cli import cli
from src.db import SCHEMA_SQL
from src.recall import selector


@contextmanager
def isolated_recall_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)

    original_get_connection = selector.get_connection

    def get_connection():
        return conn

    selector.get_connection = get_connection
    try:
        yield conn
    finally:
        selector.get_connection = original_get_connection
        conn.close()


def insert_recall_item(conn: sqlite3.Connection, table: str, item_id: str, title: str) -> None:
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=20)
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, title, author, video_url, cover_url,
            {time_column}, first_seen_at, last_seen_at, last_recalled_at,
            user_note, raw_json, is_removed, discovery_index,
            video_created_at, digg_count
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, 0, ?, ?, ?)
        """,
        (
            item_id,
            title,
            f"{table} author",
            f"https://example.test/{item_id}",
            now - timedelta(days=10),
            first_seen,
            now,
            "{}",
            1,
            now - timedelta(days=365),
            100,
        ),
    )


def test_recall_candidates_can_target_likes_table() -> None:
    with isolated_recall_db() as conn:
        insert_recall_item(conn, "favorites", "fav-1", "favorite only")
        insert_recall_item(conn, "likes", "like-1", "liked only")

        candidates = selector.fetch_candidates(
            ignore_warmup=True,
            content_kind="likes",
        )

        assert [c.id for c in candidates] == ["like-1"]
        assert candidates[0].title == "liked only"


def test_mark_recalled_can_target_likes_table() -> None:
    with isolated_recall_db() as conn:
        insert_recall_item(conn, "likes", "like-1", "liked only")

        selector.mark_recalled(["like-1"], channel="test_digest", content_kind="likes")

        like_row = conn.execute(
            "SELECT last_recalled_at FROM likes WHERE id = 'like-1'"
        ).fetchone()
        fav_log_count = conn.execute("SELECT COUNT(*) AS c FROM recall_log").fetchone()["c"]
        like_log = conn.execute(
            "SELECT like_id, channel FROM like_recall_log"
        ).fetchone()

        assert like_row["last_recalled_at"] is not None
        assert fav_log_count == 0
        assert like_log["like_id"] == "like-1"
        assert like_log["channel"] == "test_digest"


def test_anniversary_picker_handles_timezone_aware_like_timestamps() -> None:
    with isolated_recall_db() as conn:
        insert_recall_item(conn, "likes", "like-anniv", "liked anniversary")
        created_at = (datetime.now(timezone.utc) - timedelta(days=365)).replace(
            microsecond=0
        ).isoformat(sep=" ")
        conn.execute(
            "UPDATE likes SET video_created_at = ? WHERE id = 'like-anniv'",
            (created_at,),
        )

        picked = selector.pick_anniversary(limit=1, seed=1, content_kind="likes")

        assert [item.id for item in picked] == ["like-anniv"]
        assert picked[0].video_created_at is not None


def test_theme_pick_filters_candidates_by_text() -> None:
    with isolated_recall_db() as conn:
        insert_recall_item(conn, "favorites", "fitness", "健身动作合集")
        insert_recall_item(conn, "favorites", "cooking", "家常菜谱")

        picked = selector.pick(
            count=5,
            ignore_warmup=True,
            seed=1,
            content_kind="favorites",
            theme="健身",
        )

        assert [item.id for item in picked] == ["fitness"]


def test_digest_cli_exposes_kind_option() -> None:
    result = CliRunner().invoke(cli, ["digest", "--help"])

    assert result.exit_code == 0
    assert "--kind" in result.output
    assert "--theme" in result.output
    assert "likes" in result.output


if __name__ == "__main__":
    tests = [
        test_recall_candidates_can_target_likes_table,
        test_mark_recalled_can_target_likes_table,
        test_anniversary_picker_handles_timezone_aware_like_timestamps,
        test_theme_pick_filters_candidates_by_text,
        test_digest_cli_exposes_kind_option,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
