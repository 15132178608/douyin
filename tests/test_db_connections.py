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


def test_block_new_connections_waits_then_releases_connection_acquisition(
    isolated_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.init_schema()
    waiter_entered = threading.Event()
    connection_acquired = threading.Event()
    worker_finished = threading.Event()
    errors: list[str] = []

    original_wait = db._CONNECTION_GATE.wait

    def tracked_wait(timeout: float | None = None) -> bool:
        waiter_entered.set()
        return original_wait(timeout)

    monkeypatch.setattr(db._CONNECTION_GATE, "wait", tracked_wait)

    def worker() -> None:
        try:
            connection = db.get_connection()
            connection.execute("SELECT 1").fetchone()
            connection_acquired.set()
        except Exception as exc:  # pragma: no cover - assertion reports the exact error
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            worker_finished.set()

    thread = threading.Thread(target=worker)
    with db.block_new_connections():
        thread.start()
        assert waiter_entered.wait(timeout=5)
        assert not connection_acquired.is_set()
        assert not worker_finished.is_set()

    assert connection_acquired.wait(timeout=5)
    assert worker_finished.wait(timeout=5)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


def test_block_new_connections_waits_for_inflight_connection_acquisition(
    isolated_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_started = threading.Event()
    allow_make_to_finish = threading.Event()
    make_finished = threading.Event()
    block_attempted = threading.Event()
    block_entered = threading.Event()
    connection_acquired = threading.Event()
    errors: list[str] = []
    make_was_finished_on_block_entry: list[bool] = []

    original_make_connection = db._make_connection

    def paused_make_connection(path: Path) -> sqlite3.Connection:
        make_started.set()
        if not allow_make_to_finish.wait(timeout=5):
            raise TimeoutError("test did not release connection creation")
        connection = original_make_connection(path)
        make_finished.set()
        return connection

    monkeypatch.setattr(db, "_make_connection", paused_make_connection)

    def acquire_connection() -> None:
        try:
            db.get_connection()
            connection_acquired.set()
        except Exception as exc:  # pragma: no cover - assertion reports the exact error
            errors.append(f"acquire_connection: {type(exc).__name__}: {exc}")

    def enter_connection_block() -> None:
        block_attempted.set()
        try:
            with db.block_new_connections():
                make_was_finished_on_block_entry.append(make_finished.is_set())
                block_entered.set()
        except Exception as exc:  # pragma: no cover - assertion reports the exact error
            errors.append(f"enter_connection_block: {type(exc).__name__}: {exc}")

    connection_thread = threading.Thread(target=acquire_connection)
    block_thread = threading.Thread(target=enter_connection_block)
    connection_thread.start()

    make_started_seen = make_started.wait(timeout=5)
    block_attempted_seen = False
    gate_was_available_during_make: bool | None = None
    if make_started_seen:
        block_thread.start()
        block_attempted_seen = block_attempted.wait(timeout=5)
        if block_attempted_seen:
            gate_was_available_during_make = db._CONNECTION_GATE.acquire(blocking=False)
            if gate_was_available_during_make:
                db._CONNECTION_GATE.release()

    allow_make_to_finish.set()
    connection_acquired_seen = connection_acquired.wait(timeout=5)
    block_entered_seen = block_entered.wait(timeout=5) if block_thread.ident else False
    connection_thread.join(timeout=5)
    if block_thread.ident:
        block_thread.join(timeout=5)

    assert make_started_seen
    assert block_attempted_seen
    assert gate_was_available_during_make is False
    assert connection_acquired_seen
    assert block_entered_seen
    assert not connection_thread.is_alive()
    assert not block_thread.is_alive()
    assert make_was_finished_on_block_entry == [True]
    assert errors == []


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


def test_init_schema_at_isolated_path_does_not_replace_thread_connection(
    isolated_db_path: Path,
    tmp_path: Path,
) -> None:
    db.init_schema()
    primary_connection = db.get_connection()
    primary_connection.execute(
        """
        INSERT INTO favorites(user_id, id, first_seen_at, last_seen_at)
        VALUES ('default', 'primary-only', '2026-01-01', '2026-01-01')
        """
    )
    isolated_path = tmp_path / "migration-copy.db"

    db.init_schema_at(isolated_path)

    assert db.get_connection() is primary_connection
    assert settings.db_path == isolated_db_path
    assert primary_connection.execute(
        "SELECT COUNT(*) FROM favorites WHERE id = 'primary-only'"
    ).fetchone()[0] == 1
    isolated = sqlite3.connect(isolated_path)
    try:
        assert isolated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert isolated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert isolated.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0
        assert isolated.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        isolated.close()
    assert Path(str(isolated_path) + "-wal").exists() is False
    assert Path(str(isolated_path) + "-shm").exists() is False
