"""
Background job queue tests.

Run:
    python tests/test_jobs.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src import db
from src import exporter
from src import jobs


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
    jobs.get_connection = lambda: conn
    try:
        yield conn
    finally:
        jobs.get_connection = original_get_connection
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
        test_finish_and_fail_job_persist_terminal_state,
        test_fail_job_requeues_until_max_attempts_with_backoff,
        test_claim_next_job_skips_pending_jobs_until_next_run_at,
        test_recover_stale_running_jobs_requeues_expired_running_work,
        test_recover_stale_running_jobs_can_recover_immediately_on_startup,
        test_run_next_job_dispatches_known_jobs_and_marks_success,
        test_default_backup_sqlite_handler_writes_backup_to_payload_output_dir,
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
