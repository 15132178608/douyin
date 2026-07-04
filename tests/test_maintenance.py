"""
Maintenance center tests.

Run:
    python tests/test_maintenance.py
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from src import db
from src import jobs
from src import maintenance


def create_test_db(path: Path, *, favorite_title: str = "item") -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute("ALTER TABLE favorites ADD COLUMN category_id INTEGER")
    conn.execute("ALTER TABLE likes ADD COLUMN category_id INTEGER")
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES ('default', '本地默认用户', '2026-07-04 00:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, first_seen_at, last_seen_at, is_removed
        ) VALUES ('default', 'fav-1', ?, '2026-07-04', '2026-07-04', 0)
        """,
        (favorite_title,),
    )
    conn.commit()
    conn.close()


@contextmanager
def isolated_maintenance_db():
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY)")
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES ('default', '本地默认用户', '2026-07-04 00:00:00')
        """
    )

    original_maintenance_get_connection = maintenance.get_connection
    original_jobs_get_connection = jobs.get_connection
    maintenance.get_connection = lambda: conn
    jobs.get_connection = lambda: conn
    try:
        yield conn
    finally:
        maintenance.get_connection = original_maintenance_get_connection
        jobs.get_connection = original_jobs_get_connection
        conn.close()


def test_status_reports_last_runs_backups_and_attention_items() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        conn.execute(
            """
            INSERT INTO crawl_runs (
                user_id, started_at, finished_at, status, new_count, updated_count, removed_count
            ) VALUES ('default', '2026-07-01 09:00:00', '2026-07-01 09:03:00', 'success', 1, 2, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO like_crawl_runs (
                user_id, started_at, finished_at, status, error_message
            ) VALUES ('default', '2026-07-02 09:00:00', '2026-07-02 09:01:00', 'failed', '用户未登录')
            """
        )
        conn.execute(
            """
            INSERT INTO job_queue (user_id, kind, status, attempts, max_attempts, created_at, error_message)
            VALUES ('default', 'sync_likes', 'failed', 1, 3, '2026-07-02 09:00:00', '用户未登录')
            """
        )
        backup_dir = Path(tmp)
        older = backup_dir / "recall-backup-20260701-090000.db"
        newer = backup_dir / "recall-backup-20260703-090000.db"
        older.write_bytes(b"old")
        newer.write_bytes(b"newer")

        status = maintenance.get_maintenance_status("default", backup_dir=backup_dir)

        assert status["crawl_runs"]["favorites"]["latest"]["status"] == "success"
        assert status["crawl_runs"]["likes"]["latest"]["status"] == "failed"
        assert status["backups"]["count"] == 2
        assert status["backups"]["latest"]["name"] == "recall-backup-20260703-090000.db"
        assert status["jobs"]["failed"] == 1
        assert "failed_jobs" in status["attention_codes"]
        assert "latest_likes_crawl_failed" in status["attention_codes"]


def test_status_can_include_update_status_for_maintenance_center() -> None:
    with isolated_maintenance_db() as _conn, TemporaryDirectory() as tmp:
        status = maintenance.get_maintenance_status(
            "default",
            backup_dir=Path(tmp),
            include_update=True,
            update_status_getter=lambda: {
                "local_version": "0.1.6",
                "latest_version": "0.1.7",
                "update_available": True,
                "release_url": "https://github.com/15132178608/douyin/releases/tag/v0.1.7",
                "asset_name": "DouyinRecallSetup.exe",
                "asset_url": "https://example.test/DouyinRecallSetup.exe",
                "checked_at": "2026-07-05T00:00:00+00:00",
                "error": None,
            },
        )

        assert status["update"]["local_version"] == "0.1.6"
        assert status["update"]["latest_version"] == "0.1.7"
        assert status["update"]["update_available"] is True
        assert status["update"]["asset_name"] == "DouyinRecallSetup.exe"


def test_enqueue_full_maintenance_adds_sync_index_and_backup_jobs_in_order() -> None:
    with isolated_maintenance_db() as conn:
        job_ids = maintenance.enqueue_full_maintenance("default", max_pages=12)

        rows = conn.execute(
            "SELECT id, kind, payload_json FROM job_queue ORDER BY id"
        ).fetchall()
        assert job_ids == [row["id"] for row in rows]
        assert [row["kind"] for row in rows] == [
            "sync_favorites",
            "sync_likes",
            "index",
            "index",
            "backup_sqlite",
        ]
        assert '"max_pages": 12' in rows[0]["payload_json"]
        assert '"content_kind": "favorites"' in rows[2]["payload_json"]
        assert '"content_kind": "likes"' in rows[3]["payload_json"]


def test_validate_sqlite_backup_reports_counts_and_required_tables() -> None:
    with TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "recall-backup-valid.db"
        create_test_db(backup_path, favorite_title="valid backup")

        report = maintenance.validate_sqlite_backup(backup_path)

        assert report["ok"] is True
        assert report["counts"]["favorites"] == 1
        assert report["counts"]["likes"] == 0
        assert report["required_tables_present"] is True
        assert report["integrity_check"] == "ok"


def test_validate_sqlite_backup_rejects_non_database_file() -> None:
    with TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "not-a-db.db"
        backup_path.write_text("not sqlite", encoding="utf-8")

        report = maintenance.validate_sqlite_backup(backup_path)

        assert report["ok"] is False
        assert report["errors"]


def test_restore_sqlite_backup_replaces_target_and_creates_safety_backup() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        current_db = root / "recall.db"
        restore_db = root / "recall-backup-restore.db"
        safety_dir = root / "exports"
        create_test_db(current_db, favorite_title="current")
        create_test_db(restore_db, favorite_title="restored")
        close_calls: list[str] = []

        result = maintenance.restore_sqlite_backup(
            restore_db,
            db_path=current_db,
            backup_dir=safety_dir,
            close_connection=lambda: close_calls.append("closed"),
        )

        restored = sqlite3.connect(current_db)
        safety = sqlite3.connect(result.safety_backup_path)
        try:
            restored_title = restored.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
            safety_title = safety.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            restored.close()
            safety.close()
        assert result.restored_path == current_db
        assert result.safety_backup_path.exists()
        assert restored_title == "restored"
        assert safety_title == "current"
        assert close_calls == ["closed"]


if __name__ == "__main__":
    tests = [
        test_status_reports_last_runs_backups_and_attention_items,
        test_status_can_include_update_status_for_maintenance_center,
        test_enqueue_full_maintenance_adds_sync_index_and_backup_jobs_in_order,
        test_validate_sqlite_backup_reports_counts_and_required_tables,
        test_validate_sqlite_backup_rejects_non_database_file,
        test_restore_sqlite_backup_replaces_target_and_creates_safety_backup,
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
