"""
Background job queue tests.

Run:
    python tests/test_jobs.py
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src import db
from src import exporter
from src import jobs
from src.config import settings


@contextmanager
def isolated_jobs_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-05-26 00:00:00')"
    )

    original_get_connection = jobs.get_connection
    original_db_get_connection = db.get_connection
    jobs.get_connection = lambda: conn
    db.get_connection = lambda: conn
    try:
        yield conn
    finally:
        jobs.get_connection = original_get_connection
        db.get_connection = original_db_get_connection
        conn.close()


def test_enqueue_and_claim_job_are_user_scoped() -> None:
    with isolated_jobs_db() as conn:
        job_id = jobs.enqueue_job("sync_favorites", user_id="alice", payload={"max_pages": 1})

        row = conn.execute("SELECT user_id, kind, status, payload_json FROM job_queue").fetchone()
        assert row["user_id"] == "alice"
        assert row["kind"] == "sync_favorites"
        assert row["status"] == "pending"
        assert '"max_pages": 1' in row["payload_json"]

        claimed = jobs.claim_next_job()
        assert claimed["id"] == job_id
        assert claimed["user_id"] == "alice"
        assert claimed["payload"]["max_pages"] == 1
        assert conn.execute("SELECT status FROM job_queue WHERE id = ?", (job_id,)).fetchone()["status"] == "running"


def test_enqueue_job_suppresses_duplicate_open_work_with_same_payload() -> None:
    with isolated_jobs_db() as conn:
        first_id = jobs.enqueue_job(
            "sync_favorites",
            user_id="alice",
            payload={"content_kind": "favorites", "max_pages": 5},
        )
        second_id = jobs.enqueue_job(
            "sync_favorites",
            user_id="alice",
            payload={"max_pages": 5, "content_kind": "favorites"},
        )

        rows = conn.execute("SELECT id, payload_json FROM job_queue").fetchall()
        assert second_id == first_id
        assert len(rows) == 1
        assert rows[0]["payload_json"] == '{"content_kind": "favorites", "max_pages": 5}'


def test_enqueue_job_joins_explicit_outer_transaction_without_committing() -> None:
    with isolated_jobs_db() as conn:
        original_get_connection = jobs.get_connection

        def unexpected_get_connection():
            raise AssertionError("explicit connection should bypass jobs.get_connection")

        jobs.get_connection = unexpected_get_connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            first_id = jobs.enqueue_job(
                "index",
                user_id="alice",
                payload={"content_kind": "favorites", "force": True},
                connection=conn,
            )
            second_id = jobs.enqueue_job(
                "index",
                user_id="alice",
                payload={"force": True, "content_kind": "favorites"},
                connection=conn,
            )

            assert conn.in_transaction is True
            assert second_id == first_id
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM job_queue"
            ).fetchone()["c"] == 1
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            jobs.get_connection = original_get_connection

        assert conn.in_transaction is False
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM job_queue"
        ).fetchone()["c"] == 0


def test_enqueue_job_suppresses_concurrent_duplicate_work(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs-concurrency.db"
    original_path = settings.db_path
    db.close_connection()
    settings.db_path = db_path
    try:
        db.init_schema()
        conn = db.get_connection()
        conn.execute(
            """
            INSERT INTO users (id, display_name, created_at)
            VALUES ('alice', 'Alice', ?)
            """,
            (datetime.now(timezone.utc),),
        )
        barrier = threading.Barrier(2)

        def enqueue(_index: int) -> int:
            barrier.wait(timeout=5)
            return jobs.enqueue_job(
                "index",
                user_id="alice",
                payload={"content_kind": "favorites", "force": True},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            job_ids = list(pool.map(enqueue, range(2)))

        assert job_ids[0] == job_ids[1]
        assert db.get_connection().execute(
            "SELECT COUNT(*) AS c FROM job_queue"
        ).fetchone()["c"] == 1
    finally:
        db.close_connection()
        settings.db_path = original_path


def test_enqueue_job_allows_new_work_after_terminal_state() -> None:
    with isolated_jobs_db() as conn:
        first_id = jobs.enqueue_job("index", user_id="alice", payload={"content_kind": "likes"})
        jobs.finish_job(first_id)

        second_id = jobs.enqueue_job("index", user_id="alice", payload={"content_kind": "likes"})

        states = [
            row["status"]
            for row in conn.execute("SELECT status FROM job_queue ORDER BY id").fetchall()
        ]
        assert second_id != first_id
        assert states == ["success", "pending"]


def test_finish_and_fail_job_persist_terminal_state() -> None:
    with isolated_jobs_db() as conn:
        done_id = jobs.enqueue_job("index", user_id="alice")
        failed_id = jobs.enqueue_job("uncollect", user_id="alice", max_attempts=1)

        jobs.finish_job(done_id)
        jobs.claim_next_job()
        jobs.fail_job(failed_id, "boom")

        states = {
            r["id"]: (r["status"], r["error_message"])
            for r in conn.execute("SELECT id, status, error_message FROM job_queue").fetchall()
        }
        assert states[done_id] == ("success", None)
        assert states[failed_id] == ("failed", "boom")


def test_fail_job_requeues_until_max_attempts_with_backoff() -> None:
    with isolated_jobs_db() as conn:
        job_id = jobs.enqueue_job("sync_favorites", user_id="alice", max_attempts=3)
        claimed = jobs.claim_next_job()

        jobs.fail_job(job_id, "temporary")

        row = conn.execute(
            "SELECT status, attempts, max_attempts, next_run_at, error_message FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert claimed["attempts"] == 1
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["max_attempts"] == 3
        assert row["next_run_at"] is not None
        assert "temporary" in row["error_message"]


def test_durable_search_reindex_keeps_retrying_after_attempt_budget() -> None:
    with isolated_jobs_db() as conn:
        conn.execute(
            """
            INSERT INTO search_reindex_state (
                user_id, content_kind, required_at, reason
            ) VALUES ('alice', 'favorites', ?, 'schema_rebuilt')
            """,
            (datetime.now(timezone.utc),),
        )
        job_id = jobs.enqueue_job(
            "index",
            user_id="alice",
            payload={"content_kind": "favorites", "force": True},
            max_attempts=3,
        )

        class RecoveringHandlers:
            calls = 0

            def index(self, user_id: str, payload: dict) -> None:
                self.calls += 1
                if self.calls <= 4:
                    raise RuntimeError("encoder temporarily unavailable")
                conn.execute(
                    """
                    UPDATE search_reindex_state
                    SET completed_at = ?
                    WHERE user_id = ? AND content_kind = ?
                    """,
                    (datetime.now(timezone.utc), user_id, payload["content_kind"]),
                )

        handlers = RecoveringHandlers()
        for expected_attempts in (1, 2):
            assert jobs.run_next_job(handlers) is True
            row = conn.execute(
                "SELECT status, attempts, next_run_at FROM job_queue WHERE id = ?",
                (job_id,),
            ).fetchone()
            assert (row["status"], row["attempts"]) == ("pending", expected_attempts)
            conn.execute(
                "UPDATE job_queue SET next_run_at = ? WHERE id = ?",
                (datetime.now(timezone.utc) - timedelta(seconds=1), job_id),
            )

        assert jobs.run_next_job(handlers) is True
        exhausted = conn.execute(
            "SELECT status, attempts, max_attempts, next_run_at FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert (exhausted["status"], exhausted["attempts"], exhausted["max_attempts"]) == (
            "pending",
            3,
            4,
        )
        assert exhausted["next_run_at"] > datetime.now(timezone.utc)
        assert jobs.claim_next_job() is None
        assert conn.execute("SELECT COUNT(*) AS c FROM job_queue").fetchone()["c"] == 1

        conn.execute(
            "UPDATE job_queue SET next_run_at = ? WHERE id = ?",
            (datetime.now(timezone.utc) - timedelta(seconds=1), job_id),
        )
        assert jobs.run_next_job(handlers) is True
        delayed_again = conn.execute(
            "SELECT status, attempts, max_attempts, next_run_at FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert (
            delayed_again["status"],
            delayed_again["attempts"],
            delayed_again["max_attempts"],
        ) == ("pending", 4, 5)
        assert delayed_again["next_run_at"] > datetime.now(timezone.utc)

        conn.execute(
            "UPDATE job_queue SET next_run_at = ? WHERE id = ?",
            (datetime.now(timezone.utc) - timedelta(seconds=1), job_id),
        )
        assert jobs.run_next_job(handlers) is True
        final = conn.execute(
            "SELECT status, attempts FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        marker = conn.execute(
            "SELECT completed_at FROM search_reindex_state WHERE user_id = 'alice'"
        ).fetchone()
        assert (final["status"], final["attempts"]) == ("success", 5)
        assert marker["completed_at"] is not None


def test_non_object_payload_does_not_crash_worker_or_become_durable() -> None:
    class FailingHandlers:
        def index(self, user_id: str, payload: dict) -> None:
            assert payload == {}
            raise RuntimeError("invalid payload is terminal")

    with isolated_jobs_db() as conn:
        conn.execute(
            """
            INSERT INTO search_reindex_state (
                user_id, content_kind, required_at, reason
            ) VALUES ('alice', 'favorites', ?, 'schema_rebuilt')
            """,
            (datetime.now(timezone.utc),),
        )
        job_id = jobs.enqueue_job("index", user_id="alice", max_attempts=1)
        conn.execute("UPDATE job_queue SET payload_json = '[]' WHERE id = ?", (job_id,))

        assert jobs.run_next_job(FailingHandlers()) is True

        row = conn.execute(
            "SELECT status, attempts FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert (row["status"], row["attempts"]) == ("failed", 1)


def test_force_index_without_reindex_marker_still_fails_terminally() -> None:
    class FailingHandlers:
        def index(self, user_id: str, payload: dict) -> None:
            raise RuntimeError("not durable")

    with isolated_jobs_db() as conn:
        job_id = jobs.enqueue_job(
            "index",
            user_id="alice",
            payload={"content_kind": "favorites", "force": True},
            max_attempts=1,
        )

        assert jobs.run_next_job(FailingHandlers()) is True

        row = conn.execute(
            "SELECT status, attempts FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert (row["status"], row["attempts"]) == ("failed", 1)


def test_pending_reindex_marker_replaces_old_failed_job_only_once() -> None:
    with isolated_jobs_db() as conn:
        conn.execute(
            """
            INSERT INTO search_reindex_state (
                user_id, content_kind, required_at, reason
            ) VALUES ('alice', 'favorites', ?, 'schema_rebuilt')
            """,
            (datetime.now(timezone.utc),),
        )
        old_id = jobs.enqueue_job(
            "index",
            user_id="alice",
            payload={"content_kind": "favorites", "force": True},
            max_attempts=1,
        )
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'failed', attempts = 1, finished_at = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc), old_id),
        )

        first = jobs.enqueue_pending_search_reindexes()
        second = jobs.enqueue_pending_search_reindexes()

        rows = conn.execute(
            "SELECT id, status FROM job_queue ORDER BY id"
        ).fetchall()
        assert first == second
        assert first[0] != old_id
        assert [(row["id"], row["status"]) for row in rows] == [
            (old_id, "failed"),
            (first[0], "pending"),
        ]


def test_claim_next_job_skips_pending_jobs_until_next_run_at() -> None:
    with isolated_jobs_db() as conn:
        due_id = jobs.enqueue_job("index", user_id="alice")
        delayed_id = jobs.enqueue_job("sync_favorites", user_id="alice")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        conn.execute(
            "UPDATE job_queue SET next_run_at = ? WHERE id = ?",
            (future, delayed_id),
        )

        claimed = jobs.claim_next_job()

        assert claimed["id"] == due_id


def test_recover_stale_running_jobs_requeues_expired_running_work() -> None:
    with isolated_jobs_db() as conn:
        stale_id = jobs.enqueue_job("sync_favorites", user_id="alice")
        fresh_id = jobs.enqueue_job("index", user_id="alice")
        stale_started = datetime.now(timezone.utc) - timedelta(hours=2)
        fresh_started = datetime.now(timezone.utc)
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (stale_started, stale_id),
        )
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (fresh_started, fresh_id),
        )

        recovered = jobs.recover_stale_running_jobs(stale_after_seconds=3600)

        rows = {
            r["id"]: (r["status"], r["next_run_at"], r["error_message"])
            for r in conn.execute(
                "SELECT id, status, next_run_at, error_message FROM job_queue ORDER BY id"
            ).fetchall()
        }
        assert recovered == 1
        assert rows[stale_id][0] == "pending"
        assert rows[stale_id][1] is not None
        assert "stale running job recovered" in rows[stale_id][2]
        assert rows[fresh_id][0] == "running"


def test_recover_stale_running_jobs_can_recover_immediately_on_startup() -> None:
    with isolated_jobs_db() as conn:
        job_id = jobs.enqueue_job("sync_likes", user_id="alice")
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (datetime.now(timezone.utc), job_id),
        )

        recovered = jobs.recover_stale_running_jobs(stale_after_seconds=0)

        row = conn.execute(
            "SELECT status, started_at, next_run_at, error_message FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert recovered == 1
        assert row["status"] == "pending"
        assert row["started_at"] is None
        assert row["next_run_at"] is not None
        assert "stale running job recovered" in row["error_message"]


def test_recover_stale_durable_reindex_after_attempt_budget() -> None:
    with isolated_jobs_db() as conn:
        conn.execute(
            """
            INSERT INTO search_reindex_state (
                user_id, content_kind, required_at, reason
            ) VALUES ('alice', 'favorites', ?, 'schema_rebuilt')
            """,
            (datetime.now(timezone.utc),),
        )
        job_id = jobs.enqueue_job(
            "index",
            user_id="alice",
            payload={"content_kind": "favorites", "force": True},
            max_attempts=3,
        )
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'running', attempts = 3, started_at = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc) - timedelta(hours=2), job_id),
        )

        recovered = jobs.recover_stale_running_jobs(stale_after_seconds=3600)

        row = conn.execute(
            """
            SELECT status, attempts, max_attempts, started_at, next_run_at
            FROM job_queue WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        assert recovered == 1
        assert (row["status"], row["attempts"], row["max_attempts"]) == (
            "pending",
            3,
            4,
        )
        assert row["started_at"] is None
        assert row["next_run_at"] > datetime.now(timezone.utc)


