"""
Second-level tag suggestion/storage tests.

Run:
    python tests/test_llm_tags.py
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from src import db
from src.tagging import llm_tags


@contextmanager
def isolated_tag_db():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    original_get_connection = llm_tags.get_connection
    llm_tags.get_connection = lambda: conn
    try:
        yield conn
    finally:
        llm_tags.get_connection = original_get_connection
        conn.close()


def test_suggest_second_level_tags_returns_short_domain_tags() -> None:
    tags = llm_tags.suggest_second_level_tags("健身 拉伸 肩颈 放松 肌肉", max_tags=2)

    assert tags == ["健身", "拉伸"]


def test_ollama_provider_parses_json_tags() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"response": "{\"tags\":[\"健身\",\"肩颈\"]}"}

    original_post = llm_tags.httpx.post
    llm_tags.httpx.post = lambda *args, **kwargs: FakeResponse()
    try:
        tags = llm_tags.suggest_second_level_tags(
            "健身 肩颈 放松",
            max_tags=3,
            provider="ollama",
            model="qwen",
        )
    finally:
        llm_tags.httpx.post = original_post

    assert tags == ["健身", "肩颈"]


def test_write_tags_updates_user_scoped_item_only() -> None:
    with isolated_tag_db() as conn:
        for user_id in ("alice", "bob"):
            conn.execute(
                """
                INSERT INTO favorites (
                    user_id, id, title, first_seen_at, last_seen_at, is_removed
                ) VALUES (?, 'same', ?, '2026-05-26', '2026-05-26', 0)
                """,
                (user_id, f"{user_id} item"),
            )

        assert llm_tags.write_tags("same", ["健身", "拉伸"], user_id="alice") is True

        rows = {
            r["user_id"]: r["llm_tags"]
            for r in conn.execute("SELECT user_id, llm_tags FROM favorites ORDER BY user_id").fetchall()
        }
        assert json.loads(rows["alice"]) == ["健身", "拉伸"]
        assert rows["bob"] is None


if __name__ == "__main__":
    tests = [
        test_suggest_second_level_tags_returns_short_domain_tags,
        test_ollama_provider_parses_json_tags,
        test_write_tags_updates_user_scoped_item_only,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as e:
            print(f"FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
