"""
Crawler API-mode tests.

Run:
    python tests/test_crawler_api.py
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

from click.testing import CliRunner

from src import db
from src.cli import _console_safe, cli
from src.crawler.douyin import (
    AUTH_QR_REGENERATE_FALLBACK_INTERVAL_S,
    AUTH_QR_REGENERATE_SAFETY_MARGIN_S,
    AUTH_SHOW_ACCOUNT_WAIT_S,
    AuthQrCapture,
    DouyinCrawler,
    CLICK_LOGIN_BUTTON_JS,
    CONFIRM_SCANNED_LOGIN_JS,
    FETCH_COLLECTION_BY_API_JS,
    FETCH_LIKES_BY_API_JS,
    REGENERATE_LOGIN_QR_JS,
    SHOW_LOGIN_PANEL_JS,
    _auth_qr_ttl_seconds,
    _auth_qr_display_path,
    classify_auth_flow_state,
    _collection_query,
    _extract_self_profile,
    _extract_self_sec_user_id,
    _extract_auth_qr_payload,
    _likes_query,
    _next_auth_qr_refresh_time,
    _qr_candidate_priority,
    _is_qr_like_box,
    _is_login_required_payload,
    _should_refresh_login_screenshot,
)


def create_backup_db(path: Path, *, favorite_title: str = "backup") -> None:
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


def create_backup_marker(path: Path) -> None:
    path.write_text(f"backup marker: {path.name}\n", encoding="utf-8")


def backup_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def write_manifest_for_backup(manifest_path: Path, backup_path: Path, *, sha256: str | None = None) -> None:
    payload = {
        "schema_version": 1,
        "ok": True,
        "evidence": {
            "pre_release_backup": {
                "ok": True,
                "backup": {
                    "path": str(backup_path),
                    "sha256": sha256 if sha256 is not None else backup_sha256(backup_path),
                    "source_counts": {"users": 1, "favorites": 1, "likes": 0},
                },
            }
        },
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_collection_query_contains_cursor_count_and_web_params() -> None:
    query = _collection_query(cursor=42, count=18)
    params = parse_qs(query)

    assert params["cursor"] == ["42"]
    assert params["count"] == ["18"]
    assert params["device_platform"] == ["webapp"]
    assert params["aid"] == ["6383"]
    assert params["channel"] == ["channel_pc_web"]


def test_collection_api_fetch_uses_post() -> None:
    assert "method: 'POST'" in FETCH_COLLECTION_BY_API_JS


def test_likes_query_contains_sec_uid_cursor_count_and_web_params() -> None:
    query = _likes_query(sec_user_id="SEC_UID", cursor=42, count=18)
    params = parse_qs(query)

    assert params["sec_user_id"] == ["SEC_UID"]
    assert params["max_cursor"] == ["42"]
    assert params["count"] == ["18"]
    assert params["device_platform"] == ["webapp"]
    assert params["aid"] == ["6383"]
    assert params["channel"] == ["channel_pc_web"]


def test_likes_api_fetch_uses_get_and_favorite_endpoint() -> None:
    assert "/aweme/v1/web/aweme/favorite/" in FETCH_LIKES_BY_API_JS
    assert "method: 'GET'" in FETCH_LIKES_BY_API_JS


def test_extract_self_sec_user_id_handles_known_payload_shapes() -> None:
    assert _extract_self_sec_user_id({"user": {"sec_uid": "SEC_A"}}) == "SEC_A"
    assert _extract_self_sec_user_id({"user_info": {"sec_user_id": "SEC_B"}}) == "SEC_B"
    assert _extract_self_sec_user_id({"data": {"user": {"sec_uid": "SEC_C"}}}) == "SEC_C"
    assert _extract_self_sec_user_id({"status_code": 0}) is None


def test_extract_self_profile_handles_nested_user_payload() -> None:
    payload = {
        "data": {
            "user": {
                "nickname": "抖音小号",
                "unique_id": "douyin_123",
                "sec_uid": "SEC_SELF",
                "avatar_thumb": {"url_list": ["https://example.com/me.jpeg"]},
            }
        }
    }

    assert _extract_self_profile(payload) == {
        "nickname": "抖音小号",
        "unique_id": "douyin_123",
        "sec_uid": "SEC_SELF",
        "avatar_url": "https://example.com/me.jpeg",
    }


def test_auth_panel_prefers_scan_code_login() -> None:
    assert "window.showAccount" in SHOW_LOGIN_PANEL_JS
    assert "LOGIN_SCAN_CODE" in SHOW_LOGIN_PANEL_JS
    assert "登录" in CLICK_LOGIN_BUTTON_JS


def test_qr_screenshot_rejects_one_click_login_card_shape() -> None:
    assert _is_qr_like_box({"width": 179, "height": 179}) is True
    assert _is_qr_like_box({"width": 344, "height": 222}) is False
    assert _is_qr_like_box({"width": 344, "height": 344}) is False


def test_qr_candidate_prefers_data_image_and_rejects_remote_app_art() -> None:
    qr_meta = {"tag": "IMG", "src": "data:image/png;base64,abc", "text": "", "class": "RhjdbXj8"}
    hidden_qr_meta = {
        "tag": "IMG",
        "src": "data:image/png;base64,abc",
        "text": "",
        "class": "RhjdbXj8",
        "visible": False,
    }
    app_meta = {
        "tag": "IMG",
        "src": "https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/app.png",
        "text": "",
        "class": "RtNruesw",
    }

    assert _qr_candidate_priority(qr_meta, {"width": 179, "height": 179}) == 0
    assert _qr_candidate_priority(hidden_qr_meta, {"width": 179, "height": 179}) is None
    assert _qr_candidate_priority(app_meta, {"width": 128, "height": 123}) is None


def test_auth_refresh_does_not_overwrite_qr_with_one_click_state() -> None:
    assert _should_refresh_login_screenshot({"has_qr": True}) is True
    assert _should_refresh_login_screenshot({"has_one_click_login": True}) is False
    assert "一键登录" in CONFIRM_SCANNED_LOGIN_JS


def test_auth_qr_api_payload_extracts_image_and_expiry() -> None:
    qrcode, expire_time = _extract_auth_qr_payload({
        "data": {
            "qrcode": "data:image/png;base64,aGVsbG8=",
            "expire_time": "100",
        }
    })

    assert qrcode == "aGVsbG8="
    assert expire_time == 100
    assert _auth_qr_ttl_seconds(expire_time, now=70) == 30


def test_auth_qr_payload_writes_stable_display_copy_and_metadata() -> None:
    with TemporaryDirectory() as tmp:
        crawler = DouyinCrawler()
        captures = []
        crawler._auth_qr_capture_callback = captures.append
        qr_path = Path(tmp) / "douyin-login.png"

        capture = crawler._save_auth_qr_payload(
            {"data": {"qrcode": "aGVsbG8=", "expire_time": "9999999999"}},
            qr_path,
        )

        assert qr_path.read_bytes() == b"hello"
        assert capture.display_path is not None
        assert capture.display_path != qr_path
        assert capture.display_path.read_bytes() == b"hello"
        assert capture.display_path == _auth_qr_display_path(qr_path, capture.saved_at)
        metadata = json.loads(qr_path.with_suffix(".json").read_text(encoding="utf-8"))
        assert metadata["display_path"] == str(capture.display_path)
        assert captures == [capture]


def test_auth_refresh_uses_server_expiry_and_regenerates_qr() -> None:
    source = inspect.getsource(DouyinCrawler.authorize_by_qr)
    regen_source = inspect.getsource(DouyinCrawler._regenerate_login_qr)

    assert AUTH_QR_REGENERATE_FALLBACK_INTERVAL_S <= 60
    assert _next_auth_qr_refresh_time(100, now=70) == 100 - AUTH_QR_REGENERATE_SAFETY_MARGIN_S
    assert _next_auth_qr_refresh_time(None, now=70) == 70 + AUTH_QR_REGENERATE_FALLBACK_INTERVAL_S
    assert "_attach_auth_qr_capture" in source
    assert "_regenerate_login_qr" in source
    assert "reload" in regen_source
    assert "closed" in regen_source
    assert "window.showAccount" in REGENERATE_LOGIN_QR_JS
    assert "LOGIN_SCAN_CODE" in REGENERATE_LOGIN_QR_JS


def test_auth_panel_does_not_wait_for_douyin_network_idle_before_showing_qr() -> None:
    source = inspect.getsource(DouyinCrawler._open_auth_panel)

    assert "wait_for_load_state(\"networkidle\"" not in source


def test_auth_panel_uses_short_show_account_wait_before_login_button_fallback() -> None:
    source = inspect.getsource(DouyinCrawler._open_auth_panel)

    assert AUTH_SHOW_ACCOUNT_WAIT_S <= 3
    assert "show_account_deadline" in source
    assert "trigger_login_button_fallback" in source
    assert source.index("CLICK_LOGIN_BUTTON_JS") < source.index("time.sleep(0.1)")
    assert "typeof window.showAccount === 'function'" in source
    assert 'wait_until="commit"' in source
    assert "time.sleep(0.1)" in source


def test_qr_auth_notifies_scan_confirmation_quickly() -> None:
    source = inspect.getsource(DouyinCrawler.authorize_by_qr)
    visual_source = inspect.getsource(DouyinCrawler._wait_for_auth_visual)

    assert "on_login_confirmed" in source
    assert "notify_login_confirmed()" in source
    assert "_has_usable_login_state(timeout_ms=1_000)" in source
    assert "time.sleep(2)" not in source
    assert "timeout_ms=10_000" not in source
    assert "time.sleep(0.25)" in source
    assert "time.sleep(0.5)" not in source
    assert "time.sleep(0.2)" in visual_source


def test_qr_auth_reports_phone_scan_pending_before_confirmation() -> None:
    source = inspect.getsource(DouyinCrawler.authorize_by_qr)
    visual_source = inspect.getsource(DouyinCrawler._auth_visual_state)

    assert "on_scan_pending" in source
    assert "notify_scan_pending()" in source
    assert "scan_pending_notified" in source
    assert "has_scan_pending" in source
    assert "last_api_login_check_at" in source
    assert "self._is_api_logged_in(timeout_ms=1_500)" in source
    assert "扫码成功" in visual_source
    assert "请在手机上确认" in visual_source
    assert "has_scan_pending" in visual_source


def test_qr_auth_detects_phone_cancel_or_expired_qr_state() -> None:
    source = inspect.getsource(DouyinCrawler.authorize_by_qr)
    visual_source = inspect.getsource(DouyinCrawler._auth_visual_state)

    assert "has_login_cancelled" in source
    assert "手机取消了登录，请重新扫码。" in source
    assert "登录已取消" in visual_source
    assert "二维码已失效" in visual_source
    assert "has_login_cancelled" in visual_source


class _FakeQrAuthCrawler(DouyinCrawler):
    def __init__(
        self,
        visual_states: list[dict],
        *,
        usable_login_results: list[bool] | None = None,
        api_login_results: list[bool] | None = None,
        capture_qr: bool = True,
    ) -> None:
        super().__init__()
        self._page = object()
        self.visual_states = list(visual_states)
        self.usable_login_results = list(usable_login_results or [False])
        self.api_login_results = list(api_login_results or [False])
        self.capture_qr = capture_qr
        self.confirm_clicks = 0
        self.detached_handler = None
        self.saved_screenshots: list[tuple[Path, bool]] = []

    def _has_usable_login_state(self, timeout_ms: int = 5_000) -> bool:
        if self.usable_login_results:
            return self.usable_login_results.pop(0)
        return False

    def _attach_auth_qr_capture(self, screenshot_path: Path):
        if self.capture_qr:
            self._auth_qr_capture = AuthQrCapture(
                path=screenshot_path,
                saved_at=0,
                expire_time=9_999_999_999,
                ttl_seconds=3600,
                display_path=screenshot_path,
            )
            if self._auth_qr_capture_callback:
                self._auth_qr_capture_callback(self._auth_qr_capture)
        return "fake-handler"

    def _detach_auth_qr_capture(self, handler) -> None:
        self.detached_handler = handler

    def _open_auth_panel(self, panel_timeout_s: int = 60) -> None:
        return None

    def _auth_visual_state(self) -> dict:
        if self.visual_states:
            return self.visual_states.pop(0)
        return {}

    def _confirm_scanned_login_if_needed(self) -> bool:
        self.confirm_clicks += 1
        return True

    def _save_login_screenshot(self, screenshot_path: Path, allow_page_fallback: bool = False) -> bool:
        self.saved_screenshots.append((screenshot_path, allow_page_fallback))
        return True

    def _login_cookie_fingerprint(self) -> tuple:
        return ()

    def _is_api_logged_in(self, timeout_ms: int = 10_000) -> bool:
        if self.api_login_results:
            return self.api_login_results.pop(0)
        return False


def test_qr_auth_mock_flow_notifies_scan_pending_once_before_confirmed() -> None:
    with TemporaryDirectory() as tmp:
        events: list[str] = []
        crawler = _FakeQrAuthCrawler(
            [
                {"has_qr": True},
                {"has_scan_pending": True},
                {"has_scan_pending": True},
                {"has_one_click_login": True},
            ],
            usable_login_results=[False, True],
        )

        result = crawler.authorize_by_qr(
            timeout_s=3,
            panel_timeout_s=1,
            screenshot_path=Path(tmp) / "douyin-login.png",
            on_qr_capture=lambda _capture: events.append("qr"),
            on_scan_pending=lambda: events.append("scan_pending"),
            on_login_confirmed=lambda: events.append("confirmed"),
        )

    assert result.success is True
    assert events == ["qr", "scan_pending", "confirmed"]
    assert crawler.confirm_clicks == 1
    assert crawler.detached_handler == "fake-handler"


def test_qr_auth_mock_flow_returns_cancel_without_confirming() -> None:
    with TemporaryDirectory() as tmp:
        events: list[str] = []
        crawler = _FakeQrAuthCrawler(
            [{"has_login_cancelled": True}],
            capture_qr=False,
        )

        result = crawler.authorize_by_qr(
            timeout_s=3,
            panel_timeout_s=1,
            screenshot_path=Path(tmp) / "douyin-login.png",
            on_scan_pending=lambda: events.append("scan_pending"),
            on_login_confirmed=lambda: events.append("confirmed"),
        )

    assert result.success is False
    assert "手机取消了登录" in result.message
    assert events == []
    assert crawler.confirm_clicks == 0


def test_qr_auth_mock_flow_reports_expired_qr_separately_from_phone_cancel() -> None:
    with TemporaryDirectory() as tmp:
        crawler = _FakeQrAuthCrawler(
            [{"has_expired_qr": True}],
            capture_qr=False,
        )

        result = crawler.authorize_by_qr(
            timeout_s=0,
            panel_timeout_s=1,
            screenshot_path=Path(tmp) / "douyin-login.png",
        )

    assert result.success is False
    assert result.message == "二维码已失效，请重新生成二维码。"
    assert "手机取消" not in result.message


def test_login_required_payload_detects_common_failures() -> None:
    assert _is_login_required_payload({"status_code": 8, "status_msg": "login required"}) is True
    assert _is_login_required_payload({"status_code": 10008, "message": "请登录"}) is True
    assert _is_login_required_payload({"status_code": 5, "uid": 0, "sec_uid": ""}) is True
    assert _is_login_required_payload({"status_code": 0, "aweme_list": []}) is False


def test_auth_flow_state_machine_covers_qr_scan_confirm_cancel_timeout_and_expiry() -> None:
    cases = [
        ({"event": "starting"}, "qr_generating", "正在生成二维码"),
        ({"visual_state": {"has_qr": True}}, "waiting_scan", "请用抖音 App 扫描二维码"),
        ({"visual_state": {"has_scan_pending": True}}, "scan_pending", "等待你在抖音 App 确认登录"),
        ({"visual_state": {"has_one_click_login": True}}, "confirmed", "正在保存登录状态"),
        ({"event": "login_confirmed"}, "confirmed", "正在保存登录状态"),
        ({"event": "success"}, "success", "绑定成功"),
        ({"visual_state": {"has_login_cancelled": True}}, "cancelled", "手机取消了登录"),
        ({"visual_state": {"has_expired_qr": True}}, "timeout", "二维码已失效"),
        ({"event": "timeout"}, "timeout", "扫码超时"),
        ({"api_payload": {"status_code": 10008, "message": "请登录"}}, "login_invalid", "登录态已失效"),
    ]

    for kwargs, expected_status, expected_message in cases:
        state = classify_auth_flow_state(**kwargs)
        assert state["status"] == expected_status
        assert expected_message in state["message"]


def test_auth_flow_state_machine_trace_covers_all_mocked_states_without_phone() -> None:
    from src.crawler.douyin import trace_auth_flow_state_machine

    flows = {
        "successful_qr_flow": [
            {"event": "starting"},
            {"visual_state": {"has_qr": True}},
            {"visual_state": {"has_scan_pending": True}},
            {"event": "login_confirmed"},
            {"event": "success"},
        ],
        "phone_cancelled": [
            {"event": "starting"},
            {"visual_state": {"has_qr": True}},
            {"visual_state": {"has_login_cancelled": True}},
        ],
        "qr_timeout": [
            {"event": "starting"},
            {"visual_state": {"has_qr": True}},
            {"event": "timeout"},
        ],
        "login_invalid": [
            {"event": "starting"},
            {"api_payload": {"status_code": 10008, "message": "请登录"}},
        ],
    }

    traces = {
        name: trace_auth_flow_state_machine(signals)
        for name, signals in flows.items()
    }

    assert traces["successful_qr_flow"]["statuses"] == [
        "qr_generating",
        "waiting_scan",
        "scan_pending",
        "confirmed",
        "success",
    ]
    assert traces["phone_cancelled"]["terminal_status"] == "cancelled"
    assert traces["qr_timeout"]["terminal_status"] == "timeout"
    assert traces["login_invalid"]["terminal_status"] == "login_invalid"

    covered = {
        status
        for trace in traces.values()
        for status in trace["covered_statuses"]
    }
    assert covered >= {
        "qr_generating",
        "waiting_scan",
        "scan_pending",
        "confirmed",
        "success",
        "cancelled",
        "timeout",
        "login_invalid",
    }
    for trace in traces.values():
        assert all(step["message"] for step in trace["states"])


def test_crawler_defaults_to_hidden_api_mode() -> None:
    sig = inspect.signature(DouyinCrawler)

    assert sig.parameters["headless"].default is True
    assert sig.parameters["api_mode"].default is True


def test_cli_exposes_auth_and_hidden_crawl_options() -> None:
    runner = CliRunner()

    auth = runner.invoke(cli, ["auth", "--help"])
    assert auth.exit_code == 0
    assert "扫码授权" in auth.output
    assert "--visible-debug" in auth.output
    assert "--panel-timeout" in auth.output

    crawl = runner.invoke(cli, ["crawl", "--help"])
    assert crawl.exit_code == 0
    assert "默认后台" in crawl.output
    assert "--legacy-scroll" in crawl.output
    assert "--dry-run" in crawl.output
    assert "--max-pages" in crawl.output
    assert "--allow-large-removal" in crawl.output

    crawl_likes = runner.invoke(cli, ["crawl-likes", "--help"])
    assert crawl_likes.exit_code == 0
    assert "喜欢" in crawl_likes.output
    assert "--dry-run" in crawl_likes.output
    assert "--max-pages" in crawl_likes.output
    assert "--allow-large-removal" in crawl_likes.output

    uncollect = runner.invoke(cli, ["uncollect", "--help"])
    assert uncollect.exit_code == 0
    assert "默认后台" in uncollect.output
    assert "后台 profile" in uncollect.output
    assert "--cdp" in uncollect.output

    unlike = runner.invoke(cli, ["unlike", "--help"])
    assert unlike.exit_code == 0
    assert "取消喜欢" in unlike.output
    assert "后台 profile" in unlike.output
    assert "--cdp" in unlike.output

    index = runner.invoke(cli, ["index", "--help"])
    assert index.exit_code == 0
    assert "--kind" in index.output
    assert "likes" in index.output

    categorize = runner.invoke(cli, ["categorize", "--help"])
    assert categorize.exit_code == 0
    assert "--kind" in categorize.output
    assert "likes" in categorize.output

    verify_backup = runner.invoke(cli, ["verify-backup", "--help"])
    assert verify_backup.exit_code == 0
    assert "--output" in verify_backup.output
    assert "--path" in verify_backup.output

    prune_backups = runner.invoke(cli, ["prune-backups", "--help"])
    assert prune_backups.exit_code == 0
    assert "--output" in prune_backups.output
    assert "--keep" in prune_backups.output
    assert "--apply" in prune_backups.output
    assert "--json" in prune_backups.output

    rollback = runner.invoke(cli, ["rollback-from-manifest", "--help"])
    assert rollback.exit_code == 0
    assert "--manifest" in rollback.output
    assert "--apply" in rollback.output
    assert "--json" in rollback.output


def test_verify_backup_cli_reports_missing_backups() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        result = runner.invoke(cli, ["verify-backup", "--output", tmp])

    combined = result.output + getattr(result, "stderr", "")
    assert result.exit_code == 1
    assert "没有找到可校验的备份文件。" in combined


def test_verify_backup_cli_validates_latest_backup() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "recall-backup-20260705-100000.db"
        create_backup_db(backup_path, favorite_title="valid cli backup")

        result = runner.invoke(cli, ["verify-backup", "--output", tmp])

    assert result.exit_code == 0
    assert "SQLite backup OK:" in result.output
    assert "integrity: ok" in result.output
    assert "required tables: ok" in result.output
    assert "favorites: 1" in result.output
    assert str(backup_path) in result.output


def test_prune_backups_cli_dry_run_reports_candidates_without_deleting() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        old_backup = backup_dir / "recall-backup-20260705-100000.db"
        kept_backup = backup_dir / "recall-backup-20260705-110000.db"
        newest_backup = backup_dir / "recall-backup-20260705-120000.db"
        protected_install = backup_dir / "pre-install-recall-20260705-090000.db"
        protected_restore = backup_dir / "pre-restore-recall-20260705-080000.db"
        for path in (old_backup, kept_backup, newest_backup, protected_install, protected_restore):
            create_backup_marker(path)

        result = runner.invoke(cli, ["prune-backups", "--output", tmp, "--keep", "2"])

        assert result.exit_code == 0
        assert old_backup.exists()
        assert kept_backup.exists()
        assert newest_backup.exists()
        assert protected_install.exists()
        assert protected_restore.exists()
        assert "dry-run" in result.output
        assert "不会删除文件" in result.output
        assert old_backup.name in result.output
        assert protected_install.name in result.output
        assert protected_restore.name in result.output


def test_prune_backups_cli_apply_deletes_only_old_ordinary_backups() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        old_backup = backup_dir / "recall-backup-20260705-100000.db"
        kept_backup = backup_dir / "recall-backup-20260705-110000.db"
        newest_backup = backup_dir / "recall-backup-20260705-120000.db"
        protected_install = backup_dir / "pre-install-recall-20260705-090000.db"
        protected_restore = backup_dir / "pre-restore-recall-20260705-080000.db"
        for path in (old_backup, kept_backup, newest_backup, protected_install, protected_restore):
            create_backup_marker(path)

        result = runner.invoke(cli, ["prune-backups", "--output", tmp, "--keep", "2", "--apply"])

        assert result.exit_code == 0
        assert not old_backup.exists()
        assert kept_backup.exists()
        assert newest_backup.exists()
        assert protected_install.exists()
        assert protected_restore.exists()
        assert "已删除 1 个旧普通备份" in result.output
        assert "one_file_at_a_time" in result.output
        assert old_backup.name in result.output


def test_prune_backups_cli_json_dry_run_is_machine_readable() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        old_backup = backup_dir / "recall-backup-20260705-100000.db"
        newest_backup = backup_dir / "recall-backup-20260705-120000.db"
        protected_install = backup_dir / "pre-install-recall-20260705-090000.db"
        for path in (old_backup, newest_backup, protected_install):
            create_backup_marker(path)

        result = runner.invoke(cli, ["prune-backups", "--output", tmp, "--keep", "1", "--json"])
        payload = json.loads(result.output)

        assert result.exit_code == 0
        assert old_backup.exists()
        assert payload["ok"] is True
        assert payload["mode"] == "dry_run"
        assert payload["output_dir"] == str(backup_dir)
        assert [item["name"] for item in payload["report"]["delete_candidates"]] == [old_backup.name]
    assert [item["name"] for item in payload["report"]["protected"]] == [protected_install.name]


def test_rollback_from_manifest_cli_dry_run_validates_without_restoring() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_path = root / "pre-release-recall-cli.db"
        current_db = root / "current.db"
        manifest_path = root / "delivery-manifest-cli.json"
        create_backup_db(backup_path, favorite_title="manifest restored")
        create_backup_db(current_db, favorite_title="current")
        write_manifest_for_backup(manifest_path, backup_path)

        result = runner.invoke(
            cli,
            [
                "rollback-from-manifest",
                "--manifest",
                str(manifest_path),
                "--db-path",
                str(current_db),
                "--json",
            ],
        )
        payload = json.loads(result.output)

        conn = sqlite3.connect(current_db)
        try:
            title = conn.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            conn.close()
        assert result.exit_code == 0
        assert payload["ok"] is True
        assert payload["mode"] == "dry_run"
        assert payload["restored"] is False
        assert title == "current"


def test_rollback_from_manifest_cli_apply_restores_valid_manifest_backup() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_path = root / "pre-release-recall-cli-apply.db"
        current_db = root / "current.db"
        safety_dir = root / "safety"
        manifest_path = root / "delivery-manifest-cli-apply.json"
        create_backup_db(backup_path, favorite_title="manifest restored")
        create_backup_db(current_db, favorite_title="current")
        write_manifest_for_backup(manifest_path, backup_path)

        result = runner.invoke(
            cli,
            [
                "rollback-from-manifest",
                "--manifest",
                str(manifest_path),
                "--db-path",
                str(current_db),
                "--backup-dir",
                str(safety_dir),
                "--apply",
            ],
        )

        conn = sqlite3.connect(current_db)
        try:
            title = conn.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            conn.close()
        assert result.exit_code == 0
        assert title == "manifest restored"
        assert "已按 delivery manifest 恢复" in result.output
        assert "pre-restore-recall" in result.output


def test_rollback_from_manifest_cli_rejects_sha_mismatch() -> None:
    runner = CliRunner()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        backup_path = root / "pre-release-recall-cli-bad.db"
        current_db = root / "current.db"
        manifest_path = root / "delivery-manifest-cli-bad.json"
        create_backup_db(backup_path, favorite_title="manifest restored")
        create_backup_db(current_db, favorite_title="current")
        write_manifest_for_backup(manifest_path, backup_path, sha256="bad-sha")

        result = runner.invoke(
            cli,
            [
                "rollback-from-manifest",
                "--manifest",
                str(manifest_path),
                "--db-path",
                str(current_db),
            ],
        )

        conn = sqlite3.connect(current_db)
        try:
            title = conn.execute("SELECT title FROM favorites WHERE id = 'fav-1'").fetchone()[0]
        finally:
            conn.close()
        combined = result.output + getattr(result, "stderr", "")
        assert result.exit_code == 1
        assert "SHA256" in combined
        assert title == "current"


def test_doctor_cli_json_returns_stable_reusable_sections() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] in {True, False}
    assert {
        "python",
        "uv",
        "playwright",
        "chromium",
        "sqlite",
        "database",
        "backups",
        "jobs",
        "web_service",
        "model_cache",
        "avatar_cache",
        "smtp",
    } <= set(payload["checks"])
    assert {"status", "ok", "message", "details"} <= set(payload["checks"]["database"])
    assert "checked_at" in payload


def test_doctor_cli_text_uses_chinese_summary_without_raw_json() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0
    assert "环境诊断" in result.output
    assert '"checks"' not in result.output
    assert "数据库" in result.output


def test_console_safe_replaces_characters_current_terminal_cannot_encode() -> None:
    assert _console_safe("音乐🙈", encoding="gbk") == "音乐?"
    assert _console_safe("音乐🙈", encoding="utf-8") == "音乐🙈"


if __name__ == "__main__":
    tests = [
        test_collection_query_contains_cursor_count_and_web_params,
        test_collection_api_fetch_uses_post,
        test_likes_query_contains_sec_uid_cursor_count_and_web_params,
        test_likes_api_fetch_uses_get_and_favorite_endpoint,
        test_extract_self_sec_user_id_handles_known_payload_shapes,
        test_extract_self_profile_handles_nested_user_payload,
        test_auth_panel_prefers_scan_code_login,
        test_qr_screenshot_rejects_one_click_login_card_shape,
        test_qr_candidate_prefers_data_image_and_rejects_remote_app_art,
        test_auth_refresh_does_not_overwrite_qr_with_one_click_state,
        test_auth_qr_api_payload_extracts_image_and_expiry,
        test_auth_qr_payload_writes_stable_display_copy_and_metadata,
        test_auth_refresh_uses_server_expiry_and_regenerates_qr,
        test_auth_panel_does_not_wait_for_douyin_network_idle_before_showing_qr,
        test_auth_panel_uses_short_show_account_wait_before_login_button_fallback,
        test_qr_auth_notifies_scan_confirmation_quickly,
        test_qr_auth_reports_phone_scan_pending_before_confirmation,
        test_qr_auth_detects_phone_cancel_or_expired_qr_state,
        test_qr_auth_mock_flow_notifies_scan_pending_once_before_confirmed,
        test_qr_auth_mock_flow_returns_cancel_without_confirming,
        test_qr_auth_mock_flow_reports_expired_qr_separately_from_phone_cancel,
        test_login_required_payload_detects_common_failures,
        test_auth_flow_state_machine_covers_qr_scan_confirm_cancel_timeout_and_expiry,
        test_auth_flow_state_machine_trace_covers_all_mocked_states_without_phone,
        test_crawler_defaults_to_hidden_api_mode,
        test_cli_exposes_auth_and_hidden_crawl_options,
        test_verify_backup_cli_reports_missing_backups,
        test_verify_backup_cli_validates_latest_backup,
        test_prune_backups_cli_dry_run_reports_candidates_without_deleting,
        test_prune_backups_cli_apply_deletes_only_old_ordinary_backups,
        test_prune_backups_cli_json_dry_run_is_machine_readable,
        test_rollback_from_manifest_cli_dry_run_validates_without_restoring,
        test_rollback_from_manifest_cli_apply_restores_valid_manifest_backup,
        test_rollback_from_manifest_cli_rejects_sha_mismatch,
        test_doctor_cli_json_returns_stable_reusable_sections,
        test_doctor_cli_text_uses_chinese_summary_without_raw_json,
        test_console_safe_replaces_characters_current_terminal_cannot_encode,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
