"""Installed-layout smoke checks using isolated local data."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from src import accounts
from src import category_import
from src import db
from src import jobs
from src import maintenance
from src import server_runtime
from src.crawler import sync
from src.config import PROJECT_ROOT, settings
from src.models import Favorite


def _check(ok: bool, message: str, details: dict[str, Any] | None = None) -> dict:
    return {"ok": bool(ok), "message": message, "details": details or {}}


def _write_env(app_root: Path, port: int) -> Path:
    env_path = app_root / ".env"
    env_text = "\n".join(
        [
            "DB_PATH=data/recall-smoke.db",
            "PLAYWRIGHT_PROFILE_PATH=data/playwright_profile",
            "USER_DATA_ROOT=data/users",
            "AVATAR_CACHE_DIR=data/avatar_cache",
            "WEB_HOST=127.0.0.1",
            f"WEB_PORT={int(port)}",
            "WEB_AUTH_REQUIRED=false",
            "LOG_LEVEL=INFO",
            "",
        ]
    )
    env_path.write_text(env_text, encoding="utf-8")
    return env_path


def _seed_isolated_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA_SQL)
        conn.execute("CREATE TABLE IF NOT EXISTS favorites_vec (id TEXT PRIMARY KEY, user_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS likes_vec (id TEXT PRIMARY KEY, user_id TEXT)")
        conn.execute(
            """
            INSERT OR IGNORE INTO users (id, display_name, created_at)
            VALUES ('default', '本地默认用户', ?)
            """,
            (datetime.now(timezone.utc),),
        )
        conn.commit()
    finally:
        conn.close()


def _download_env(app_root: Path) -> dict[str, str]:
    runtime_root = app_root / "runtime-downloads"
    return {
        "UV_CACHE_DIR": str(runtime_root / "uv-cache"),
        "UV_LINK_MODE": "copy",
        "PLAYWRIGHT_BROWSERS_PATH": str(runtime_root / "ms-playwright"),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(db_path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
    finally:
        conn.close()


def _copy_sqlite(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(str(source_path))
    target = sqlite3.connect(str(target_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _rollback_command_has_apply(control_script: Path) -> bool:
    if not control_script.exists():
        return False
    for line in control_script.read_text(encoding="utf-8", errors="replace").splitlines():
        if "rollback-from-manifest" in line and "--apply" in line:
            return True
    return False


def _run_rollback_check_smoke(
    *,
    data_dir: Path,
    exports_dir: Path,
    isolated_db: Path,
    control_script: Path,
) -> dict:
    missing_manifest_dir = data_dir / "release-checks-empty"
    missing_manifest_dir.mkdir(parents=True, exist_ok=True)
    release_checks_dir = data_dir / "release-checks"
    release_checks_dir.mkdir(parents=True, exist_ok=True)
    manifests_before = sorted(missing_manifest_dir.glob("delivery-manifest-*.json"))
    no_manifest = {
        "ok": not manifests_before,
        "message": (
            "No delivery-manifest-*.json found; rollback-check will prompt clearly."
            if not manifests_before
            else "delivery-manifest files already exist before smoke setup."
        ),
        "count": len(manifests_before),
    }

    backup_path = exports_dir / "pre-release-recall-smoke.db"
    _copy_sqlite(isolated_db, backup_path)
    backup_path = backup_path.resolve()
    counts = _table_counts(
        backup_path,
        ("users", "favorites", "likes", "job_queue", "crawl_runs", "like_crawl_runs"),
    )
    manifest_path = release_checks_dir / "delivery-manifest-smoke.json"
    manifest_path = manifest_path.resolve()
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ok": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "evidence": {
                    "pre_release_backup": {
                        "ok": True,
                        "backup": {
                            "path": str(backup_path),
                            "sha256": _sha256(backup_path),
                            "source_counts": counts,
                        },
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    rollback = maintenance.restore_from_delivery_manifest(
        manifest_path,
        apply=False,
        db_path=isolated_db,
        backup_dir=exports_dir,
    )
    command = ["rollback-from-manifest", "--manifest", str(manifest_path), "--json"]
    control_has_apply = _rollback_command_has_apply(control_script)
    control_exists = control_script.exists()
    dry_run = {
        "ok": bool(rollback.get("ok")) and rollback.get("mode") == "dry_run",
        "mode": rollback.get("mode"),
        "restored": bool(rollback.get("restored")),
        "errors": rollback.get("errors") or [],
        "manifest": str(manifest_path),
        "backup": str(backup_path),
    }
    return {
        "ok": bool(no_manifest["ok"]) and bool(dry_run["ok"]) and control_exists and not control_has_apply,
        "message": "rollback-check dry-run entrypoint verified.",
        "details": {
            "no_manifest": no_manifest,
            "dry_run": dry_run,
            "command": command,
            "control_script": str(control_script),
            "control_script_exists": control_exists,
            "control_script_has_apply": control_has_apply,
        },
    }


def _run_auth_setup_fragment_smoke(client, web_app, *, qr_path: Path) -> dict:
    from src.web import douyin_auth

    user_id = "default"
    qr_path_str = str(qr_path)
    qr_version = hashlib.sha256(qr_path_str.encode("utf-8")).hexdigest()[:16]
    sensitive_message = (
        "Traceback (most recent call last): "
        f"command: uv run python -m src.cli auth {qr_path_str}"
    )
    sensitive_tokens = ["Traceback", "command:", "uv run", qr_path_str, str(qr_path.parent)]
    states = ["qr_ready", "scan_pending", "confirmed", "failed"]
    endpoints = ["/auth/status", "/setup/auth-status", "/setup/scan-state"]
    checks: list[dict[str, Any]] = []
    setup_unchanged_status_codes: list[int] = []
    sensitive_tokens_found: list[str] = []

    previous_session = douyin_auth.auth_session_snapshot(user_id)

    def set_session(status: str, message: str = "", path: str | None = qr_path_str) -> None:
        douyin_auth.set_auth_session(
            user_id,
            {
                "status": status,
                "message": message,
                "qr_path": path,
            },
        )

    def add_check(name: str, ok: bool, response=None, expected: str = "") -> None:
        details = {
            "name": name,
            "ok": bool(ok),
        }
        if response is not None:
            details["status_code"] = response.status_code
        if expected:
            details["expected"] = expected
        checks.append(details)

    try:
        set_session("qr_ready", "请用抖音 App 扫描二维码")
        auth_qr = client.get("/auth/status")
        setup_qr = client.get("/setup/auth-status")
        unchanged_qr = client.get(f"/setup/scan-state?qr={qr_version}")
        setup_unchanged_status_codes.append(unchanged_qr.status_code)
        add_check(
            "auth_qr_ready",
            auth_qr.status_code == 200 and "打开抖音 App 扫一扫" in auth_qr.text,
            auth_qr,
            "打开抖音 App 扫一扫",
        )
        add_check(
            "setup_qr_ready",
            setup_qr.status_code == 200 and 'data-setup-auth-state="qr_ready"' in setup_qr.text,
            setup_qr,
            'data-setup-auth-state="qr_ready"',
        )
        add_check("setup_qr_unchanged", unchanged_qr.status_code == 204, unchanged_qr)

        set_session("scan_pending", "")
        auth_scan = client.get("/auth/status")
        setup_scan = client.get("/setup/scan-state?state=qr_ready")
        unchanged_scan = client.get("/setup/scan-state?state=scan_pending")
        setup_unchanged_status_codes.append(unchanged_scan.status_code)
        add_check(
            "auth_scan_pending",
            auth_scan.status_code == 200 and "手机已扫码，等待你在抖音 App 确认登录" in auth_scan.text,
            auth_scan,
            "手机已扫码",
        )
        add_check(
            "setup_scan_pending",
            setup_scan.status_code == 200 and "手机已扫码，等待你在抖音 App 确认登录" in setup_scan.text,
            setup_scan,
            "手机已扫码",
        )
        add_check("setup_scan_unchanged", unchanged_scan.status_code == 204, unchanged_scan)

        set_session("confirmed", "", None)
        auth_confirmed = client.get("/auth/status")
        setup_confirmed = client.get("/setup/scan-state?state=scan_pending")
        unchanged_confirmed = client.get("/setup/scan-state?state=confirmed")
        setup_unchanged_status_codes.append(unchanged_confirmed.status_code)
        add_check(
            "auth_confirmed",
            auth_confirmed.status_code == 200 and "确认完成，正在保存账号" in auth_confirmed.text,
            auth_confirmed,
            "确认完成",
        )
        add_check(
            "setup_confirmed",
            setup_confirmed.status_code == 200 and "扫码成功" in setup_confirmed.text,
            setup_confirmed,
            "扫码成功",
        )
        add_check("setup_confirmed_unchanged", unchanged_confirmed.status_code == 204, unchanged_confirmed)

        set_session("failed", sensitive_message, None)
        auth_failed = client.get("/auth/status")
        setup_failed = client.get("/setup/auth-status")
        failed_text = auth_failed.text + setup_failed.text
        sensitive_tokens_found = [token for token in sensitive_tokens if token and token in failed_text]
        add_check(
            "auth_failed_redacted",
            auth_failed.status_code == 200
            and "授权过程出错，请重新生成二维码后再试。" in auth_failed.text
            and not sensitive_tokens_found,
            auth_failed,
            "授权过程出错，请重新生成二维码后再试。",
        )
        add_check(
            "setup_failed_redacted",
            setup_failed.status_code == 200
            and "授权过程出错，请重新生成二维码后再试。" in setup_failed.text
            and not sensitive_tokens_found,
            setup_failed,
            "授权过程出错，请重新生成二维码后再试。",
        )
    finally:
        if previous_session:
            douyin_auth.set_auth_session(user_id, previous_session)
        else:
            douyin_auth.clear_auth_session(user_id)

    ok = all(item["ok"] for item in checks) and not sensitive_tokens_found
    return _check(
        ok,
        "账号授权和首次设置状态片段可在安装后环境中渲染。",
        {
            "states": states,
            "endpoints": endpoints,
            "setup_unchanged_status_codes": setup_unchanged_status_codes,
            "sensitive_tokens_found": sensitive_tokens_found,
            "checks": checks,
        },
    )


def _run_job_queue_stability_smoke(*, backup_dir: Path) -> dict:
    conn = db.get_connection()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    smoke_user_id = f"smoke_queue_{stamp}"
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (smoke_user_id, "队列稳定性烟测", datetime.now(timezone.utc)),
    )

    duplicate_payload = {"max_pages": 11, "smoke_run_id": stamp}
    duplicate_payload_json = json.dumps(duplicate_payload, ensure_ascii=False, sort_keys=True)
    duplicate_first_id = jobs.enqueue_job(
        "sync_favorites",
        user_id=smoke_user_id,
        payload=duplicate_payload,
    )
    duplicate_second_id = jobs.enqueue_job(
        "sync_favorites",
        user_id=smoke_user_id,
        payload={"smoke_run_id": stamp, "max_pages": 11},
    )
    duplicate_job_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM job_queue
            WHERE user_id = ? AND kind = 'sync_favorites' AND payload_json = ?
            """,
            (smoke_user_id, duplicate_payload_json),
        ).fetchone()[0]
    )
    jobs.finish_job(duplicate_first_id)

    retry_id = jobs.enqueue_job(
        "sync_likes",
        user_id=smoke_user_id,
        payload={"max_pages": 7, "smoke_run_id": stamp},
        max_attempts=3,
    )
    conn.execute(
        """
        UPDATE job_queue
        SET status = 'running', attempts = 1, started_at = ?
        WHERE id = ?
        """,
        (datetime.now(timezone.utc), retry_id),
    )
    jobs.fail_job(retry_id, "temporary smoke failure")
    retry_row = conn.execute(
        """
        SELECT status, attempts, max_attempts, next_run_at, error_message
        FROM job_queue
        WHERE id = ?
        """,
        (retry_id,),
    ).fetchone()

    stale_id = jobs.enqueue_job(
        "index",
        user_id=smoke_user_id,
        payload={"content_kind": "favorites", "smoke_run_id": stamp},
        max_attempts=3,
    )
    conn.execute(
        """
        UPDATE job_queue
        SET status = 'running', attempts = 1, started_at = ?
        WHERE id = ?
        """,
        (datetime.now(timezone.utc) - timedelta(hours=2), stale_id),
    )

    failed_id = jobs.enqueue_job(
        "backup_sqlite",
        user_id=smoke_user_id,
        payload={"output_dir": str(backup_dir), "smoke_run_id": stamp},
        max_attempts=1,
    )
    conn.execute(
        """
        UPDATE job_queue
        SET status = 'running', attempts = 1, started_at = ?
        WHERE id = ?
        """,
        (datetime.now(timezone.utc), failed_id),
    )
    jobs.fail_job(failed_id, "permanent smoke failure")

    status = maintenance.get_maintenance_status(smoke_user_id, backup_dir=backup_dir)
    stale_row = conn.execute(
        """
        SELECT status, started_at, next_run_at, error_message
        FROM job_queue
        WHERE id = ?
        """,
        (stale_id,),
    ).fetchone()
    failed_section_details = (
        (status.get("sections") or {})
        .get("failed_tasks", {})
        .get("details", {})
    )
    failed_items = failed_section_details.get("items") or []
    terminal_failed_count = int(status.get("jobs", {}).get("failed") or 0)
    maintenance_running_count = int(status.get("jobs", {}).get("running") or 0)
    recovered_stale_running = int(status.get("jobs", {}).get("recovered_stale_running") or 0)
    retrying_count = int(status.get("jobs", {}).get("retrying") or 0)
    failed_section_retrying_count = int(failed_section_details.get("retrying_count") or 0)
    page_backend_inconsistent = bool(
        maintenance_running_count > 0
        or stale_row["status"] == "running"
        or recovered_stale_running < 1
    )
    terminal_failed_can_retry = [
        bool(item.get("can_retry"))
        for item in failed_items
        if item.get("id") == failed_id
    ]
    details = {
        "user_id": smoke_user_id,
        "duplicate_suppressed": duplicate_first_id == duplicate_second_id,
        "duplicate_job_count": duplicate_job_count,
        "retry_status": retry_row["status"],
        "retry_attempts": int(retry_row["attempts"] or 0),
        "retry_max_attempts": int(retry_row["max_attempts"] or 0),
        "retry_next_run_at": retry_row["next_run_at"],
        "recovered_stale_running": recovered_stale_running,
        "stale_status_after_maintenance": stale_row["status"],
        "stale_error_message": stale_row["error_message"],
        "maintenance_running_count": maintenance_running_count,
        "maintenance_retrying_count": retrying_count,
        "terminal_failed_count": terminal_failed_count,
        "terminal_failed_can_retry": terminal_failed_can_retry[0] if terminal_failed_can_retry else None,
        "page_backend_inconsistent": page_backend_inconsistent,
        "failed_section_retrying_count": failed_section_retrying_count,
        "failed_section_next_run_at": failed_section_details.get("next_run_at"),
    }
    ok = (
        details["duplicate_suppressed"]
        and duplicate_job_count == 1
        and retry_row["status"] == "pending"
        and int(retry_row["attempts"] or 0) == 1
        and retry_row["next_run_at"] is not None
        and recovered_stale_running == 1
        and stale_row["status"] == "pending"
        and maintenance_running_count == 0
        and retrying_count == 2
        and terminal_failed_count == 1
        and details["terminal_failed_can_retry"] is False
        and not page_backend_inconsistent
        and failed_section_retrying_count == 2
    )
    return _check(ok, "后台任务队列稳定性可在隔离安装环境中验证。", details)


