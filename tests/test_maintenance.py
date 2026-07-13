"""
Maintenance center tests.

Run:
    python tests/test_maintenance.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def write_delivery_manifest(
    manifest_path: Path,
    backup_path: Path,
    *,
    sha256: str | None = None,
    counts: dict[str, int] | None = None,
) -> None:
    payload = {
        "schema_version": 1,
        "ok": True,
        "evidence": {
            "pre_release_backup": {
                "ok": True,
                "backup": {
                    "path": str(backup_path),
                    "sha256": sha256 if sha256 is not None else file_sha256(backup_path),
                    "source_counts": counts or {"users": 1, "favorites": 1, "likes": 0},
                },
            }
        },
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    conn.execute("CREATE TABLE favorites_vec (id TEXT PRIMARY KEY, user_id TEXT)")
    conn.execute("CREATE TABLE likes_vec (id TEXT PRIMARY KEY, user_id TEXT)")
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


def test_status_flags_douyin_login_expired_recovery_hint() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        conn.execute(
            """
            UPDATE users
            SET douyin_nickname = '旧昵称',
                douyin_unique_id = 'old_douyin',
                douyin_profile_updated_at = '2026-07-01 09:00:00'
            WHERE id = 'default'
            """
        )
        conn.execute(
            """
            INSERT INTO like_crawl_runs (
                user_id, started_at, finished_at, status, error_message
            ) VALUES (
                'default', '2026-07-02 09:00:00', '2026-07-02 09:01:00',
                'failed', '抖音登录态失效：用户未登录'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO job_queue (
                user_id, kind, status, attempts, max_attempts, created_at, error_message
            ) VALUES (
                'default', 'sync_likes', 'failed', 3, 3,
                '2026-07-02 09:02:00', 'API 返回：用户未登录'
            )
            """
        )

        status = maintenance.get_maintenance_status("default", backup_dir=Path(tmp))

        assert status["auth"]["needs_rebind"] is True
        assert status["auth"]["status"] == "expired"
        assert status["auth"]["recovery_url"] == "/auth"
        assert status["auth"]["profile"]["nickname"] == "旧昵称"
        assert status["auth"]["latest_error"]["source"] == "sync_likes"
        assert "用户未登录" in status["auth"]["latest_error"]["message"]
        assert "douyin_login_expired" in status["attention_codes"]


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


def test_status_returns_stable_suggested_actions() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES ('default', 'fav-needs-index', '需要索引', '2026-07-06', '2026-07-06', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO job_queue (user_id, kind, status, attempts, max_attempts, created_at, error_message)
            VALUES ('default', 'sync_favorites', 'failed', 3, 3, '2026-07-06 09:00:00', '用户未登录')
            """
        )

        status = maintenance.get_maintenance_status("default", backup_dir=Path(tmp))

        actions = status["suggested_actions"]
        codes = [action["code"] for action in actions]
        assert {"code", "label", "description", "target"} <= set(actions[0])
        assert "review_failed_jobs" in codes
        assert "rebind_douyin" in codes
        assert "create_backup" in codes
        assert "index_favorites" in codes


def test_status_recovers_stale_running_jobs_before_reporting_queue_state() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        job_id = jobs.enqueue_job("sync_favorites", user_id="default", payload={"max_pages": 1})
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'running', started_at = ?, attempts = 1
            WHERE id = ?
            """,
            (datetime.now(timezone.utc) - timedelta(hours=2), job_id),
        )

        status = maintenance.get_maintenance_status("default", backup_dir=Path(tmp))

        row = conn.execute(
            "SELECT status, started_at, next_run_at, error_message FROM job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert status["jobs"]["running"] == 0
        assert status["jobs"]["pending"] == 1
        assert status["sections"]["failed_tasks"]["ok"] is True
        assert row["status"] == "pending"
        assert row["started_at"] is None
        assert "stale running job recovered" in row["error_message"]


def test_status_reports_queue_recovery_retrying_and_terminal_failures() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        stale_id = jobs.enqueue_job("sync_favorites", user_id="default", payload={"max_pages": 1})
        retry_id = jobs.enqueue_job("sync_likes", user_id="default", payload={"max_pages": 1})
        failed_id = jobs.enqueue_job("index", user_id="default", payload={"content_kind": "favorites"}, max_attempts=1)
        conn.execute(
            """
            UPDATE job_queue
            SET status = 'running', started_at = ?, attempts = 1
            WHERE id = ?
            """,
            (datetime.now(timezone.utc) - timedelta(hours=2), stale_id),
        )
        claimed_retry = jobs.claim_next_job()
        assert claimed_retry["id"] == retry_id
        jobs.fail_job(retry_id, "temporary network failure")
        claimed_failed = jobs.claim_next_job()
        assert claimed_failed["id"] == failed_id
        jobs.fail_job(failed_id, "permanent index failure")

        status = maintenance.get_maintenance_status("default", backup_dir=Path(tmp))

        failed_tasks = status["sections"]["failed_tasks"]["details"]
        retrying_ids = {item["id"] for item in failed_tasks["retrying_items"]}
        failed_items = {item["id"]: item for item in failed_tasks["items"]}
        assert status["jobs"]["recovered_stale_running"] == 1
        assert status["jobs"]["retrying"] == 2
        assert status["jobs"]["next_run_at"] is not None
        assert failed_tasks["recovered_stale_running"] == 1
        assert failed_tasks["retrying_count"] == 2
        assert failed_tasks["next_run_at"] == status["jobs"]["next_run_at"]
        assert stale_id in retrying_ids
        assert retry_id in retrying_ids
        assert failed_items[failed_id]["can_retry"] is False
        assert failed_items[failed_id]["next_run_at"] is None


def test_status_exposes_stable_backend_sections_for_page_reuse() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES ('default', 'fav-needs-index', '需要索引', '2026-07-06', '2026-07-06', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO job_queue (
                user_id, kind, payload_json, status, attempts, max_attempts,
                created_at, finished_at, error_message
            ) VALUES (
                'default', 'sync_favorites', '{"max_pages": 1}', 'failed', 3, 3,
                '2026-07-06 09:00:00', '2026-07-06 09:01:00', '用户未登录'
            )
            """
        )

        status = maintenance.get_maintenance_status("default", backup_dir=Path(tmp))

        assert status["schema_version"] == 1
        sections = status["sections"]
        assert set(sections) == {
            "service",
            "login",
            "failed_tasks",
            "backup",
            "index",
            "actions",
        }
        for section in sections.values():
            assert {"status", "ok", "message", "details"} <= set(section)
            assert isinstance(section["status"], str)
            assert isinstance(section["ok"], bool)
            assert isinstance(section["message"], str)
            assert isinstance(section["details"], dict)

        failed_tasks = sections["failed_tasks"]
        assert failed_tasks["status"] == "failed"
        assert failed_tasks["ok"] is False
        assert failed_tasks["details"]["count"] == 1
        assert failed_tasks["details"]["items"][0]["id"] > 0
        assert failed_tasks["details"]["items"][0]["kind"] == "sync_favorites"
        assert "用户未登录" in failed_tasks["details"]["items"][0]["error_message"]

        index = sections["index"]
        assert index["status"] == "needs_index"
        assert index["ok"] is False
        assert index["details"]["needs_index"] == ["favorites"]
        assert index["details"]["contents"]["favorites"]["total"] == 1

        backup = sections["backup"]
        assert backup["status"] == "missing"
        assert backup["ok"] is False
        assert backup["details"]["count"] == 0

        action_codes = [action["code"] for action in sections["actions"]["details"]["items"]]
        assert "review_failed_jobs" in action_codes
        assert "create_backup" in action_codes
        assert "index_favorites" in action_codes


