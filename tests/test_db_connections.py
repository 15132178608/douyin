from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import settings


@pytest.fixture()
def isolated_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original_path = settings.db_path
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", tmp_path / "recall.db")
    try:
        yield settings.db_path
    finally:
        db.close_connection()
        monkeypatch.setattr(settings, "db_path", original_path)


def test_get_connection_returns_distinct_connection_per_thread(isolated_db_path: Path) -> None:
    connections: list[sqlite3.Connection] = []

    def worker() -> None:
        connections.append(db.get_connection())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(connections) == 2
    assert connections[0] is not connections[1]


def test_concurrent_transactions_are_isolated_and_independent(isolated_db_path: Path) -> None:
    db.init_schema()
    writer_started = threading.Event()
    release_writer = threading.Event()
    errors: list[str] = []

    def rolling_writer() -> None:
        try:
            with db.transaction() as conn:
                for index in range(20):
                    conn.execute(
                        """
                        INSERT INTO favorites (
                            user_id, id, title, first_seen_at, last_seen_at, is_removed
                        ) VALUES ('default', ?, ?, ?, ?, 0)
                        """,
                        (
                            f"rollback-{index}",
                            f"rollback {index}",
                            datetime.now(timezone.utc),
                            datetime.now(timezone.utc),
                        ),
                    )
                writer_started.set()
                release_writer.wait(timeout=5)
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        except Exception as exc:  # pragma: no cover - assertion reports the exact thread error
            errors.append(f"rolling_writer: {type(exc).__name__}: {exc}")

    def independent_writer() -> None:
        writer_started.wait(timeout=5)
        try:
            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO likes (
                        user_id, id, title, first_seen_at, last_seen_at, is_removed
                    ) VALUES ('default', 'like-kept', 'kept like', ?, ?, 0)
                    """,
                    (datetime.now(timezone.utc), datetime.now(timezone.utc)),
                )
        except Exception as exc:  # pragma: no cover - assertion reports the exact thread error
            errors.append(f"independent_writer: {type(exc).__name__}: {exc}")
        finally:
            release_writer.set()

    first = threading.Thread(target=rolling_writer)
    second = threading.Thread(target=independent_writer)
    first.start()
    second.start()
    second.join(timeout=10)
    release_writer.set()
    first.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []

    conn = db.get_connection()
    kept = conn.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE user_id = 'default' AND id = 'like-kept'"
    ).fetchone()["c"]
    rolled_back = conn.execute(
        "SELECT COUNT(*) AS c FROM favorites WHERE user_id = 'default' AND id LIKE 'rollback-%'"
    ).fetchone()["c"]
    assert kept == 1
    assert rolled_back == 0


def test_timestamp_converter_handles_datetime_and_legacy_strings(isolated_db_path: Path) -> None:
    db.init_schema()
    conn = db.get_connection()
    naive = datetime(2026, 1, 2, 3, 4, 5, 123456)
    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    values = [
        ("naive", naive),
        ("aware", aware),
        ("legacy_space", "2026-01-02 03:04:05.250000"),
        ("legacy_iso_tz", "2026-01-02T03:04:05+00:00"),
    ]
    for status, started_at in values:
        conn.execute(
            "INSERT INTO crawl_runs (user_id, started_at, status) VALUES ('default', ?, ?)",
            (started_at, status),
        )

    rows = conn.execute(
        "SELECT started_at, status FROM crawl_runs ORDER BY id"
    ).fetchall()

    by_status = {row["status"]: row["started_at"] for row in rows}
    assert by_status["naive"] == naive
    assert by_status["naive"].tzinfo is None
    assert by_status["aware"] == aware
    assert by_status["aware"].utcoffset() == timedelta(hours=8)
    assert by_status["legacy_space"] == datetime(2026, 1, 2, 3, 4, 5, 250000)
    assert by_status["legacy_iso_tz"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