def _insert_sync_smoke_row(
    *,
    conn,
    table: str,
    time_column: str,
    user_id: str,
    item_id: str,
    title: str,
    category_id: int,
    action_time: str,
    video_created_at: str,
    is_removed: int = 0,
    note: str = "keep smoke note",
) -> None:
    conn.execute(
        f"""
        INSERT INTO {table} (
            user_id, id, title, author, {time_column}, first_seen_at, last_seen_at,
            user_note, raw_json, is_removed, discovery_index, category_id,
            video_created_at
        ) VALUES (?, ?, ?, 'old author', ?, '2026-01-01 00:00:00',
                  '2026-01-01 00:00:00', ?, '{{}}', ?, 1, ?, ?)
        """,
        (
            user_id,
            item_id,
            title,
            action_time,
            note,
            int(is_removed),
            category_id,
            video_created_at,
        ),
    )


def _run_sync_idempotency_for_kind(
    *,
    conn,
    content_kind: str,
    user_id: str,
    category_id: int,
    stamp: str,
) -> dict:
    if content_kind == "favorites":
        table = "favorites"
        time_column = "favorited_at"
        apply = sync.apply_crawl
        prefix = "fav"
    else:
        table = "likes"
        time_column = "liked_at"
        apply = sync.apply_like_crawl
        prefix = "like"

    stable_id = f"{prefix}-stable-{stamp}"
    missing_id = f"{prefix}-missing-{stamp}"
    returning_id = f"{prefix}-returning-{stamp}"
    new_id = f"{prefix}-new-{stamp}"
    action_time = "2026-01-02 03:04:05"
    video_time = "2025-12-01 00:00:00"
    _insert_sync_smoke_row(
        conn=conn,
        table=table,
        time_column=time_column,
        user_id=user_id,
        item_id=stable_id,
        title="stable old title",
        category_id=category_id,
        action_time=action_time,
        video_created_at=video_time,
    )
    _insert_sync_smoke_row(
        conn=conn,
        table=table,
        time_column=time_column,
        user_id=user_id,
        item_id=missing_id,
        title="missing old title",
        category_id=category_id,
        action_time="2026-01-03 03:04:05",
        video_created_at="2025-12-02 00:00:00",
    )
    _insert_sync_smoke_row(
        conn=conn,
        table=table,
        time_column=time_column,
        user_id=user_id,
        item_id=returning_id,
        title="returning old title",
        category_id=category_id,
        action_time="2026-01-04 03:04:05",
        video_created_at="2025-12-03 00:00:00",
        is_removed=1,
    )
    batch = [
        Favorite(
            id=stable_id,
            title="stable new title",
            author="new author",
            raw_json='{"fresh": true}',
            video_created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        Favorite(
            id=returning_id,
            title="returning new title",
            author="new author",
            raw_json='{"returned": true}',
            video_created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        ),
        Favorite(
            id=new_id,
            title="new title",
            author="new author",
            raw_json='{"new": true}',
            video_created_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        ),
    ]

    first = apply(batch, is_first_crawl=False, user_id=user_id)
    second = apply(batch, is_first_crawl=False, user_id=user_id)

    stable = conn.execute(
        f"""
        SELECT user_note, category_id, {time_column} AS action_time,
               video_created_at, is_removed
        FROM {table}
        WHERE user_id = ? AND id = ?
        """,
        (user_id, stable_id),
    ).fetchone()
    missing = conn.execute(
        f"SELECT is_removed FROM {table} WHERE user_id = ? AND id = ?",
        (user_id, missing_id),
    ).fetchone()
    returning = conn.execute(
        f"SELECT is_removed FROM {table} WHERE user_id = ? AND id = ?",
        (user_id, returning_id),
    ).fetchone()
    row_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    duplicate_rows = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ? AND id = ?",
            (user_id, stable_id),
        ).fetchone()[0]
    )
    return {
        "row_count_after_repeated_sync": row_count,
        "duplicate_rows_for_stable_item": duplicate_rows,
        "first_sync": {
            "new_count": first.new_count,
            "updated_count": first.updated_count,
            "removed_count": first.removed_count,
        },
        "second_sync": {
            "new_count": second.new_count,
            "updated_count": second.updated_count,
            "removed_count": second.removed_count,
        },
        "note_preserved": stable["user_note"] == "keep smoke note",
        "category_preserved": stable["category_id"] == category_id,
        "action_time_preserved": str(stable["action_time"]) == action_time,
        "video_created_at_preserved": str(stable["video_created_at"]) == video_time,
        "missing_item_marked_removed": int(missing["is_removed"]) == 1,
        "returning_item_reactivated": int(returning["is_removed"]) == 0,
    }