def test_status_exposes_backend_capabilities_contract_for_page_reuse() -> None:
    with isolated_maintenance_db() as conn, TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        backup_path = backup_dir / "recall-backup-20260707-090000.db"
        backup_path.write_bytes(b"backup")
        conn.execute(
            """
            INSERT INTO favorites (
                user_id, id, title, first_seen_at, last_seen_at, is_removed
            ) VALUES ('default', 'fav-needs-index', '需要索引', '2026-07-07', '2026-07-07', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO job_queue (
                user_id, kind, payload_json, status, attempts, max_attempts,
                created_at, finished_at, error_message
            ) VALUES (
                'default', 'sync_favorites', '{"max_pages": 1}', 'failed', 3, 3,
                '2026-07-07 09:00:00', '2026-07-07 09:01:00', '用户未登录'
            )
            """
        )

        status = maintenance.get_maintenance_status("default", backup_dir=backup_dir)

        assert status["capabilities_schema_version"] == 1
        capabilities = status["capabilities"]
        assert set(capabilities) == {
            "service_status",
            "login_status",
            "failed_tasks",
            "backup_status",
            "index_status",
            "suggested_actions",
        }
        for name, capability in capabilities.items():
            if name == "suggested_actions":
                continue
            assert {"status", "ok", "message"} <= set(capability)
            assert isinstance(capability["status"], str)
            assert isinstance(capability["ok"], bool)
            assert isinstance(capability["message"], str)

        assert capabilities["service_status"]["details"]["state"] in {"running", "stopped", "stale", "unknown"}
        assert capabilities["login_status"]["needs_rebind"] is True
        assert capabilities["login_status"]["recovery_url"] == "/auth"
        assert capabilities["failed_tasks"]["count"] == 1
        assert capabilities["failed_tasks"]["items"][0]["kind"] == "sync_favorites"
        assert capabilities["backup_status"]["count"] == 1
        assert capabilities["backup_status"]["latest"]["name"] == backup_path.name
        assert capabilities["index_status"]["needs_index"] == ["favorites"]
        assert capabilities["index_status"]["contents"]["favorites"]["total"] == 1
        action_codes = [action["code"] for action in capabilities["suggested_actions"]]
        assert action_codes == [action["code"] for action in status["suggested_actions"]]
        assert {"code", "label", "description", "target", "severity"} <= set(
            capabilities["suggested_actions"][0]
        )
        assert "rebind_douyin" in action_codes
        assert "index_favorites" in action_codes


def test_status_reports_backup_retention_policy() -> None:
    with isolated_maintenance_db() as _conn, TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        for stamp in ("20260701-090000", "20260702-090000", "20260703-090000"):
            (backup_dir / f"recall-backup-{stamp}.db").write_bytes(stamp.encode("ascii"))
        (backup_dir / "pre-install-recall-20260704-090000.db").write_bytes(b"protected")
        (backup_dir / "pre-restore-recall-20260705-090000.db").write_bytes(b"protected")
        (backup_dir / "pre-release-recall-20260706-090000.db").write_bytes(b"protected")

        status = maintenance.get_maintenance_status("default", backup_dir=backup_dir)

        retention = status["backups"]["retention"]
        assert retention["keep_latest"] == maintenance.DEFAULT_BACKUP_RETENTION_KEEP
        assert retention["ordinary_count"] == 3
        assert retention["protected_count"] == 3
        assert retention["delete_candidates"] == []
        assert retention["protected_patterns"] == [
            "pre-install-recall-*.db",
            "pre-restore-recall-*.db",
            "pre-release-recall-*.db",
        ]


