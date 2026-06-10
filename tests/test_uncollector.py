"""
取消收藏链路的纯函数测试。

跑法：
    python tests/test_uncollector.py
"""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import threading

from src.uncollector import douyin as douyin_mod
from src.uncollector.douyin import (
    PersistentUncollectBridge,
    UncollectResult,
    _collect_body,
    _digg_body,
    _is_douyin_page_url,
    _parse_collect_response,
    _parse_digg_response,
    uncollect_many,
    uncollect_one,
)


def test_collect_body_uses_action_zero_for_uncollect() -> None:
    body = _collect_body("7448853910531460402", action=0)
    assert body == "action=0&aweme_id=7448853910531460402&aweme_type=0"


def test_digg_body_uses_type_zero_for_unlike() -> None:
    body = _digg_body("7448853910531460402", digg_type=0)
    assert body == "aweme_id=7448853910531460402&item_type=0&type=0"


def test_parse_collect_response_success_when_flag_false() -> None:
    ok, message, already = _parse_collect_response(
        {"status_code": 0, "collects_flag": False}
    )
    assert ok is True
    assert already is False
    assert "成功" in message


def test_parse_collect_response_rejects_nonzero_status() -> None:
    ok, message, already = _parse_collect_response(
        {"status_code": 8, "status_msg": "login required"}
    )
    assert ok is False
    assert already is False
    assert "login required" in message


def test_parse_digg_response_accepts_status_zero_for_unlike() -> None:
    ok, message = _parse_digg_response({"status_code": 0})
    assert ok is True
    assert "取消喜欢成功" in message


def test_parse_digg_response_rejects_nonzero_status() -> None:
    ok, message = _parse_digg_response({"status_code": 8, "status_msg": "login required"})
    assert ok is False
    assert "login required" in message


def test_douyin_page_url_detection_accepts_only_douyin_origins() -> None:
    assert _is_douyin_page_url("https://www.douyin.com/video/123") is True
    assert _is_douyin_page_url("https://www-hj.douyin.com/aweme/v1/web/") is True
    assert _is_douyin_page_url("https://example.com/video/123") is False
    assert _is_douyin_page_url("about:blank") is False


def test_uncollect_one_does_not_open_video_page_fallback_by_default() -> None:
    sig = inspect.signature(uncollect_one)
    param = sig.parameters["allow_page_fallback"]
    assert param.default is False


def test_uncollect_defaults_to_background_profile_not_cdp() -> None:
    one_sig = inspect.signature(uncollect_one)
    many_sig = inspect.signature(uncollect_many)

    assert one_sig.parameters["cdp_endpoint"].default is None
    assert many_sig.parameters["cdp_endpoint"].default is None
    assert many_sig.parameters["headless"].default is True


def test_persistent_bridge_reuses_api_context_until_closed() -> None:
    calls: list[str] = []

    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        def close(self) -> None:
            calls.append("page.close")
            self.closed = True

    fake_page = FakePage()

    @contextmanager
    def fake_api_context(cdp_endpoint, headless, hide_window, browser_channel):
        calls.append("context.enter")
        try:
            yield object(), "profile"
        finally:
            calls.append("context.exit")

    def fake_prepare_api_page(context, timeout_ms):
        calls.append("prepare")
        return fake_page, True

    def fake_uncollect_by_api(page, aweme_id, timeout_ms):
        calls.append(f"api:{aweme_id}")
        return UncollectResult(True, "ok")

    def fake_unlike_by_api(page, aweme_id, timeout_ms):
        calls.append(f"digg:{aweme_id}")
        return UncollectResult(True, "ok")

    originals = (
        douyin_mod._api_context,
        douyin_mod._prepare_api_page,
        douyin_mod._uncollect_by_api,
        douyin_mod._unlike_by_api,
    )
    douyin_mod._api_context = fake_api_context
    douyin_mod._prepare_api_page = fake_prepare_api_page
    douyin_mod._uncollect_by_api = fake_uncollect_by_api
    douyin_mod._unlike_by_api = fake_unlike_by_api
    try:
        bridge = PersistentUncollectBridge()
        bridge.warmup()
        assert bridge.uncollect_one("a").success is True
        assert bridge.unlike_one("liked").success is True
        assert bridge.uncollect_one("b").success is True
        assert calls == ["context.enter", "prepare", "api:a", "digg:liked", "api:b"]

        bridge.close()
        assert calls == [
            "context.enter", "prepare", "api:a", "digg:liked", "api:b",
            "page.close", "context.exit",
        ]
    finally:
        (
            douyin_mod._api_context,
            douyin_mod._prepare_api_page,
            douyin_mod._uncollect_by_api,
            douyin_mod._unlike_by_api,
        ) = originals


def test_persistent_worker_runs_bridge_on_single_owner_thread() -> None:
    calls: list[tuple[str, int, str | None]] = []

    class FakeBridge:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def warmup(self) -> bool:
            calls.append(("warmup", threading.get_ident(), None))
            return True

        def uncollect_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
            calls.append(("uncollect", threading.get_ident(), aweme_id))
            return UncollectResult(True, f"ok:{aweme_id}")

        def unlike_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
            calls.append(("unlike", threading.get_ident(), aweme_id))
            return UncollectResult(True, f"ok:{aweme_id}")

        def close(self) -> None:
            calls.append(("close", threading.get_ident(), None))

    original_bridge = douyin_mod.PersistentUncollectBridge
    douyin_mod.PersistentUncollectBridge = FakeBridge
    try:
        worker = douyin_mod.PersistentUncollectWorker()
        assert worker.warmup() is True
        assert worker.uncollect_one("a").success is True
        assert worker.unlike_one("liked").success is True
        assert worker.uncollect_one("b").success is True
        worker.close()

        worker_thread_ids = {thread_id for _, thread_id, _ in calls}
        assert len(worker_thread_ids) == 1
        assert [name for name, _, _ in calls] == ["warmup", "uncollect", "unlike", "uncollect", "close"]
    finally:
        douyin_mod.PersistentUncollectBridge = original_bridge


def test_persistent_worker_exposes_ready_status_after_warmup() -> None:
    class FakeBridge:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def warmup(self) -> bool:
            return True

        def uncollect_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
            return UncollectResult(True, "ok")

        def unlike_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
            return UncollectResult(True, "ok")

        def close(self) -> None:
            pass

    original_bridge = douyin_mod.PersistentUncollectBridge
    douyin_mod.PersistentUncollectBridge = FakeBridge
    try:
        worker = douyin_mod.PersistentUncollectWorker()
        assert worker.is_ready() is False
        assert worker.warmup() is True
        assert worker.is_ready() is True
        status = worker.status()
        assert status["ready"] is True
        assert status["last_error"] is None
        worker.close()
    finally:
        douyin_mod.PersistentUncollectBridge = original_bridge


if __name__ == "__main__":
    tests = [
        test_collect_body_uses_action_zero_for_uncollect,
        test_digg_body_uses_type_zero_for_unlike,
        test_parse_collect_response_success_when_flag_false,
        test_parse_collect_response_rejects_nonzero_status,
        test_parse_digg_response_accepts_status_zero_for_unlike,
        test_parse_digg_response_rejects_nonzero_status,
        test_douyin_page_url_detection_accepts_only_douyin_origins,
        test_uncollect_one_does_not_open_video_page_fallback_by_default,
        test_uncollect_defaults_to_background_profile_not_cdp,
        test_persistent_bridge_reuses_api_context_until_closed,
        test_persistent_worker_runs_bridge_on_single_owner_thread,
        test_persistent_worker_exposes_ready_status_after_warmup,
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