def _run_sync_idempotency_smoke() -> dict:
    conn = db.get_connection()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    user_id = f"smoke_sync_{stamp}"
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (user_id, "同步幂等性烟测", datetime.now(timezone.utc)),
    )
    conn.execute(
        """
        INSERT INTO categories (
            account_id, name, auto_name, item_count, created_at, updated_at
        ) VALUES (?, '烟测分类', '烟测分类', 0, ?, ?)
        """,
        (user_id, datetime.now(timezone.utc), datetime.now(timezone.utc)),
    )
    category_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    favorites = _run_sync_idempotency_for_kind(
        conn=conn,
        content_kind="favorites",
        user_id=user_id,
        category_id=category_id,
        stamp=stamp,
    )
    likes = _run_sync_idempotency_for_kind(
        conn=conn,
        content_kind="likes",
        user_id=user_id,
        category_id=category_id,
        stamp=stamp,
    )

    def kind_ok(details: dict) -> bool:
        return (
            details["row_count_after_repeated_sync"] == 4
            and details["duplicate_rows_for_stable_item"] == 1
            and details["first_sync"]["new_count"] == 1
            and details["first_sync"]["updated_count"] == 2
            and details["first_sync"]["removed_count"] == 1
            and details["second_sync"]["new_count"] == 0
            and details["second_sync"]["removed_count"] == 0
            and details["note_preserved"]
            and details["category_preserved"]
            and details["action_time_preserved"]
            and details["video_created_at_preserved"]
            and details["missing_item_marked_removed"]
            and details["returning_item_reactivated"]
        )

    details = {
        "content_kinds": ["favorites", "likes"],
        "favorites": favorites,
        "likes": likes,
    }
    return _check(
        kind_ok(favorites) and kind_ok(likes),
        "收藏和喜欢的重复同步幂等性可在隔离安装环境中验证。",
        details,
    )