def test_enforce_backup_retention_deletes_only_old_ordinary_backups_and_reports_paths() -> None:
    with TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        older = backup_dir / "recall-backup-20260701-090000.db"
        middle = backup_dir / "recall-backup-20260702-090000.db"
        newer = backup_dir / "recall-backup-20260703-090000.db"
        protected_install = backup_dir / "pre-install-recall-20260700-090000.db"
        protected_restore = backup_dir / "pre-restore-recall-20260700-090000.db"
        protected_release = backup_dir / "pre-release-recall-20260700-090000.db"
        for path in (older, middle, newer, protected_install, protected_restore, protected_release):
            path.write_bytes(path.name.encode("utf-8"))

        report = maintenance.enforce_backup_retention(backup_dir, keep_latest=2)

        assert report["ok"] is True
        assert [Path(item["path"]).name for item in report["deleted"]] == [older.name]
        assert older.exists() is False
        assert middle.exists() is True
        assert newer.exists() is True
        assert protected_install.exists() is True
        assert protected_restore.exists() is True
        assert protected_release.exists() is True
        assert {Path(item["path"]).name for item in report["protected"]} == {
            protected_install.name,
            protected_restore.name,
            protected_release.name,
        }
        assert report["delete_method"] == "one_file_at_a_time"


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


def test_validate_and_restore_sqlite_backup_include_committed_wal_rows() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_path = root / "recall-backup-wal.db"
        target_path = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(backup_path, favorite_title="base")
        create_test_db(target_path, favorite_title="current")
        writer = sqlite3.connect(backup_path)
        try:
            assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute(
                """
                INSERT INTO favorites(
                    user_id, id, title, first_seen_at, last_seen_at, is_removed
                ) VALUES (
                    'default', 'wal-only', 'committed in WAL',
                    '2026-07-05', '2026-07-05', 0
                )
                """
            )
            writer.commit()
            assert Path(str(backup_path) + "-wal").stat().st_size > 0

            validation = maintenance.validate_sqlite_backup(backup_path)
            result = maintenance.restore_sqlite_backup(
                backup_path,
                db_path=target_path,
                backup_dir=safety_dir,
            )

            restored = sqlite3.connect(target_path)
            try:
                restored_count = restored.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
                restored_title = restored.execute(
                    "SELECT title FROM favorites WHERE id = 'wal-only'"
                ).fetchone()[0]
                violations = restored.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                restored.close()
        finally:
            writer.close()

        assert validation["ok"] is True
        assert validation["counts"]["favorites"] == 2
        assert validation["sidecars"]["-wal"]["size_bytes"] > 0
        assert result.restored_path == target_path
        assert restored_count == 2
        assert restored_title == "committed in WAL"
        assert violations == []


def test_list_recovery_backups_includes_manual_and_preinstall_backups() -> None:
    with TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        manual = backup_dir / "recall-backup-20260704-100000.db"
        preinstall = backup_dir / "pre-install-recall-20260705-100000.db"
        prerelease = backup_dir / "pre-release-recall-20260706-100000.db"
        ignored = backup_dir / "notes.txt"
        manual.write_bytes(b"manual")
        preinstall.write_bytes(b"preinstall")
        prerelease.write_bytes(b"prerelease")
        ignored.write_text("ignore", encoding="utf-8")

        items = maintenance.list_recovery_backups(backup_dir, limit=4)

        assert [item.name for item in items] == [
            "pre-release-recall-20260706-100000.db",
            "pre-install-recall-20260705-100000.db",
            "recall-backup-20260704-100000.db",
        ]


def test_verify_latest_backup_validates_newest_recovery_backup() -> None:
    with TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        older = backup_dir / "recall-backup-20260704-100000.db"
        newer = backup_dir / "pre-install-recall-20260705-100000.db"
        create_test_db(older, favorite_title="older")
        create_test_db(newer, favorite_title="newer")

        report = maintenance.verify_latest_backup(backup_dir)

        assert report["ok"] is True
        assert report["backup"]["name"] == "pre-install-recall-20260705-100000.db"
        assert report["validation"]["counts"]["favorites"] == 1
        assert report["validation"]["integrity_check"] == "ok"
        assert report["errors"] == []


def test_verify_latest_backup_reports_no_backups() -> None:
    with TemporaryDirectory() as tmp:
        report = maintenance.verify_latest_backup(Path(tmp))

        assert report["ok"] is False
        assert report["backup"] is None
        assert "没有找到可校验的备份文件。" in report["errors"]


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

        assert Path(str(current_db) + "-wal").exists() is False
        assert Path(str(current_db) + "-shm").exists() is False
        restored = sqlite3.connect(current_db)
        safety = sqlite3.connect(result.safety_backup_path)
        try:
            restored_title = restored.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
            restored_journal_mode = restored.execute("PRAGMA journal_mode").fetchone()[0]
            safety_title = safety.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            restored.close()
            safety.close()
        assert result.restored_path == current_db
        assert result.safety_backup_path.exists()
        assert restored_title == "restored"
        assert restored_journal_mode == "delete"
        assert safety_title == "current"
        assert close_calls == ["closed"]


