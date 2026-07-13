"""
Maintenance center tests.

Run:
    python tests/test_maintenance.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
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
