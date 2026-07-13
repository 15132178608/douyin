"""Transactional and tenant-bound Web item action tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from src import accounts, db, jobs
from src.config import settings
from src.web import app as web_app
from src.web import item_action_service


@dataclass(frozen=True)
class ContentCase:
    kind: str
    table: str
    time_column: str
    log_table: str
    log_item_column: str
    recall_log_table: str
    recall_item_column: str
    track_path: str


@dataclass(frozen=True)
class ItemActionsDb:
    path: Path
    alice_token: str


CONTENT_CASES = (
    ContentCase(
        kind="favorites",
        table="favorites",
        time_column="favorited_at",
        log_table="uncollect_log",
        log_item_column="favorite_id",
        recall_log_table="recall_log",
        recall_item_column="favorite_id",
        track_path="/track/open/{item_id}",
    ),
    ContentCase(
        kind="likes",
        table="likes",
        time_column="liked_at",
        log_table="unlike_log",
        log_item_column="like_id",
        recall_log_table="like_recall_log",
        recall_item_column="like_id",
        track_path="/likes/track/open/{item_id}",
    ),
)


@pytest.fixture()
def file_backed_item_actions_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ItemActionsDb:
    """Use production connection management so worker threads get distinct connections."""
    db.close_connection()
    db_path = tmp_path / "item-actions.db"
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "web_auth_required", True)
    try:
        db.init_schema()
        conn = db.get_connection()
        now = datetime.now(timezone.utc)
        conn.executemany(
            "INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
            (
                ("alice", "Alice", now),
                ("bob", "Bob", now),
            ),
        )
        token = accounts.create_session("alice")
        assert db_path.is_file()
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        yield ItemActionsDb(path=db_path, alice_token=token)
    finally:
        db.close_connection()


def _insert_item(
    conn: sqlite3.Connection,
    case: ContentCase,
    *,
    user_id: str,
    item_id: str,
) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        f"""
        INSERT INTO {case.table} (
            user_id, id, title, {case.time_column}, first_seen_at,
            last_seen_at, is_removed, discovery_index
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 1)
        """,
        (user_id, item_id, f"{user_id}-{case.kind}-{item_id}", now, now, now),
    )


def _other_case(case: ContentCase) -> ContentCase:
    return CONTENT_CASES[1] if case.kind == "favorites" else CONTENT_CASES[0]


def _removal_logs(conn: sqlite3.Connection, case: ContentCase) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT id, user_id, {case.log_item_column} AS item_id, status, channel
        FROM {case.log_table}
        ORDER BY id
        """
    ).fetchall()