def test_restore_captures_write_committed_by_close_callback_in_safety_backup() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-late-write.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="restored")
        create_test_db(target_db, favorite_title="current")

        def close_with_late_commit() -> None:
            conn = sqlite3.connect(target_db)
            try:
                conn.execute(
                    """
                    INSERT INTO favorites(
                        user_id, id, title, first_seen_at, last_seen_at, is_removed
                    ) VALUES (
                        'default', 'late-target-write', 'late',
                        '2026-07-05', '2026-07-05', 0
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

        result = maintenance.restore_sqlite_backup(
            source_db,
            db_path=target_db,
            backup_dir=safety_dir,
            close_connection=close_with_late_commit,
        )

        assert result.safety_backup_path is not None
        safety = sqlite3.connect(result.safety_backup_path)
        restored = sqlite3.connect(target_db)
        try:
            assert safety.execute(
                "SELECT COUNT(*) FROM favorites WHERE id = 'late-target-write'"
            ).fetchone()[0] == 1
            assert restored.execute(
                "SELECT COUNT(*) FROM favorites WHERE id = 'late-target-write'"
            ).fetchone()[0] == 0
        finally:
            safety.close()
            restored.close()


def test_restore_migratable_target_safety_validation_bypasses_live_connection_gate() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-current.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="restored")
        create_test_db(target_db, favorite_title="migratable current")
        target = sqlite3.connect(target_db)
        try:
            target.execute("ALTER TABLE favorites DROP COLUMN category_id")
            target.commit()
        finally:
            target.close()

        original_wait = db._CONNECTION_GATE.wait

        def fail_if_bound_connection_waits(*_args, **_kwargs):
            raise AssertionError("isolated migration connection waited on the live DB gate")

        db._CONNECTION_GATE.wait = fail_if_bound_connection_waits
        try:
            result = maintenance.restore_sqlite_backup(
                source_db,
                db_path=target_db,
                backup_dir=safety_dir,
            )
        finally:
            db._CONNECTION_GATE.wait = original_wait

        assert result.safety_backup_path is not None
        assert result.safety_backup_path.suffix == ".db"
        safety_validation = maintenance.validate_sqlite_backup(result.safety_backup_path)
        assert safety_validation["ok"] is True
        assert safety_validation["schema_migration_required"] is True
        restored = sqlite3.connect(target_db)
        try:
            assert restored.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "restored"
        finally:
            restored.close()


def test_restore_valid_source_over_corrupt_target_preserves_forensic_copy() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-valid.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="restored")
        target_db.write_bytes(b"not a sqlite database")

        result = maintenance.restore_sqlite_backup(
            source_db,
            db_path=target_db,
            backup_dir=safety_dir,
        )

        assert result.safety_backup_path is not None
        assert result.safety_backup_path.suffix == ".corrupt"
        assert result.safety_backup_path.read_bytes() == b"not a sqlite database"
        restored = sqlite3.connect(target_db)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "restored"
        finally:
            restored.close()
        assert list(safety_dir.glob("pre-restore-recall-*.db")) == []


def test_restore_to_missing_target_does_not_create_empty_safety_backup() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-new-target.db"
        target_db = root / "missing" / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="restored")

        result = maintenance.restore_sqlite_backup(
            source_db,
            db_path=target_db,
            backup_dir=safety_dir,
        )

        assert result.safety_backup_path is None
        assert target_db.exists()
        assert safety_dir.exists() is False


def test_restore_to_missing_target_removes_orphan_journal_before_cutover() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-orphan-journal.db"
        target_db = root / "missing" / "recall.db"
        target_db.parent.mkdir(parents=True)
        orphan_journal = Path(str(target_db) + "-journal")
        create_test_db(source_db, favorite_title="restored")
        create_test_db(target_db, favorite_title="old target")
        crash_writer = r"""
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode = DELETE")
conn.execute("PRAGMA synchronous = FULL")
conn.execute("PRAGMA cache_size = 1")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE favorites SET title = 'dirty' WHERE id = 'fav-1'")
for _ in range(1000):
    conn.execute(
        "INSERT INTO job_queue("
        "user_id, kind, payload_json, status, created_at, next_run_at"
        ") VALUES ("
        "'default', 'x', '{}', 'queued', '2026-01-01', '2026-01-01'"
        ")"
    )
os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-c", crash_writer, str(target_db)],
            check=True,
        )
        assert orphan_journal.exists()
        assert orphan_journal.stat().st_size > 512
        target_db.unlink()

        result = maintenance.restore_sqlite_backup(
            source_db,
            db_path=target_db,
            backup_dir=root / "exports",
        )

        assert result.safety_backup_path is None
        assert orphan_journal.exists() is False
        assert list(target_db.parent.glob(".recall.db.restore-sidecar-*")) == []
        restored = sqlite3.connect(target_db)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "restored"
        finally:
            restored.close()


def test_restore_zero_byte_target_creates_forensic_not_protected_backup() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-valid.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="restored")
        target_db.write_bytes(b"")

        result = maintenance.restore_sqlite_backup(
            source_db,
            db_path=target_db,
            backup_dir=safety_dir,
        )

        assert result.safety_backup_path is not None
        assert result.safety_backup_path.suffix == ".corrupt"
        assert result.safety_backup_path.read_bytes() == b""
        assert list(safety_dir.glob("pre-restore-recall-*.db")) == []
        restored = sqlite3.connect(target_db)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()


def test_restore_rejects_orphan_before_touching_target() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-orphan.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="bad source")
        create_test_db(target_db, favorite_title="current")
        source = sqlite3.connect(source_db, isolation_level=None)
        try:
            source.execute("PRAGMA foreign_keys = OFF")
            source.execute(
                """
                INSERT INTO recall_log(user_id, favorite_id, recalled_at)
                VALUES ('default', 'missing-item', '2026-07-05')
                """
            )
        finally:
            source.close()
        close_calls: list[str] = []

        try:
            maintenance.restore_sqlite_backup(
                source_db,
                db_path=target_db,
                backup_dir=safety_dir,
                close_connection=lambda: close_calls.append("closed"),
            )
        except ValueError as exc:
            assert "foreign_key_check" in str(exc)
        else:
            raise AssertionError("orphan backup must be rejected")

        current = sqlite3.connect(target_db)
        try:
            title = current.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
            assert current.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            current.close()
        assert title == "current"
        assert close_calls == []
        assert safety_dir.exists() is False


def test_restore_atomic_replace_failure_keeps_target_and_cleans_stage() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-replace-failure.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="replacement")
        create_test_db(target_db, favorite_title="current")
        original_replace = maintenance.os.replace
        maintenance.os.replace = lambda _source, _target: (_ for _ in ()).throw(
            PermissionError("simulated replace failure")
        )
        try:
            try:
                maintenance.restore_sqlite_backup(
                    source_db,
                    db_path=target_db,
                    backup_dir=safety_dir,
                )
            except PermissionError as exc:
                assert "simulated replace failure" in str(exc)
            else:
                raise AssertionError("replace failure must escape")
        finally:
            maintenance.os.replace = original_replace

        current = sqlite3.connect(target_db)
        try:
            title = current.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
            assert current.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            current.close()
        safety_backups = list(safety_dir.glob("pre-restore-recall-*.db"))
        assert title == "current"
        assert len(safety_backups) == 1
        assert list(root.glob(".recall.db.restore-*.db")) == []