def test_recover_stale_running_jobs_uses_compare_and_swap() -> None:
    with isolated_jobs_db() as conn:
        job_id = jobs.enqueue_job("sync_favorites", user_id="alice")
        stale_started = datetime.now(timezone.utc) - timedelta(hours=2)
        fresh_started = datetime.now(timezone.utc)
        conn.execute(
            "UPDATE job_queue SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (stale_started, job_id),
        )

        class RacingConnection:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self.wrapped = wrapped
                self.injected = False

            def execute(self, sql: str, params=()):
                normalized = " ".join(sql.split())
                if not self.injected and "UPDATE job_queue SET status = 'pending'" in normalized:
                    self.injected = True
                    self.wrapped.execute(
                        "UPDATE job_queue SET started_at = ?, attempts = 2 WHERE id = ?",
                        (fresh_started, job_id),
                    )
                return self.wrapped.execute(sql, params)

        original_get_connection = jobs.get_connection
        jobs.get_connection = lambda: RacingConnection(conn)
        racing = jobs.get_connection()
        jobs.get_connection = lambda: racing
        try:
            recovered = jobs.recover_stale_running_jobs(stale_after_seconds=3600)
        finally:
            jobs.get_connection = original_get_connection

        row = conn.execute(
            "SELECT status, attempts, started_at FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert recovered == 0
        assert (row["status"], row["attempts"], row["started_at"]) == (
            "running",
            2,
            fresh_started,
        )


def test_old_worker_cannot_finish_or_fail_a_reclaimed_attempt() -> None:
    with isolated_jobs_db() as conn:
        job_id = jobs.enqueue_job("sync_favorites", user_id="alice")

        class ReclaimingHandlers:
            first_attempts: int | None = None
            first_started_at = None

            def sync_favorites(self, user_id: str, payload: dict) -> None:
                row = conn.execute(
                    "SELECT attempts, started_at FROM job_queue WHERE id = ?",
                    (job_id,),
                ).fetchone()
                self.first_attempts = row["attempts"]
                self.first_started_at = row["started_at"]
                conn.execute(
                    """
                    UPDATE job_queue
                    SET status = 'pending', started_at = NULL, next_run_at = NULL
                    WHERE id = ?
                    """,
                    (job_id,),
                )
                reclaimed = jobs.claim_next_job()
                assert reclaimed["id"] == job_id
                assert reclaimed["attempts"] == 2

        handlers = ReclaimingHandlers()
        assert jobs.run_next_job(handlers) is True

        row = conn.execute(
            "SELECT status, attempts FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert (row["status"], row["attempts"]) == ("running", 2)
        assert jobs.fail_job(
            job_id,
            "late failure",
            attempts=handlers.first_attempts,
            started_at=handlers.first_started_at,
        ) is False
        assert conn.execute(
            "SELECT status FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()["status"] == "running"


def test_completed_schema_reindex_replay_is_a_safe_noop() -> None:
    from src.embedding import indexer

    with isolated_jobs_db() as conn:
        conn.execute(
            """
            INSERT INTO search_reindex_state (
                user_id, content_kind, required_at, reason, completed_at
            ) VALUES ('alice', 'favorites', ?, 'schema_rebuilt', ?)
            """,
            (datetime.now(timezone.utc), datetime.now(timezone.utc)),
        )
        job_id = jobs.enqueue_job(
            "index",
            user_id="alice",
            payload={
                "content_kind": "favorites",
                "force": True,
                "schema_reindex": True,
            },
            max_attempts=1,
        )
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'running', attempts = 1, started_at = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc) - timedelta(hours=2), job_id),
        )

        recovered = jobs.recover_stale_running_jobs(stale_after_seconds=3600)
        row = conn.execute(
            "SELECT status, attempts, max_attempts FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert recovered == 1
        assert (row["status"], row["attempts"], row["max_attempts"]) == (
            "pending",
            1,
            2,
        )
        conn.execute(
            "UPDATE job_queue SET next_run_at = ? WHERE id = ?",
            (datetime.now(timezone.utc) - timedelta(seconds=1), job_id),
        )
        original_index_all = indexer.index_all
        indexer.index_all = lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("completed schema recovery must not rebuild again")
        )
        try:
            assert jobs.run_next_job() is True
        finally:
            indexer.index_all = original_index_all

        final = conn.execute(
            "SELECT status, attempts FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert (final["status"], final["attempts"]) == ("success", 2)


def test_run_next_job_dispatches_known_jobs_and_marks_success() -> None:
    class FakeHandlers:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        def sync_favorites(self, user_id: str, payload: dict) -> None:
            self.calls.append(("sync_favorites", user_id, payload))

        def sync_likes(self, user_id: str, payload: dict) -> None:
            self.calls.append(("sync_likes", user_id, payload))

        def index(self, user_id: str, payload: dict) -> None:
            self.calls.append(("index", user_id, payload))

        def uncollect(self, user_id: str, payload: dict) -> None:
            self.calls.append(("uncollect", user_id, payload))

        def backup_sqlite(self, user_id: str, payload: dict) -> None:
            self.calls.append(("backup_sqlite", user_id, payload))

    with isolated_jobs_db() as conn:
        handlers = FakeHandlers()
        jobs.enqueue_job("sync_favorites", user_id="alice", payload={"max_pages": 2})
        jobs.enqueue_job("sync_likes", user_id="alice", payload={"max_pages": 3})
        jobs.enqueue_job("index", user_id="alice", payload={"content_kind": "likes"})
        jobs.enqueue_job("uncollect", user_id="alice", payload={"content_kind": "favorites", "aweme_id": "a1"})
        jobs.enqueue_job("backup_sqlite", user_id="alice")

        while jobs.run_next_job(handlers):
            pass

        assert handlers.calls == [
            ("sync_favorites", "alice", {"max_pages": 2}),
            ("sync_likes", "alice", {"max_pages": 3}),
            ("index", "alice", {"content_kind": "likes"}),
            ("uncollect", "alice", {"content_kind": "favorites", "aweme_id": "a1"}),
            ("backup_sqlite", "alice", {}),
        ]
        statuses = [
            r["status"]
            for r in conn.execute("SELECT status FROM job_queue ORDER BY id").fetchall()
        ]
        assert statuses == ["success", "success", "success", "success", "success"]


def test_default_backup_sqlite_handler_writes_backup_to_payload_output_dir() -> None:
    with isolated_jobs_db() as conn, TemporaryDirectory() as tmp:
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES ('alice', 'a1', 'backup me', '2026-07-04', '2026-07-04', 0)
            """
        )

        original_exporter_get_connection = exporter.get_connection
        exporter.get_connection = lambda: conn
        try:
            jobs.DefaultJobHandlers().backup_sqlite("alice", {"output_dir": tmp})
        finally:
            exporter.get_connection = original_exporter_get_connection

        backups = list(Path(tmp).glob("recall-backup-*.db"))
        assert len(backups) == 1
        copied = sqlite3.connect(backups[0])
        try:
            count = copied.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        finally:
            copied.close()
        assert count == 1


def test_default_categorize_handler_runs_kmeans_for_requested_content_kind() -> None:
    from src.categorize import cluster as cluster_mod

    calls: list[dict] = []
    original_categorize_all = cluster_mod.categorize_all
    cluster_mod.categorize_all = lambda **kwargs: calls.append(kwargs)
    try:
        jobs.DefaultJobHandlers().categorize("alice", {"content_kind": "likes", "algo": "kmeans"})
    finally:
        cluster_mod.categorize_all = original_categorize_all

    assert calls == [
        {"algo": "kmeans", "account_id": "alice", "content_kind": "likes"},
    ]


def test_run_next_job_marks_failure_when_handler_raises() -> None:
    class FailingHandlers:
        def sync_favorites(self, user_id: str, payload: dict) -> None:
            raise RuntimeError("sync broke")

    with isolated_jobs_db() as conn:
        jobs.enqueue_job("sync_favorites", user_id="alice", max_attempts=1)

        assert jobs.run_next_job(FailingHandlers()) is True

        row = conn.execute("SELECT status, error_message FROM job_queue").fetchone()
        assert row["status"] == "failed"
        assert "sync broke" in row["error_message"]


if __name__ == "__main__":
    tests = [
        test_enqueue_and_claim_job_are_user_scoped,
        test_enqueue_job_suppresses_duplicate_open_work_with_same_payload,
        test_enqueue_job_joins_explicit_outer_transaction_without_committing,
        test_enqueue_job_allows_new_work_after_terminal_state,
        test_finish_and_fail_job_persist_terminal_state,
        test_fail_job_requeues_until_max_attempts_with_backoff,
        test_durable_search_reindex_keeps_retrying_after_attempt_budget,
        test_non_object_payload_does_not_crash_worker_or_become_durable,
        test_force_index_without_reindex_marker_still_fails_terminally,
        test_pending_reindex_marker_replaces_old_failed_job_only_once,
        test_claim_next_job_skips_pending_jobs_until_next_run_at,
        test_recover_stale_running_jobs_requeues_expired_running_work,
        test_recover_stale_running_jobs_can_recover_immediately_on_startup,
        test_recover_stale_durable_reindex_after_attempt_budget,
        test_recover_stale_running_jobs_uses_compare_and_swap,
        test_old_worker_cannot_finish_or_fail_a_reclaimed_attempt,
        test_completed_schema_reindex_replay_is_a_safe_noop,
        test_run_next_job_dispatches_known_jobs_and_marks_success,
        test_default_backup_sqlite_handler_writes_backup_to_payload_output_dir,
        test_default_categorize_handler_runs_kmeans_for_requested_content_kind,
        test_run_next_job_marks_failure_when_handler_raises,
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