def _queued_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, user_id, kind, payload_json, status
        FROM job_queue
        ORDER BY id
        """
    ).fetchall()


@pytest.mark.parametrize("case", CONTENT_CASES, ids=lambda case: case.kind)
@pytest.mark.parametrize("batch", (False, True), ids=("single", "batch"))
def test_queue_item_removals_succeeds_and_repeats_idempotently(
    file_backed_item_actions_db: ItemActionsDb,
    case: ContentCase,
    batch: bool,
) -> None:
    conn = db.get_connection()
    item_ids = ("item-1", "item-2") if batch else ("item-1",)
    other = _other_case(case)
    for item_id in item_ids:
        _insert_item(conn, case, user_id="alice", item_id=item_id)
        _insert_item(conn, case, user_id="bob", item_id=item_id)
        _insert_item(conn, other, user_id="alice", item_id=item_id)

    channel = "web-batch" if batch else "web"
    first = item_action_service.queue_item_removals(
        case.kind,
        "alice",
        item_ids,
        channel=channel,
    )
    second = item_action_service.queue_item_removals(
        case.kind,
        "alice",
        item_ids,
        channel=channel,
    )

    assert first.queued_ids == item_ids
    assert first.missing_ids == ()
    assert first.already_removed_ids == ()
    assert len(first.job_ids) == len(item_ids)
    assert second.queued_ids == ()
    assert second.missing_ids == ()
    assert second.already_removed_ids == item_ids
    assert second.job_ids == ()

    alice_states = conn.execute(
        f"SELECT id, is_removed FROM {case.table} WHERE user_id = 'alice' ORDER BY id"
    ).fetchall()
    bob_states = conn.execute(
        f"SELECT id, is_removed FROM {case.table} WHERE user_id = 'bob' ORDER BY id"
    ).fetchall()
    other_states = conn.execute(
        f"SELECT id, is_removed FROM {other.table} WHERE user_id = 'alice' ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["is_removed"]) for row in alice_states] == [
        (item_id, 1) for item_id in item_ids
    ]
    assert [(row["id"], row["is_removed"]) for row in bob_states] == [
        (item_id, 0) for item_id in item_ids
    ]
    assert [(row["id"], row["is_removed"]) for row in other_states] == [
        (item_id, 0) for item_id in item_ids
    ]

    logs = _removal_logs(conn, case)
    queued = _queued_jobs(conn)
    assert [(row["user_id"], row["item_id"], row["status"], row["channel"]) for row in logs] == [
        ("alice", item_id, "pending", channel) for item_id in item_ids
    ]
    assert len(queued) == len(item_ids)
    logs_by_item = {row["item_id"]: row["id"] for row in logs}
    for row in queued:
        payload = json.loads(row["payload_json"])
        assert row["user_id"] == "alice"
        assert row["kind"] == "uncollect"
        assert row["status"] == "pending"
        assert payload["content_kind"] == case.kind
        assert payload["log_id"] == logs_by_item[payload["aweme_id"]]


@pytest.mark.parametrize("case", CONTENT_CASES, ids=lambda case: case.kind)
@pytest.mark.parametrize("batch", (False, True), ids=("single", "batch"))
def test_queue_item_removals_rolls_back_after_enqueued_job_then_failure(
    file_backed_item_actions_db: ItemActionsDb,
    monkeypatch: pytest.MonkeyPatch,
    case: ContentCase,
    batch: bool,
) -> None:
    conn = db.get_connection()
    item_ids = ("rollback-1", "rollback-2") if batch else ("rollback-1",)
    for item_id in item_ids:
        _insert_item(conn, case, user_id="alice", item_id=item_id)
    before = {
        row["id"]: row["last_seen_at"]
        for row in conn.execute(
            f"SELECT id, last_seen_at FROM {case.table} WHERE user_id = 'alice' ORDER BY id"
        ).fetchall()
    }

    original_enqueue = jobs.enqueue_job
    fail_on_call = len(item_ids)
    calls = 0

    def enqueue_then_fail(*args, **kwargs):
        nonlocal calls
        job_id = original_enqueue(*args, **kwargs)
        calls += 1
        if calls == fail_on_call:
            raise RuntimeError("injected enqueue failure after insert")
        return job_id

    monkeypatch.setattr(item_action_service.jobs, "enqueue_job", enqueue_then_fail)
    with pytest.raises(RuntimeError, match="injected enqueue failure after insert"):
        item_action_service.queue_item_removals(
            case.kind,
            "alice",
            item_ids,
            channel="web-batch" if batch else "web",
        )

    rows = conn.execute(
        f"""
        SELECT id, is_removed, last_seen_at
        FROM {case.table}
        WHERE user_id = 'alice'
        ORDER BY id
        """
    ).fetchall()
    assert calls == fail_on_call
    assert [(row["id"], row["is_removed"]) for row in rows] == [
        (item_id, 0) for item_id in item_ids
    ]
    assert {row["id"]: row["last_seen_at"] for row in rows} == before
    assert _removal_logs(conn, case) == []
    assert _queued_jobs(conn) == []
    assert conn.in_transaction is False


@pytest.mark.parametrize("case", CONTENT_CASES, ids=lambda case: case.kind)
def test_concurrent_queue_item_removals_use_distinct_connections_and_enqueue_once(
    file_backed_item_actions_db: ItemActionsDb,
    case: ContentCase,
) -> None:
    conn = db.get_connection()
    _insert_item(conn, case, user_id="alice", item_id="concurrent-item")
    barrier = threading.Barrier(2)

    def attempt() -> tuple[int, item_action_service.RemovalBatchResult]:
        thread_conn = db.get_connection()
        connection_id = id(thread_conn)
        barrier.wait(timeout=5)
        result = item_action_service.queue_item_removals(
            case.kind,
            "alice",
            ("concurrent-item",),
            channel="web",
            connection=thread_conn,
        )
        return connection_id, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    connection_ids = {connection_id for connection_id, _result in results}
    removal_results = [result for _connection_id, result in results]
    assert len(connection_ids) == 2
    assert sorted(result.queued_count for result in removal_results) == [0, 1]
    assert sorted(len(result.already_removed_ids) for result in removal_results) == [0, 1]
    assert all(result.missing_ids == () for result in removal_results)
    assert conn.execute(
        f"SELECT is_removed FROM {case.table} WHERE user_id = 'alice' AND id = 'concurrent-item'"
    ).fetchone()["is_removed"] == 1

    logs = _removal_logs(conn, case)
    queued = _queued_jobs(conn)
    assert len(logs) == 1
    assert len(queued) == 1
    payload = json.loads(queued[0]["payload_json"])
    assert logs[0]["user_id"] == "alice"
    assert logs[0]["item_id"] == "concurrent-item"
    assert payload == {
        "aweme_id": "concurrent-item",
        "content_kind": case.kind,
        "log_id": logs[0]["id"],
    }


@pytest.mark.parametrize("case", CONTENT_CASES, ids=lambda case: case.kind)
def test_track_open_records_existing_item_for_each_content_kind(
    file_backed_item_actions_db: ItemActionsDb,
    case: ContentCase,
) -> None:
    conn = db.get_connection()
    _insert_item(conn, case, user_id="alice", item_id="opened-item")
    client = TestClient(web_app.app, raise_server_exceptions=False)
    client.cookies.set(settings.session_cookie_name, file_backed_item_actions_db.alice_token)

    response = client.post(case.track_path.format(item_id="opened-item"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert conn.execute(
        f"SELECT last_recalled_at FROM {case.table} "
        "WHERE user_id = 'alice' AND id = 'opened-item'"
    ).fetchone()["last_recalled_at"] is not None
    rows = conn.execute(
        f"SELECT user_id, {case.recall_item_column} AS item_id, channel, user_action "
        f"FROM {case.recall_log_table}"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("alice", "opened-item", "search", "opened")
    ]


@pytest.mark.parametrize("case", CONTENT_CASES, ids=lambda case: case.kind)
@pytest.mark.parametrize("target_id", ("missing-item", "bob-only"), ids=("missing", "foreign"))
def test_track_open_returns_404_without_writing_log_for_missing_or_foreign_item(
    file_backed_item_actions_db: ItemActionsDb,
    case: ContentCase,
    target_id: str,
) -> None:
    conn = db.get_connection()
    _insert_item(conn, case, user_id="alice", item_id="alice-control")
    _insert_item(conn, case, user_id="bob", item_id="bob-only")
    client = TestClient(web_app.app, raise_server_exceptions=False)
    client.cookies.set(settings.session_cookie_name, file_backed_item_actions_db.alice_token)

    response = client.post(case.track_path.format(item_id=target_id))

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "not found"}
    assert "FOREIGN KEY" not in response.text.upper()
    assert conn.execute(
        f"SELECT COUNT(*) AS c FROM {case.recall_log_table}"
    ).fetchone()["c"] == 0
    timestamps = conn.execute(
        f"""
        SELECT user_id, id, last_recalled_at
        FROM {case.table}
        WHERE id IN ('alice-control', 'bob-only')
        ORDER BY user_id, id
        """
    ).fetchall()
    assert [(row["user_id"], row["id"], row["last_recalled_at"]) for row in timestamps] == [
        ("alice", "alice-control", None),
        ("bob", "bob-only", None),
    ]
    assert conn.in_transaction is False