def test_restore_replace_failure_rolls_quarantined_sidecar_back() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-replace-failure.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        sidecar = Path(str(target_db) + "-journal")
        create_test_db(source_db, favorite_title="replacement")
        target_db.write_bytes(b"not a sqlite database")
        sidecar.write_bytes(b"stale journal")
        original_replace = maintenance.os.replace

        def fail_main_replace(source, destination):
            if Path(destination) == target_db and ".restore-" in Path(source).name:
                raise PermissionError("simulated main-file replace failure")
            return original_replace(source, destination)

        maintenance.os.replace = fail_main_replace
        try:
            try:
                maintenance.restore_sqlite_backup(
                    source_db,
                    db_path=target_db,
                    backup_dir=safety_dir,
                )
            except PermissionError as exc:
                assert "main-file replace failure" in str(exc)
            else:
                raise AssertionError("main-file replace failure must escape")
        finally:
            maintenance.os.replace = original_replace

        assert target_db.read_bytes() == b"not a sqlite database"
        assert sidecar.read_bytes() == b"stale journal"
        assert list(root.glob(".recall.db.restore-sidecar-*")) == []


def test_restore_preserves_both_errors_and_forensic_backup_when_sidecar_rollback_fails() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-double-failure.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        sidecar = Path(str(target_db) + "-journal")
        create_test_db(source_db, favorite_title="replacement")
        target_db.write_bytes(b"not a sqlite database")
        sidecar.write_bytes(b"stale journal")
        original_replace = maintenance.os.replace

        def fail_main_and_rollback(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path == target_db and ".restore-" in source_path.name:
                raise PermissionError("simulated main-file replace failure")
            if ".restore-sidecar-" in source_path.name and destination_path == sidecar:
                raise PermissionError("simulated sidecar rollback failure")
            return original_replace(source, destination)

        maintenance.os.replace = fail_main_and_rollback
        try:
            try:
                maintenance.restore_sqlite_backup(
                    source_db,
                    db_path=target_db,
                    backup_dir=safety_dir,
                )
            except ExceptionGroup as exc:
                combined = exc
            else:
                raise AssertionError("double filesystem failure must escape")
        finally:
            maintenance.os.replace = original_replace

        assert "主文件替换失败" in str(combined)
        assert len(combined.exceptions) == 2
        assert "main-file replace failure" in str(combined.exceptions[0])
        assert "sidecar rollback failure" in str(combined.exceptions[1])
        forensic = list(safety_dir.glob("pre-restore-recall-*.corrupt"))
        assert len(forensic) == 1
        assert forensic[0].read_bytes() == b"not a sqlite database"
        assert Path(str(forensic[0]) + "-journal").read_bytes() == b"stale journal"
        assert target_db.read_bytes() == b"not a sqlite database"
        assert sidecar.exists() is False
        assert len(list(root.glob(".recall.db.restore-sidecar-*-journal"))) == 1


def test_restore_reports_forensic_path_when_sidecar_move_and_rollback_both_fail() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-isolation-double-failure.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        wal_sidecar = Path(str(target_db) + "-wal")
        journal_sidecar = Path(str(target_db) + "-journal")
        create_test_db(source_db, favorite_title="replacement")
        target_db.write_bytes(b"not a sqlite database")
        wal_sidecar.write_bytes(b"stale wal")
        journal_sidecar.write_bytes(b"stale journal")
        original_replace = maintenance.os.replace

        def fail_second_move_and_first_rollback(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path == journal_sidecar and ".restore-sidecar-" in destination_path.name:
                raise PermissionError("simulated sidecar move failure")
            if ".restore-sidecar-" in source_path.name and destination_path == wal_sidecar:
                raise PermissionError("simulated sidecar rollback failure")
            return original_replace(source, destination)

        maintenance.os.replace = fail_second_move_and_first_rollback
        try:
            try:
                maintenance.restore_sqlite_backup(
                    source_db,
                    db_path=target_db,
                    backup_dir=safety_dir,
                )
            except ExceptionGroup as exc:
                combined = exc
            else:
                raise AssertionError("sidecar isolation double failure must escape")
        finally:
            maintenance.os.replace = original_replace

        forensic = list(safety_dir.glob("pre-restore-recall-*.corrupt"))
        assert len(forensic) == 1
        assert "sidecar 隔离失败" in str(combined)
        assert "安全备份/取证副本" in str(combined)
        assert str(forensic[0]) in str(combined)
        assert len(combined.exceptions) == 2
        assert "sidecar move failure" in str(combined.exceptions[0])
        assert "sidecar rollback failure" in str(combined.exceptions[1])
        assert forensic[0].read_bytes() == b"not a sqlite database"
        assert Path(str(forensic[0]) + "-wal").read_bytes() == b"stale wal"
        assert Path(str(forensic[0]) + "-journal").read_bytes() == b"stale journal"
        assert target_db.read_bytes() == b"not a sqlite database"
        assert wal_sidecar.exists() is False
        assert journal_sidecar.read_bytes() == b"stale journal"
        assert len(list(root.glob(".recall.db.restore-sidecar-*-wal"))) == 1


def test_restore_reports_cleanup_warning_after_committed_replacement() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-cleanup-warning.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        sidecar = Path(str(target_db) + "-journal")
        create_test_db(source_db, favorite_title="replacement")
        target_db.write_bytes(b"not a sqlite database")
        sidecar.write_bytes(b"stale journal")
        original_unlink = Path.unlink

        def fail_quarantine_cleanup(path, *args, **kwargs):
            if ".restore-sidecar-" in path.name:
                raise PermissionError("simulated quarantine cleanup failure")
            return original_unlink(path, *args, **kwargs)

        Path.unlink = fail_quarantine_cleanup
        try:
            result = maintenance.restore_sqlite_backup(
                source_db,
                db_path=target_db,
                backup_dir=safety_dir,
            )
        finally:
            Path.unlink = original_unlink

        assert result.cleanup_warnings
        assert "恢复已完成" in result.cleanup_warnings[0]
        assert sidecar.exists() is False
        quarantined = list(root.glob(".recall.db.restore-sidecar-*-journal"))
        assert len(quarantined) == 1
        restored = sqlite3.connect(target_db)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "replacement"
        finally:
            restored.close()


def test_restore_stage_cleanup_failure_does_not_report_committed_restore_as_failed() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-stage-cleanup.db"
        target_db = root / "recall.db"
        create_test_db(source_db, favorite_title="replacement")
        create_test_db(target_db, favorite_title="current")
        original_replace = maintenance.os.replace
        original_unlink = Path.unlink

        def replace_with_stage_sidecar(source, destination):
            source_path = Path(source)
            if Path(destination) == target_db and ".restore-" in source_path.name:
                Path(str(source_path) + "-journal").write_bytes(b"staged cleanup residue")
            return original_replace(source, destination)

        def fail_stage_cleanup(path, *args, **kwargs):
            if ".restore-" in path.name and path.name.endswith(".db-journal"):
                raise PermissionError("simulated stage cleanup failure")
            return original_unlink(path, *args, **kwargs)

        maintenance.os.replace = replace_with_stage_sidecar
        Path.unlink = fail_stage_cleanup
        try:
            result = maintenance.restore_sqlite_backup(
                source_db,
                db_path=target_db,
                backup_dir=root / "exports",
            )
        finally:
            maintenance.os.replace = original_replace
            Path.unlink = original_unlink

        assert result.cleanup_warnings
        assert any("恢复临时文件" in warning for warning in result.cleanup_warnings)
        assert len(list(root.glob(".recall.db.restore-*.db-journal"))) == 1
        restored = sqlite3.connect(target_db)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "replacement"
        finally:
            restored.close()


def test_validate_and_restore_reject_database_missing_required_content_column() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "recall-backup-missing-column.db"
        target_db = root / "recall.db"
        safety_dir = root / "exports"
        create_test_db(source_db, favorite_title="bad source")
        create_test_db(target_db, favorite_title="current")
        source = sqlite3.connect(source_db)
        try:
            source.execute("ALTER TABLE favorites DROP COLUMN description")
            source.commit()
        finally:
            source.close()

        validation = maintenance.validate_sqlite_backup(source_db)
        try:
            maintenance.restore_sqlite_backup(
                source_db,
                db_path=target_db,
                backup_dir=safety_dir,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("backup missing an application column must be rejected")

        assert validation["ok"] is False
        assert validation["schema_migration_required"] is True
        assert validation["migration_validation"]["schema_current"] is False
        current = sqlite3.connect(target_db)
        try:
            assert current.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "current"
        finally:
            current.close()
        assert safety_dir.exists() is False


def test_validate_rejects_missing_auth_and_reindex_state_columns() -> None:
    cases = (
        ("web_sessions", "expires_at", None),
        ("search_reindex_state", "completed_at", "idx_search_reindex_pending"),
    )
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        for table, column, dependent_index in cases:
            case_root = root / table
            case_root.mkdir()
            source_db = case_root / "invalid.db"
            target_db = case_root / "current.db"
            create_test_db(source_db, favorite_title=f"invalid {table}")
            create_test_db(target_db, favorite_title="current")
            source = sqlite3.connect(source_db)
            try:
                if dependent_index:
                    source.execute(f"DROP INDEX {dependent_index}")
                source.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                source.commit()
            finally:
                source.close()

            validation = maintenance.validate_sqlite_backup(source_db)
            try:
                maintenance.restore_sqlite_backup(
                    source_db,
                    db_path=target_db,
                    backup_dir=case_root / "exports",
                )
            except ValueError:
                pass
            else:
                raise AssertionError(f"{table}.{column} omission must be rejected")

            assert validation["ok"] is False
            assert validation["schema_migration_required"] is True
            assert validation["migration_validation"]["schema_current"] is False
            current = sqlite3.connect(target_db)
            try:
                assert current.execute(
                    "SELECT title FROM favorites WHERE id = 'fav-1'"
                ).fetchone()[0] == "current"
            finally:
                current.close()


def test_validate_rejects_login_rate_limit_table_without_composite_primary_key() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_db = root / "invalid-rate-limit.db"
        create_test_db(source_db, favorite_title="invalid rate limits")
        source = sqlite3.connect(source_db)
        try:
            source.execute("DROP TABLE login_rate_limits")
            source.execute(
                """
                CREATE TABLE login_rate_limits (
                    scope TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    window_started_at TIMESTAMP NOT NULL,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    blocked_until TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            source.commit()
        finally:
            source.close()

        validation = maintenance.validate_sqlite_backup(source_db)

        assert validation["ok"] is False
        assert validation["schema_migration_required"] is True
        assert validation["migration_validation"]["schema_current"] is False


def test_validate_delivery_manifest_backup_checks_sha_and_counts() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-valid.db"
        manifest_path = root / "delivery-manifest-valid.json"
        create_test_db(backup_db, favorite_title="manifest backup")
        write_delivery_manifest(manifest_path, backup_db)

        report = maintenance.validate_delivery_manifest_backup(manifest_path)

        assert report["ok"] is True
        assert report["manifest_path"] == str(manifest_path)
        assert report["backup"]["path"] == str(backup_db)
        assert report["backup"]["expected_sha256"] == file_sha256(backup_db)
        assert report["backup"]["actual_sha256"] == file_sha256(backup_db)
        assert report["backup"]["expected_counts"] == {"users": 1, "favorites": 1, "likes": 0}
        assert report["backup"]["backup_counts"] == {"users": 1, "favorites": 1, "likes": 0}
        assert report["backup"]["validation"]["ok"] is True


def test_validate_delivery_manifest_backup_rejects_sha_mismatch() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-sha.db"
        manifest_path = root / "delivery-manifest-sha.json"
        create_test_db(backup_db, favorite_title="manifest backup")
        write_delivery_manifest(manifest_path, backup_db, sha256="bad-sha")

        report = maintenance.validate_delivery_manifest_backup(manifest_path)

        assert report["ok"] is False
        assert any("SHA256" in error for error in report["errors"])


def test_validate_delivery_manifest_backup_requires_sha_and_valid_counts() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-invalid-manifest.db"
        manifest_path = root / "delivery-manifest-invalid.json"
        create_test_db(backup_db, favorite_title="manifest backup")
        write_delivery_manifest(manifest_path, backup_db)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup = payload["evidence"]["pre_release_backup"]["backup"]
        backup.pop("sha256")
        backup["source_counts"] = {"users": "not-an-integer"}
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        report = maintenance.validate_delivery_manifest_backup(manifest_path)

        assert report["ok"] is False
        assert any("sha256" in error.lower() for error in report["errors"])
        assert any("数量无效" in error for error in report["errors"])


def test_validate_delivery_manifest_backup_requires_all_core_count_keys() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-missing-count.db"
        manifest_path = root / "delivery-manifest-missing-count.json"
        create_test_db(backup_db, favorite_title="manifest backup")
        write_delivery_manifest(
            manifest_path,
            backup_db,
            counts={"users": 1, "favorites": 1},
        )

        report = maintenance.validate_delivery_manifest_backup(manifest_path)

        assert report["ok"] is False
        assert any("缺少关键表数量：likes" in error for error in report["errors"])


def test_validate_delivery_manifest_backup_rejects_nonempty_wal_sidecar() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-wal.db"
        manifest_path = root / "delivery-manifest-wal.json"
        create_test_db(backup_db, favorite_title="manifest backup")
        writer = sqlite3.connect(backup_db)
        try:
            assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute(
                """
                INSERT INTO favorites(
                    user_id, id, title, first_seen_at, last_seen_at, is_removed
                ) VALUES (
                    'default', 'wal-manifest-item', 'WAL item',
                    '2026-07-05', '2026-07-05', 0
                )
                """
            )
            writer.commit()
            write_delivery_manifest(
                manifest_path,
                backup_db,
                counts={"users": 1, "favorites": 2, "likes": 0},
            )

            report = maintenance.validate_delivery_manifest_backup(manifest_path)
        finally:
            writer.close()

        assert report["ok"] is False
        assert any("sidecar" in error and "-wal" in error for error in report["errors"])


def test_restore_from_delivery_manifest_rejects_source_changed_after_validation() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-raced.db"
        current_db = root / "current.db"
        safety_dir = root / "safety"
        manifest_path = root / "delivery-manifest-raced.json"
        create_test_db(backup_db, favorite_title="manifest restored")
        create_test_db(current_db, favorite_title="current")

        setup = sqlite3.connect(backup_db)
        try:
            assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        finally:
            setup.close()
        write_delivery_manifest(manifest_path, backup_db)

        original_restore = maintenance.restore_sqlite_backup

        def restore_after_concurrent_write(*args, **kwargs):
            writer = sqlite3.connect(backup_db)
            try:
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute(
                    """
                    INSERT INTO favorites(
                        user_id, id, title, first_seen_at, last_seen_at, is_removed
                    ) VALUES (
                        'default', 'post-validation-item', 'Unverified item',
                        '2026-07-06', '2026-07-06', 0
                    )
                    """
                )
                writer.commit()
                return original_restore(*args, **kwargs)
            finally:
                writer.close()

        maintenance.restore_sqlite_backup = restore_after_concurrent_write
        try:
            report = maintenance.restore_from_delivery_manifest(
                manifest_path,
                db_path=current_db,
                backup_dir=safety_dir,
                apply=True,
            )
        finally:
            maintenance.restore_sqlite_backup = original_restore

        current = sqlite3.connect(current_db)
        try:
            assert current.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0] == "current"
            assert current.execute(
                "SELECT COUNT(*) FROM favorites WHERE id = 'post-validation-item'"
            ).fetchone()[0] == 0
        finally:
            current.close()
        assert report["ok"] is False
        assert report["restored"] is False
        assert any(
            marker in " ".join(report["errors"]).lower()
            for marker in ("sidecar", "sha256", "self-contained")
        )
        assert safety_dir.exists() is False


def test_restore_from_delivery_manifest_expands_unique_exception_group_leaves() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-group.db"
        manifest_path = root / "delivery-manifest-group.json"
        safety_path = root / "safety" / "pre-restore-recall-test.corrupt"
        quarantine_path = root / ".recall.db.restore-sidecar-token-journal"
        original_sidecar = root / "recall.db-journal"
        move_error = "simulated sidecar move failure"
        rollback_error = (
            f"{quarantine_path} -> {original_sidecar}: "
            "simulated sidecar rollback failure"
        )
        failure = ExceptionGroup(
            f"数据库恢复失败；安全备份/取证副本：{safety_path}",
            [
                PermissionError(move_error),
                ExceptionGroup(
                    "nested sidecar failures",
                    [RuntimeError(rollback_error), PermissionError(move_error)],
                ),
            ],
        )
        create_test_db(backup_db, favorite_title="manifest backup")
        write_delivery_manifest(manifest_path, backup_db)
        original_restore = maintenance.restore_sqlite_backup

        def raise_group(*_args, **_kwargs):
            raise failure

        maintenance.restore_sqlite_backup = raise_group
        try:
            report = maintenance.restore_from_delivery_manifest(
                manifest_path,
                db_path=root / "recall.db",
                backup_dir=root / "safety",
                apply=True,
            )
        finally:
            maintenance.restore_sqlite_backup = original_restore

        assert report["ok"] is False
        assert report["restored"] is False
        assert report["restore"] is None
        assert report["errors"][0] == str(failure)
        assert str(safety_path) in report["errors"][0]
        assert report["errors"].count(move_error) == 1
        assert report["errors"].count(rollback_error) == 1
        assert report["errors"] == [str(failure), move_error, rollback_error]


def test_restore_from_delivery_manifest_keeps_plain_exception_as_single_error() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-plain-error.db"
        manifest_path = root / "delivery-manifest-plain-error.json"
        create_test_db(backup_db, favorite_title="manifest backup")
        write_delivery_manifest(manifest_path, backup_db)
        original_restore = maintenance.restore_sqlite_backup

        def raise_plain(*_args, **_kwargs):
            raise ValueError("plain restore failure")

        maintenance.restore_sqlite_backup = raise_plain
        try:
            report = maintenance.restore_from_delivery_manifest(
                manifest_path,
                db_path=root / "recall.db",
                apply=True,
            )
        finally:
            maintenance.restore_sqlite_backup = original_restore

        assert report["ok"] is False
        assert report["restored"] is False
        assert report["errors"] == ["plain restore failure"]


def test_restore_from_delivery_manifest_dry_run_does_not_replace_target() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-dry-run.db"
        current_db = root / "current.db"
        manifest_path = root / "delivery-manifest-dry-run.json"
        create_test_db(backup_db, favorite_title="manifest restored")
        create_test_db(current_db, favorite_title="current")
        write_delivery_manifest(manifest_path, backup_db)

        report = maintenance.restore_from_delivery_manifest(
            manifest_path,
            db_path=current_db,
            apply=False,
        )

        conn = sqlite3.connect(current_db)
        try:
            title = conn.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            conn.close()
        assert report["ok"] is True
        assert report["mode"] == "dry_run"
        assert report["restored"] is False
        assert title == "current"


def test_restore_from_delivery_manifest_apply_restores_after_validation() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-apply.db"
        current_db = root / "current.db"
        safety_dir = root / "safety"
        manifest_path = root / "delivery-manifest-apply.json"
        create_test_db(backup_db, favorite_title="manifest restored")
        create_test_db(current_db, favorite_title="current")
        write_delivery_manifest(manifest_path, backup_db)

        report = maintenance.restore_from_delivery_manifest(
            manifest_path,
            db_path=current_db,
            backup_dir=safety_dir,
            apply=True,
        )

        conn = sqlite3.connect(current_db)
        try:
            title = conn.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            conn.close()
        assert report["ok"] is True
        assert report["mode"] == "apply"
        assert report["restored"] is True
        assert title == "manifest restored"
        assert Path(report["restore"]["safety_backup_path"]).exists()


def test_restore_from_delivery_manifest_accepts_clean_wal_mode_main_file() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_db = root / "pre-release-recall-clean-wal.db"
        current_db = root / "current.db"
        safety_dir = root / "safety"
        manifest_path = root / "delivery-manifest-clean-wal.json"
        create_test_db(backup_db, favorite_title="clean WAL manifest")
        create_test_db(current_db, favorite_title="current")
        setup = sqlite3.connect(backup_db)
        try:
            assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        finally:
            setup.close()
        assert not Path(str(backup_db) + "-wal").exists()
        write_delivery_manifest(manifest_path, backup_db)

        report = maintenance.restore_from_delivery_manifest(
            manifest_path,
            db_path=current_db,
            backup_dir=safety_dir,
            apply=True,
        )

        restored = sqlite3.connect(current_db)
        try:
            title = restored.execute(
                "SELECT title FROM favorites WHERE id = 'fav-1'"
            ).fetchone()[0]
        finally:
            restored.close()
        assert report["ok"] is True
        assert report["restored"] is True
        assert title == "clean WAL manifest"


if __name__ == "__main__":
    tests = [
        test_status_reports_last_runs_backups_and_attention_items,
        test_status_flags_douyin_login_expired_recovery_hint,
        test_status_can_include_update_status_for_maintenance_center,
        test_status_returns_stable_suggested_actions,
        test_status_recovers_stale_running_jobs_before_reporting_queue_state,
        test_status_reports_queue_recovery_retrying_and_terminal_failures,
        test_status_exposes_stable_backend_sections_for_page_reuse,
        test_status_exposes_backend_capabilities_contract_for_page_reuse,
        test_status_reports_backup_retention_policy,
        test_enforce_backup_retention_deletes_only_old_ordinary_backups_and_reports_paths,
        test_enqueue_full_maintenance_adds_sync_index_and_backup_jobs_in_order,
        test_validate_sqlite_backup_reports_counts_and_required_tables,
        test_validate_sqlite_backup_rejects_non_database_file,
        test_list_recovery_backups_includes_manual_and_preinstall_backups,
        test_verify_latest_backup_validates_newest_recovery_backup,
        test_verify_latest_backup_reports_no_backups,
        test_restore_sqlite_backup_replaces_target_and_creates_safety_backup,
        test_validate_delivery_manifest_backup_checks_sha_and_counts,
        test_validate_delivery_manifest_backup_rejects_sha_mismatch,
        test_restore_from_delivery_manifest_expands_unique_exception_group_leaves,
        test_restore_from_delivery_manifest_keeps_plain_exception_as_single_error,
        test_restore_from_delivery_manifest_dry_run_does_not_replace_target,
        test_restore_from_delivery_manifest_apply_restores_after_validation,
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