def _ensure_category_id_column(conn: sqlite3.Connection, table: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "category_id" not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN category_id INTEGER")


def _insert_category_import_user(conn: sqlite3.Connection, user_id: str, display_name: str) -> None:
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (user_id, display_name, datetime.now(timezone.utc)),
    )


def _insert_category_import_category(conn: sqlite3.Connection, account_id: str, name: str) -> int:
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        """
        INSERT INTO categories (
            account_id, name, auto_name, keywords_json, item_count, algo, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 0, 'smoke', ?, ?)
        """,
        (account_id, name, name, json.dumps([name], ensure_ascii=False), now, now),
    )
    return int(cur.lastrowid)


def _insert_category_import_favorite(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    item_id: str,
    title: str,
    category_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, favorited_at, first_seen_at, last_seen_at,
            raw_json, is_removed, category_id
        ) VALUES (?, ?, ?, ?, ?, ?, '{}', 0, ?)
        """,
        (user_id, item_id, title, now, now, now, category_id),
    )


def _create_category_import_source_db(
    source_path: Path,
    *,
    user_id: str,
    matching_item_id: str,
    source_only_item_id: str,
) -> None:
    source_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(source_path), detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(db.SCHEMA_SQL)
        _ensure_category_id_column(conn, "favorites")
        _ensure_category_id_column(conn, "likes")
        _insert_category_import_user(conn, user_id, "旧库用户")
        category_id = _insert_category_import_category(conn, user_id, "旧库分类")
        _insert_category_import_favorite(
            conn,
            user_id=user_id,
            item_id=matching_item_id,
            title="旧库中当前仍存在的收藏",
            category_id=category_id,
        )
        _insert_category_import_favorite(
            conn,
            user_id=user_id,
            item_id=source_only_item_id,
            title="旧库中当前不存在的收藏",
            category_id=category_id,
        )
    finally:
        conn.close()


def _run_category_import_migration_smoke(*, data_dir: Path, current_db_path: Path) -> dict:
    conn = db.get_connection()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    import_user_id = f"smoke_category_import_{stamp}"
    guard_user_id = f"smoke_category_guard_{stamp}"
    matching_item_id = f"category-match-{stamp}"
    current_only_item_id = f"category-current-only-{stamp}"
    source_only_item_id = f"category-source-only-{stamp}"
    guard_item_id = f"category-guard-{stamp}"
    legacy_db = data_dir / f"old-install-{stamp}" / "DouyinRecall" / "data" / "recall.db"

    _insert_category_import_user(conn, import_user_id, "分类导入烟测")
    _insert_category_import_favorite(
        conn,
        user_id=import_user_id,
        item_id=matching_item_id,
        title="当前库中可匹配的收藏",
    )
    _insert_category_import_favorite(
        conn,
        user_id=import_user_id,
        item_id=current_only_item_id,
        title="当前库中没有旧分类的收藏",
    )
    _create_category_import_source_db(
        legacy_db,
        user_id=import_user_id,
        matching_item_id=matching_item_id,
        source_only_item_id=source_only_item_id,
    )

    original_patterns = category_import._candidate_patterns
    category_import._candidate_patterns = lambda: [
        str(legacy_db),
        str(current_db_path),
    ]
    try:
        discovered_sources = category_import.default_category_source_paths(current_db_path)
    finally:
        category_import._candidate_patterns = original_patterns

    candidates = category_import.find_category_import_candidates(
        current_conn=conn,
        current_db_path=current_db_path,
        search_paths=discovered_sources,
        content_kind="favorites",
        user_id=import_user_id,
    )
    result = category_import.import_categories_from_database(
        legacy_db,
        current_conn=conn,
        current_db_path=current_db_path,
        content_kind="favorites",
        user_id=import_user_id,
    )

    matched_row = conn.execute(
        """
        SELECT c.name AS category_name
        FROM favorites f
        LEFT JOIN categories c ON c.id = f.category_id
        WHERE f.user_id = ? AND f.id = ?
        """,
        (import_user_id, matching_item_id),
    ).fetchone()
    imported_category_row = conn.execute(
        "SELECT name, item_count FROM categories WHERE account_id = ? ORDER BY id",
        (import_user_id,),
    ).fetchone()
    source_only_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id = ? AND id = ?",
            (import_user_id, source_only_item_id),
        ).fetchone()[0]
    )

    _insert_category_import_user(conn, guard_user_id, "分类保护烟测")
    existing_category_id = _insert_category_import_category(conn, guard_user_id, "当前分类")
    _insert_category_import_favorite(
        conn,
        user_id=guard_user_id,
        item_id=guard_item_id,
        title="已有分类时不应被覆盖的收藏",
    )
    guard_db = data_dir / f"old-install-guard-{stamp}" / "DouyinRecall" / "data" / "recall.db"
    _create_category_import_source_db(
        guard_db,
        user_id=guard_user_id,
        matching_item_id=guard_item_id,
        source_only_item_id=f"{source_only_item_id}-guard",
    )
    guard_result = category_import.import_categories_from_database(
        guard_db,
        current_conn=conn,
        current_db_path=current_db_path,
        content_kind="favorites",
        user_id=guard_user_id,
    )
    guard_category_rows = conn.execute(
        "SELECT id, name FROM categories WHERE account_id = ? ORDER BY id",
        (guard_user_id,),
    ).fetchall()
    guard_item_row = conn.execute(
        "SELECT category_id FROM favorites WHERE user_id = ? AND id = ?",
        (guard_user_id, guard_item_id),
    ).fetchone()

    first_candidate = candidates[0] if candidates else None
    details = {
        "legacy_db": str(legacy_db),
        "discovered_sources": len(discovered_sources),
        "discovered_source_paths": [str(path) for path in discovered_sources],
        "candidate_match_count": first_candidate.match_count if first_candidate else 0,
        "candidate_source_item_count": first_candidate.source_item_count if first_candidate else 0,
        "import_result": {
            "reason": result.reason,
            "category_count": result.category_count,
            "assigned_item_count": result.assigned_item_count,
        },
        "imported_category_name": imported_category_row["name"] if imported_category_row else None,
        "imported_category_item_count": int(imported_category_row["item_count"] or 0)
        if imported_category_row
        else 0,
        "matched_item_category": matched_row["category_name"] if matched_row else None,
        "unmatched_item_imported": source_only_count > 0,
        "existing_guard": {
            "reason": guard_result.reason,
            "category_count_after": len(guard_category_rows),
            "existing_category_preserved": any(
                row["id"] == existing_category_id and row["name"] == "当前分类"
                for row in guard_category_rows
            ),
            "existing_item_unassigned": bool(guard_item_row and guard_item_row["category_id"] is None),
        },
    }
    ok = (
        len(discovered_sources) == 1
        and first_candidate is not None
        and first_candidate.path == legacy_db
        and first_candidate.match_count == 1
        and first_candidate.source_item_count == 2
        and result.imported
        and result.reason == "imported"
        and result.category_count == 1
        and result.assigned_item_count == 1
        and details["imported_category_name"] == "旧库分类"
        and details["imported_category_item_count"] == 1
        and details["matched_item_category"] == "旧库分类"
        and not details["unmatched_item_imported"]
        and guard_result.imported is False
        and guard_result.reason == "current_has_categories"
        and details["existing_guard"]["category_count_after"] == 1
        and details["existing_guard"]["existing_category_preserved"]
        and details["existing_guard"]["existing_item_unassigned"]
    )
    return _check(ok, "旧安装目录分类导入和已有分类保护可在隔离安装环境中验证。", details)


def _insert_account_boundary_user(
    conn,
    *,
    user_id: str,
    display_name: str,
    nickname: str | None = None,
    unique_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO users (
            id, display_name, douyin_nickname, douyin_unique_id,
            douyin_sec_uid, douyin_avatar_url, douyin_profile_updated_at,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            user_id,
            display_name,
            nickname,
            unique_id,
            f"SEC_{user_id}" if nickname else None,
            f"https://example.test/{user_id}.jpeg" if nickname else None,
            "2026-07-07 00:00:00" if nickname else None,
            "2026-07-07 00:00:00",
        ),
    )


def _insert_account_boundary_item(
    conn,
    *,
    table: str,
    user_id: str,
    item_id: str,
    title: str,
) -> None:
    time_column = "favorited_at" if table == "favorites" else "liked_at"
    conn.execute(
        f"""
        INSERT INTO {table} (
            user_id, id, title, author, video_url, cover_url, user_note,
            raw_json, {time_column}, first_seen_at, last_seen_at,
            is_removed, discovery_index, video_created_at
        ) VALUES (?, ?, ?, 'account smoke author', ?, NULL, 'account smoke note',
                  '{{}}', '2026-07-07 00:00:00', '2026-07-07 00:00:00',
                  '2026-07-07 00:00:00', 0, 1, '2026-07-01 00:00:00')
        """,
        (user_id, item_id, title, f"https://example.test/{item_id}"),
    )


def _client_session_token(client) -> str | None:
    try:
        token = client.cookies.get(settings.session_cookie_name, domain="testserver.local")
        if token:
            return token
    except Exception:
        pass
    try:
        return client.cookies.get(settings.session_cookie_name)
    except Exception:
        return None


def _content_counts_for_user(conn, user_id: str) -> dict[str, int]:
    return {
        "favorites": int(
            conn.execute(
                "SELECT COUNT(*) FROM favorites WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        ),
        "likes": int(
            conn.execute(
                "SELECT COUNT(*) FROM likes WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        ),
    }


def _profile_state(conn, user_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT douyin_nickname, douyin_unique_id, douyin_sec_uid,
               douyin_avatar_url, douyin_profile_updated_at
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    return dict(row) if row else {}


def _run_account_boundaries_smoke(client, web_app) -> dict:
    from fastapi.testclient import TestClient

    conn = db.get_connection()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    alice_id = f"smoke_account_alice_{stamp}"
    bob_id = f"smoke_account_bob_{stamp}"
    unbound_id = f"smoke_account_unbound_{stamp}"
    alice_title = f"账号隔离 Alice 收藏 {stamp}"
    bob_title = f"账号隔离 Bob 收藏 {stamp}"
    alice_like_title = f"账号隔离 Alice 喜欢 {stamp}"
    bob_like_title = f"账号隔离 Bob 喜欢 {stamp}"

    _insert_account_boundary_user(
        conn,
        user_id=alice_id,
        display_name="账号边界 Alice",
        nickname="Alice抖音",
        unique_id=f"alice_{stamp}",
    )
    _insert_account_boundary_user(
        conn,
        user_id=bob_id,
        display_name="账号边界 Bob",
        nickname="Bob抖音",
        unique_id=f"bob_{stamp}",
    )
    _insert_account_boundary_user(
        conn,
        user_id=unbound_id,
        display_name="账号边界未绑定",
    )
    _insert_account_boundary_item(
        conn,
        table="favorites",
        user_id=alice_id,
        item_id=f"account-alice-fav-{stamp}",
        title=alice_title,
    )
    _insert_account_boundary_item(
        conn,
        table="favorites",
        user_id=bob_id,
        item_id=f"account-bob-fav-{stamp}",
        title=bob_title,
    )
    _insert_account_boundary_item(
        conn,
        table="likes",
        user_id=alice_id,
        item_id=f"account-alice-like-{stamp}",
        title=alice_like_title,
    )
    _insert_account_boundary_item(
        conn,
        table="likes",
        user_id=bob_id,
        item_id=f"account-bob-like-{stamp}",
        title=bob_like_title,
    )

    alice_token = accounts.create_session(alice_id)
    bob_token = accounts.create_session(bob_id)
    counts_before = {
        alice_id: _content_counts_for_user(conn, alice_id),
        bob_id: _content_counts_for_user(conn, bob_id),
    }
    start_calls: list[tuple[str, bool]] = []
    cleanup_calls: list[str] = []
    from src.web import douyin_auth

    original_start = douyin_auth.ensure_douyin_auth_started
    original_cleanup = douyin_auth.start_douyin_logout_cleanup
    original_fetch = douyin_auth.fetch_douyin_profile_for_user

    def fake_start(user_id: str, *, force: bool = False) -> None:
        start_calls.append((user_id, bool(force)))

    def fake_fetch(user_id: str) -> dict:
        return {
            "nickname": f"重新绑定-{user_id}",
            "unique_id": f"reb_{user_id}",
            "sec_uid": f"REB_SEC_{user_id}",
            "avatar_url": f"https://example.test/rebind/{user_id}.jpeg",
        }

    try:
        douyin_auth.ensure_douyin_auth_started = fake_start
        douyin_auth.start_douyin_logout_cleanup = cleanup_calls.append
        douyin_auth.fetch_douyin_profile_for_user = fake_fetch

        client.cookies.set(settings.session_cookie_name, alice_token)
        add_response = client.post("/auth/add", follow_redirects=False)
        new_token = _client_session_token(client)
        new_user = accounts.user_from_session(new_token)
        new_user_id = new_user["id"] if new_user else None

        switch_client = TestClient(web_app.app)
        switch_client.cookies.set(settings.session_cookie_name, alice_token)
        other_client = TestClient(web_app.app)
        other_client.cookies.set(settings.session_cookie_name, alice_token)
        switch_response = switch_client.post(
            "/auth/switch",
            data={"user_id": bob_id},
            follow_redirects=False,
        )
        switched_token = _client_session_token(switch_client)
        switched_user = accounts.user_from_session(switched_token)
        other_user = accounts.user_from_session(_client_session_token(other_client))
        reject_response = switch_client.post(
            "/auth/switch",
            data={"user_id": unbound_id},
            follow_redirects=False,
        )

        alice_home_client = TestClient(web_app.app)
        alice_home_client.cookies.set(settings.session_cookie_name, alice_token)
        bob_home_client = TestClient(web_app.app)
        bob_home_client.cookies.set(settings.session_cookie_name, bob_token)
        alice_home = alice_home_client.get("/")
        bob_home = bob_home_client.get("/")

        logout_client = TestClient(web_app.app)
        logout_client.cookies.set(settings.session_cookie_name, bob_token)
        logout_response = logout_client.post("/auth/logout", follow_redirects=False)
        bob_after_logout = _profile_state(conn, bob_id)
        alice_after_logout = _profile_state(conn, alice_id)
        counts_after_logout = {
            alice_id: _content_counts_for_user(conn, alice_id),
            bob_id: _content_counts_for_user(conn, bob_id),
        }

        rebind_client = TestClient(web_app.app)
        rebind_client.cookies.set(settings.session_cookie_name, alice_token)
        rebind_response = rebind_client.post("/auth/profile/refresh")
        alice_after_rebind = _profile_state(conn, alice_id)
        bob_after_rebind = _profile_state(conn, bob_id)
    finally:
        douyin_auth.ensure_douyin_auth_started = original_start
        douyin_auth.start_douyin_logout_cleanup = original_cleanup
        douyin_auth.fetch_douyin_profile_for_user = original_fetch

    details = {
        "user_ids": {
            "alice": alice_id,
            "bob": bob_id,
            "unbound": unbound_id,
            "added": new_user_id,
        },
        "status_codes": {
            "add_account": add_response.status_code,
            "switch_account": switch_response.status_code,
            "switch_unbound": reject_response.status_code,
            "alice_home": alice_home.status_code,
            "bob_home": bob_home.status_code,
            "logout": logout_response.status_code,
            "rebind": rebind_response.status_code,
        },
        "start_calls": start_calls,
        "cleanup_calls": cleanup_calls,
        "counts_before": counts_before,
        "counts_after_logout": counts_after_logout,
        "add_account_created_new_user": (
            add_response.status_code == 303
            and new_user_id is not None
            and new_user_id not in {alice_id, bob_id, unbound_id}
            and bool((new_user or {}).get("display_name", "").startswith("抖音账号"))
        ),
        "add_account_started_qr_for_new_user": (
            new_user_id is not None and start_calls[:1] == [(new_user_id, True)]
        ),
        "switch_account_changed_current_session": (
            switch_response.status_code == 303
            and switched_user is not None
            and switched_user["id"] == bob_id
        ),
        "other_session_unchanged_after_switch": (
            other_user is not None and other_user["id"] == alice_id
        ),
        "switch_rejects_unbound_account": reject_response.status_code == 404,
        "logout_cleared_current_profile_only": (
            logout_response.status_code == 303
            and cleanup_calls == [bob_id]
            and bob_after_logout.get("douyin_nickname") is None
            and bob_after_logout.get("douyin_unique_id") is None
            and bob_after_logout.get("douyin_sec_uid") is None
            and bob_after_logout.get("douyin_avatar_url") is None
            and bob_after_logout.get("douyin_profile_updated_at") is None
            and alice_after_logout.get("douyin_nickname") == "Alice抖音"
        ),
        "logout_preserved_local_content": counts_after_logout == counts_before,
        "rebind_updated_current_user_only": (
            rebind_response.status_code == 200
            and alice_after_rebind.get("douyin_nickname") == f"重新绑定-{alice_id}"
            and alice_after_rebind.get("douyin_unique_id") == f"reb_{alice_id}"
            and bob_after_rebind.get("douyin_nickname") is None
            and bob_after_rebind.get("douyin_unique_id") is None
        ),
        "multi_account_data_isolated": (
            alice_home.status_code == 200
            and bob_home.status_code == 200
            and alice_title in alice_home.text
            and bob_title not in alice_home.text
            and bob_title in bob_home.text
            and alice_title not in bob_home.text
        ),
    }
    ok = all(
        bool(details[key])
        for key in (
            "add_account_created_new_user",
            "add_account_started_qr_for_new_user",
            "switch_account_changed_current_session",
            "other_session_unchanged_after_switch",
            "switch_rejects_unbound_account",
            "logout_cleared_current_profile_only",
            "logout_preserved_local_content",
            "rebind_updated_current_user_only",
            "multi_account_data_isolated",
        )
    )
    return _check(ok, "账号添加、切换、退出、重新绑定和数据隔离可在安装态验证。", details)


def run_installed_smoke_test(
    app_root: Path | str,
    *,
    port: int = 18765,
    source_root: Path | str = PROJECT_ROOT,
) -> dict:
    """Create an isolated installed-like layout and verify reusable local entrypoints."""
    root = Path(app_root)
    source = Path(source_root)
    data_dir = root / "data"
    exports_dir = data_dir / "exports"
    logs_dir = data_dir / "logs"
    runtime_dir = data_dir / "runtime"
    profile_dir = data_dir / "playwright_profile"
    avatar_dir = data_dir / "avatar_cache"
    runtime_download_root = root / "runtime-downloads"
    for path in (root, data_dir, exports_dir, logs_dir, runtime_dir, profile_dir, avatar_dir, runtime_download_root):
        path.mkdir(parents=True, exist_ok=True)

    env_path = _write_env(root, port)
    isolated_db = data_dir / "recall-smoke.db"
    _seed_isolated_db(isolated_db)

    original_db_path = settings.db_path
    original_profile_path = settings.playwright_profile_path
    original_user_data_root = settings.user_data_root
    original_avatar_cache_dir = settings.avatar_cache_dir
    original_web_port = settings.web_port
    original_web_auth_required = settings.web_auth_required
    original_getter = maintenance.update_check.get_cached_update_status
    db.close_connection()
    try:
        settings.db_path = isolated_db
        settings.playwright_profile_path = profile_dir
        settings.user_data_root = data_dir / "users"
        settings.avatar_cache_dir = avatar_dir
        settings.web_port = int(port)
        settings.web_auth_required = False
        db.init_schema()
        maintenance.update_check.get_cached_update_status = lambda **_kwargs: {
            "local_version": "smoke",
            "latest_version": None,
            "update_available": False,
            "release_url": None,
            "asset_name": None,
            "asset_url": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        from fastapi.testclient import TestClient
        from src.web import app as web_app

        client = TestClient(web_app.app)
        maintenance_response = client.get("/maintenance")
        auth_setup_fragments = _run_auth_setup_fragment_smoke(
            client,
            web_app,
            qr_path=settings.user_data_root / "default" / "auth" / "douyin-login.png",
        )
        job_queue_stability = _run_job_queue_stability_smoke(backup_dir=exports_dir)
        sync_idempotency = _run_sync_idempotency_smoke()
        category_import_migration = _run_category_import_migration_smoke(
            data_dir=data_dir,
            current_db_path=isolated_db,
        )
        account_boundaries = _run_account_boundaries_smoke(client, web_app)
        status = maintenance.get_maintenance_status("default", backup_dir=exports_dir)
    finally:
        db.close_connection()
        settings.db_path = original_db_path
        settings.playwright_profile_path = original_profile_path
        settings.user_data_root = original_user_data_root
        settings.avatar_cache_dir = original_avatar_cache_dir
        settings.web_port = original_web_port
        settings.web_auth_required = original_web_auth_required
        maintenance.update_check.get_cached_update_status = original_getter

    download_env = _download_env(root)
    server_runtime.write_server_state(
        pid=os.getpid(),
        host="127.0.0.1",
        port=int(port),
        runtime_dir=runtime_dir,
    )
    lifecycle_running = server_runtime.get_server_status(
        runtime_dir=runtime_dir,
        process_checker=lambda pid: int(pid) == os.getpid(),
    )
    server_runtime.clear_server_state(runtime_dir=runtime_dir)
    lifecycle_stopped = server_runtime.get_server_status(runtime_dir=runtime_dir)
    wrong_download_paths = [
        value
        for key, value in download_env.items()
        if key != "UV_LINK_MODE" and not str(Path(value)).startswith(str(runtime_download_root))
    ]
    start_script = source / "packaging" / "windows" / "start-douyin-recall.ps1"
    control_script = source / "packaging" / "windows" / "control-douyin-recall.ps1"
    rollback_check = _run_rollback_check_smoke(
        data_dir=data_dir,
        exports_dir=exports_dir,
        isolated_db=isolated_db,
        control_script=control_script,
    )
    maintenance_sections = status.get("sections") or {}
    maintenance_section_keys = sorted(maintenance_sections.keys())
    maintenance_capabilities = status.get("capabilities") or {}
    maintenance_capability_keys = sorted(maintenance_capabilities.keys())
    required_maintenance_sections = [
        "actions",
        "backup",
        "failed_tasks",
        "index",
        "login",
        "service",
    ]
    required_maintenance_capabilities = [
        "backup_status",
        "failed_tasks",
        "index_status",
        "login_status",
        "service_status",
        "suggested_actions",
    ]
    maintenance_status_ok = (
        status.get("schema_version") == 1
        and status.get("capabilities_schema_version") == 1
        and all(key in maintenance_sections for key in required_maintenance_sections)
        and all(key in maintenance_capabilities for key in required_maintenance_capabilities)
        and "backups" in status
        and "server" in status
    )
    checks = {
        "env_file": _check(env_path.exists(), "已写入隔离 .env。", {"path": str(env_path)}),
        "start_script": _check(start_script.exists(), "启动脚本存在。", {"path": str(start_script)}),
        "status_command": _check(
            control_script.exists(),
            "状态入口脚本存在，可执行 recall status。",
            {"script": str(control_script), "command": "python -m src.cli status"},
        ),
        "maintenance_endpoint": _check(
            maintenance_response.status_code == 200 and "维护" in maintenance_response.text,
            "维护中心可在隔离测试库上渲染。",
            {"status_code": maintenance_response.status_code},
        ),
        "backup_directory": _check(exports_dir.exists(), "备份目录存在。", {"path": str(exports_dir)}),
        "logs_directory": _check(logs_dir.exists(), "日志目录存在。", {"path": str(logs_dir)}),
        "runtime_directory": _check(runtime_dir.exists(), "运行状态目录存在。", {"path": str(runtime_dir)}),
        "download_paths": _check(
            not wrong_download_paths,
            "运行时下载目录已隔离。",
            {"env": download_env, "wrong_paths": wrong_download_paths},
        ),
        "service_lifecycle": _check(
            lifecycle_running["state"] == "running" and lifecycle_stopped["state"] == "stopped",
            "服务启动/检查/停止状态流可在隔离 runtime 中完成。",
            {
                "runtime_dir": str(runtime_dir),
                "running_state": lifecycle_running["state"],
                "after_stop_state": lifecycle_stopped["state"],
            },
        ),
        "auth_setup_fragments": auth_setup_fragments,
        "job_queue_stability": job_queue_stability,
        "sync_idempotency": sync_idempotency,
        "category_import_migration": category_import_migration,
        "account_boundaries": account_boundaries,
        "rollback_check": rollback_check,
        "maintenance_status": _check(
            maintenance_status_ok,
            "维护状态结构可复用。",
            {
                "keys": sorted(status.keys()),
                "schema_version": status.get("schema_version"),
                "capabilities_schema_version": status.get("capabilities_schema_version"),
                "section_keys": maintenance_section_keys,
                "capability_keys": maintenance_capability_keys,
            },
        ),
    }
    return {
        "ok": all(item["ok"] for item in checks.values()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "app_root": str(root),
        "source_root": str(source),
        "data_dir": str(data_dir),
        "runtime_download_root": str(runtime_download_root),
        "checks": checks,
    }


def write_installed_smoke_report(report: dict, output_dir: Path | str) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "installed-smoke-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
