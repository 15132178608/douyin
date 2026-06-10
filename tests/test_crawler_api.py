"""
Crawler API-mode tests.

Run:
    python tests/test_crawler_api.py
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

from click.testing import CliRunner

from src.cli import _console_safe, cli
from src.crawler.douyin import (
    AUTH_QR_REGENERATE_FALLBACK_INTERVAL_S,
    AUTH_QR_REGENERATE_SAFETY_MARGIN_S,
    DouyinCrawler,
    CLICK_LOGIN_BUTTON_JS,
    CONFIRM_SCANNED_LOGIN_JS,
    FETCH_COLLECTION_BY_API_JS,
    FETCH_LIKES_BY_API_JS,
    REGENERATE_LOGIN_QR_JS,
    SHOW_LOGIN_PANEL_JS,
    _auth_qr_ttl_seconds,
    _auth_qr_display_path,
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


def test_login_required_payload_detects_common_failures() -> None:
    assert _is_login_required_payload({"status_code": 8, "status_msg": "login required"}) is True
    assert _is_login_required_payload({"status_code": 10008, "message": "请登录"}) is True
    assert _is_login_required_payload({"status_code": 5, "uid": 0, "sec_uid": ""}) is True
    assert _is_login_required_payload({"status_code": 0, "aweme_list": []}) is False


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
        test_login_required_payload_detects_common_failures,
        test_crawler_defaults_to_hidden_api_mode,
        test_cli_exposes_auth_and_hidden_crawl_options,
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
