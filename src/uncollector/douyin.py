"""
通过 Playwright 复用本地登录态，用抖音 Web 自己的收藏接口取消收藏。

主路径不打开视频详情页：
- 默认复用 `recall auth` 写入的持久化 profile，在后台开一个 API bridge。
- 调试时也可以通过 CDP 复用已有的 douyin.com tab；没有时才打开一个 douyin 首页作为 API 签名环境。
- 在页面上下文里 fetch /aweme/v1/web/aweme/collect/，让抖音 Web SDK 自动补签名参数。
- body 里 action=0 表示取消收藏，action=1 表示收藏。
- 喜欢模块复用同一个 bridge，fetch /aweme/v1/web/commit/item/digg/；
  body 里 type=0 表示取消喜欢，type=1 表示喜欢。

页面点击逻辑只保留为显式 fallback，用于接口失效时人工排障。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import queue
import threading
from typing import Optional, Sequence
from urllib.parse import urlencode, urlparse

from loguru import logger

from src.config import settings


DEFAULT_CDP = "http://localhost:9222"
DOUYIN_HOME_URL = "https://www.douyin.com/"
VIDEO_PAGE_URL_TEMPLATE = "https://www.douyin.com/video/{aweme_id}"

# 抖音星形 Lottie 第一个 path 的特征坐标。注意真实 d 属性带前导空格
STAR_PATH_SIGNATURE = "M43.79"

# JS 函数：找到收藏按钮的可点击 div
FIND_COLLECT_BUTTON_JS = """
() => {
    const paths = document.querySelectorAll('svg path');
    for (const p of paths) {
        const d = p.getAttribute('d') || '';
        if (!d.includes('M43.79')) continue;
        // 找 tabindex="0" 祖先（ARIA 可聚焦的按钮）
        let el = p;
        while (el && el !== document.body) {
            if (el.getAttribute && el.getAttribute('tabindex') === '0') {
                return el;
            }
            el = el.parentElement;
        }
    }
    return null;
}
"""

# JS：判断当前收藏状态
GET_COLLECTED_STATE_JS = """
(button) => {
    const gs = button.querySelectorAll('svg g');
    for (const g of gs) {
        const yellow = g.querySelector('path[fill="rgb(255,184,2)"]');
        if (yellow) {
            const style = g.getAttribute('style') || '';
            if (style.includes('display: none')) return false;
            if (style.includes('display: block')) return true;
        }
    }
    return null;
}
"""

UNCOLLECT_BY_API_JS = """
async ({body}) => {
    const chromeVersion = (navigator.userAgent.match(/Chrome\\/([\\d.]+)/) || [])[1] || '';
    const connection = navigator.connection || {};
    const params = new URLSearchParams({
        device_platform: 'webapp',
        aid: '6383',
        channel: 'channel_pc_web',
        pc_client_type: '1',
        pc_libra_divert: 'Windows',
        update_version_code: '170400',
        support_h265: '1',
        support_dash: '1',
        version_code: '170400',
        version_name: '17.4.0',
        cookie_enabled: String(navigator.cookieEnabled),
        screen_width: String(screen.width),
        screen_height: String(screen.height),
        browser_language: navigator.language || 'zh-CN',
        browser_platform: navigator.platform || 'Win32',
        browser_name: 'Chrome',
        browser_version: chromeVersion,
        browser_online: String(navigator.onLine),
        engine_name: 'Blink',
        engine_version: chromeVersion,
        os_name: 'Windows',
        os_version: '10',
        cpu_core_num: String(navigator.hardwareConcurrency || 16),
        device_memory: String(navigator.deviceMemory || 8),
        platform: 'PC',
        downlink: String(connection.downlink || 1.5),
        effective_type: connection.effectiveType || '3g',
        round_trip_time: String(connection.rtt || 600),
    });
    const resp = await fetch('/aweme/v1/web/aweme/collect/?' + params.toString(), {
        method: 'POST',
        credentials: 'include',
        headers: {'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'},
        body,
    });
    const text = await resp.text();
    let payload = null;
    try {
        payload = JSON.parse(text);
    } catch (e) {
        payload = null;
    }
    return {
        http_status: resp.status,
        ok: resp.ok,
        payload,
        text,
    };
}
"""

DIGG_BY_API_JS = """
async ({body}) => {
    const chromeVersion = (navigator.userAgent.match(/Chrome\\/([\\d.]+)/) || [])[1] || '';
    const connection = navigator.connection || {};
    const params = new URLSearchParams({
        device_platform: 'webapp',
        aid: '6383',
        channel: 'channel_pc_web',
        pc_client_type: '1',
        pc_libra_divert: 'Windows',
        update_version_code: '170400',
        support_h265: '1',
        support_dash: '1',
        version_code: '170400',
        version_name: '17.4.0',
        cookie_enabled: String(navigator.cookieEnabled),
        screen_width: String(screen.width),
        screen_height: String(screen.height),
        browser_language: navigator.language || 'zh-CN',
        browser_platform: navigator.platform || 'Win32',
        browser_name: 'Chrome',
        browser_version: chromeVersion,
        browser_online: String(navigator.onLine),
        engine_name: 'Blink',
        engine_version: chromeVersion,
        os_name: 'Windows',
        os_version: '10',
        cpu_core_num: String(navigator.hardwareConcurrency || 16),
        device_memory: String(navigator.deviceMemory || 8),
        platform: 'PC',
        downlink: String(connection.downlink || 1.5),
        effective_type: connection.effectiveType || '3g',
        round_trip_time: String(connection.rtt || 600),
    });
    const resp = await fetch('/aweme/v1/web/commit/item/digg/?' + params.toString(), {
        method: 'POST',
        credentials: 'include',
        headers: {'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'},
        body,
    });
    const text = await resp.text();
    let payload = null;
    try {
        payload = JSON.parse(text);
    } catch (e) {
        payload = null;
    }
    return {
        http_status: resp.status,
        ok: resp.ok,
        payload,
        text,
    };
}
"""


@dataclass
class UncollectResult:
    success: bool
    message: str
    debug_html: Optional[str] = None
    already_uncollected: bool = False


class PersistentUncollectBridge:
    """Reusable Douyin API bridge for Web UI cleanup sessions."""

    def __init__(
        self,
        cdp_endpoint: Optional[str] = None,
        timeout_ms: int = 25000,
        allow_page_fallback: bool = False,
        headless: bool = True,
        hide_window: bool = True,
        browser_channel: Optional[str] = None,
        profile_path: Optional[Path] = None,
    ) -> None:
        self.cdp_endpoint = cdp_endpoint
        self.timeout_ms = timeout_ms
        self.allow_page_fallback = allow_page_fallback
        self.headless = headless
        self.hide_window = hide_window
        self.browser_channel = browser_channel
        self.profile_path = profile_path
        self._context_manager = None
        self._context = None
        self._context_kind: str | None = None
        self._page = None
        self._created_page = False
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            if self._created_page and self._page is not None:
                try:
                    self._page.close()
                except Exception:
                    pass
            if self._context_manager is not None:
                try:
                    self._context_manager.__exit__(None, None, None)
                except Exception:
                    pass
            self._context_manager = None
            self._context = None
            self._context_kind = None
            self._page = None
            self._created_page = False

    def _has_live_page(self) -> bool:
        if self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except Exception:
            return False

    def _ensure_page(self):
        if self._context is not None and self._has_live_page():
            return self._page

        self.close()
        context_kwargs = {}
        if self.profile_path is not None:
            context_kwargs["profile_path"] = self.profile_path
        self._context_manager = _api_context(
            self.cdp_endpoint,
            self.headless,
            self.hide_window,
            self.browser_channel,
            **context_kwargs,
        )
        self._context, self._context_kind = self._context_manager.__enter__()
        self._page, self._created_page = _prepare_api_page(self._context, self.timeout_ms)
        logger.info(
            "Persistent uncollect bridge ready: context={}, bridge_page={}",
            self._context_kind,
            "new" if self._created_page else "existing",
        )
        return self._page

    def warmup(self) -> bool:
        with self._lock:
            try:
                self._ensure_page()
                return True
            except Exception as e:
                logger.warning("Persistent uncollect bridge warmup failed: {}", e)
                self.close()
                return False

    def uncollect_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
        with self._lock:
            aweme_id = str(aweme_id).strip()
            if not aweme_id:
                return UncollectResult(False, "没有传入 aweme_id")

            logger.info("Persistent uncollect attempt: aweme_id={}, dry_run={}", aweme_id, dry_run)
            for attempt in range(2):
                try:
                    page = self._ensure_page()
                    if dry_run:
                        webdriver = page.evaluate("navigator.webdriver")
                        return UncollectResult(
                            True,
                            (
                                f"[DRY RUN] API 模式就绪，context={self._context_kind}，"
                                f"navigator.webdriver={webdriver!r}，未发送取消收藏请求。"
                            ),
                        )

                    api_result = _uncollect_by_api(page, aweme_id, timeout_ms=self.timeout_ms)
                    if api_result.success or not self.allow_page_fallback:
                        return api_result

                    logger.warning(
                        "API uncollect failed for {}: {}. Falling back to page click.",
                        aweme_id,
                        api_result.message,
                    )
                    return _uncollect_by_page_click(page, aweme_id, self.timeout_ms)
                except Exception as e:
                    logger.warning("Persistent uncollect bridge failed on attempt {}: {}", attempt + 1, e)
                    self.close()
                    if attempt == 0:
                        continue
                    logger.exception("Persistent uncollect failed for {}: {}", aweme_id, e)
                    return UncollectResult(False, f"异常：{e}")

        return UncollectResult(False, "取消收藏失败：未知错误")

    def unlike_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
        with self._lock:
            aweme_id = str(aweme_id).strip()
            if not aweme_id:
                return UncollectResult(False, "没有传入 aweme_id")

            logger.info("Persistent unlike attempt: aweme_id={}, dry_run={}", aweme_id, dry_run)
            for attempt in range(2):
                try:
                    page = self._ensure_page()
                    if dry_run:
                        webdriver = page.evaluate("navigator.webdriver")
                        return UncollectResult(
                            True,
                            (
                                f"[DRY RUN] API 模式就绪，context={self._context_kind}，"
                                f"navigator.webdriver={webdriver!r}，未发送取消喜欢请求。"
                            ),
                        )

                    return _unlike_by_api(page, aweme_id, timeout_ms=self.timeout_ms)
                except Exception as e:
                    logger.warning("Persistent unlike bridge failed on attempt {}: {}", attempt + 1, e)
                    self.close()
                    if attempt == 0:
                        continue
                    logger.exception("Persistent unlike failed for {}: {}", aweme_id, e)
                    return UncollectResult(False, f"异常：{e}")

        return UncollectResult(False, "取消喜欢失败：未知错误")


@dataclass
class _BridgeTask:
    action: str
    aweme_id: str | None = None
    dry_run: bool = False
    result_queue: queue.Queue | None = None


class PersistentUncollectWorker:
    """Own a persistent bridge on one dedicated thread and accept blocking calls."""

    def __init__(
        self,
        cdp_endpoint: Optional[str] = None,
        timeout_ms: int = 25000,
        allow_page_fallback: bool = False,
        headless: bool = True,
        hide_window: bool = True,
        browser_channel: Optional[str] = None,
        profile_path: Optional[Path] = None,
    ) -> None:
        self._bridge_kwargs = {
            "cdp_endpoint": cdp_endpoint,
            "timeout_ms": timeout_ms,
            "allow_page_fallback": allow_page_fallback,
            "headless": headless,
            "hide_window": hide_window,
            "browser_channel": browser_channel,
            "profile_path": profile_path,
        }
        self._tasks: queue.Queue[_BridgeTask] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self._ready_event = threading.Event()
        self._last_error: str | None = None

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._closed:
                raise RuntimeError("Persistent uncollect worker is closed")
            self._thread = threading.Thread(
                target=self._run,
                name="persistent-uncollect-worker",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        bridge = PersistentUncollectBridge(**self._bridge_kwargs)
        bridge_closed = False
        try:
            while True:
                task = self._tasks.get()
                result_queue = task.result_queue
                try:
                    if task.action == "close":
                        bridge.close()
                        bridge_closed = True
                        self._ready_event.clear()
                        if result_queue is not None:
                            result_queue.put(True)
                        break
                    if task.action == "warmup":
                        result = bridge.warmup()
                        if result:
                            self._ready_event.set()
                            self._last_error = None
                        else:
                            self._ready_event.clear()
                            self._last_error = "bridge warmup failed"
                    elif task.action == "uncollect":
                        result = bridge.uncollect_one(task.aweme_id or "", dry_run=task.dry_run)
                        if isinstance(result, UncollectResult) and result.success:
                            self._ready_event.set()
                            self._last_error = None
                    elif task.action == "unlike":
                        result = bridge.unlike_one(task.aweme_id or "", dry_run=task.dry_run)
                        if isinstance(result, UncollectResult) and result.success:
                            self._ready_event.set()
                            self._last_error = None
                    else:
                        result = RuntimeError(f"Unknown bridge task: {task.action}")
                except Exception as e:
                    logger.exception("Persistent uncollect worker task failed: {}", e)
                    self._ready_event.clear()
                    self._last_error = str(e)
                    result = e
                if result_queue is not None:
                    result_queue.put(result)
        finally:
            if not bridge_closed:
                bridge.close()

    def _submit(self, task: _BridgeTask):
        self._ensure_started()
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        task.result_queue = result_queue
        self._tasks.put(task)
        result = result_queue.get()
        if isinstance(result, Exception):
            raise result
        return result

    def warmup(self) -> bool:
        return bool(self._submit(_BridgeTask(action="warmup")))

    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    def status(self) -> dict[str, object]:
        return {
            "ready": self.is_ready(),
            "last_error": self._last_error,
        }

    def uncollect_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
        result = self._submit(_BridgeTask(action="uncollect", aweme_id=aweme_id, dry_run=dry_run))
        if isinstance(result, UncollectResult):
            return result
        return UncollectResult(False, f"取消收藏返回异常：{result!r}")

    def unlike_one(self, aweme_id: str, dry_run: bool = False) -> UncollectResult:
        result = self._submit(_BridgeTask(action="unlike", aweme_id=aweme_id, dry_run=dry_run))
        if isinstance(result, UncollectResult):
            return result
        return UncollectResult(False, f"取消喜欢返回异常：{result!r}")

    def close(self) -> None:
        with self._start_lock:
            thread = self._thread
            if self._closed:
                return
            self._closed = True
            self._ready_event.clear()
        if thread is None or not thread.is_alive():
            return
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        self._tasks.put(_BridgeTask(action="close", result_queue=result_queue))
        try:
            result_queue.get(timeout=10)
        except Exception:
            pass
        thread.join(timeout=10)


@contextmanager
def _cdp_page(cdp_endpoint: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass


@contextmanager
def _cdp_context(cdp_endpoint: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        yield context


@contextmanager
def _profile_context(
    headless: bool = True,
    hide_window: bool = True,
    browser_channel: Optional[str] = None,
    profile_path: Optional[Path] = None,
):
    from playwright.sync_api import sync_playwright
    from src.crawler.douyin import STEALTH_INIT_SCRIPT

    profile_dir = profile_path or settings.playwright_profile_path
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    if hide_window:
        launch_args.extend([
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
            "--start-minimized",
        ])

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            channel=browser_channel,
            viewport={"width": 1280, "height": 900},
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        try:
            context.add_init_script(STEALTH_INIT_SCRIPT)
            yield context
        finally:
            context.close()


@contextmanager
def _api_context(
    cdp_endpoint: Optional[str],
    headless: bool,
    hide_window: bool,
    browser_channel: Optional[str],
    profile_path: Optional[Path] = None,
):
    if cdp_endpoint:
        with _cdp_context(cdp_endpoint) as context:
            yield context, "cdp"
    else:
        with _profile_context(
            headless=headless,
            hide_window=hide_window,
            browser_channel=browser_channel,
            profile_path=profile_path,
        ) as context:
            yield context, "profile"


def _is_douyin_page_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "www.douyin.com",
        "www-hj.douyin.com",
    }


def _find_existing_douyin_page(context):
    for page in context.pages:
        try:
            if not page.is_closed() and _is_douyin_page_url(page.url):
                return page
        except Exception:
            continue
    return None


def _prepare_api_page(context, timeout_ms: int):
    """
    Return (page, created).
    Existing douyin pages are reused so tool calls do not open an extra tab.
    """
    page = _find_existing_douyin_page(context)
    if page is not None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        return page, False

    page = context.new_page()
    page.goto(DOUYIN_HOME_URL, timeout=timeout_ms, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        # The home feed can keep network activity alive; the security SDK is usually ready
        # after domcontentloaded plus a short wait.
        pass
    page.wait_for_timeout(1500)
    return page, True


def _collect_body(aweme_id: str, action: int) -> str:
    """Build Douyin collect endpoint body. action=0 means uncollect."""
    return urlencode({
        "action": str(action),
        "aweme_id": aweme_id,
        "aweme_type": "0",
    })


def _digg_body(aweme_id: str, digg_type: int) -> str:
    """Build Douyin digg endpoint body. type=0 means unlike."""
    return urlencode({
        "aweme_id": aweme_id,
        "item_type": "0",
        "type": str(digg_type),
    })


def _parse_collect_response(payload: object) -> tuple[bool, str, bool]:
    """
    Parse /aweme/collect response.
    Returns (success, message, already_uncollected).
    """
    if not isinstance(payload, dict):
        return False, f"API 返回不是 JSON 对象：{payload!r}", False

    status = payload.get("status_code")
    if status != 0:
        msg = payload.get("status_msg") or payload.get("message") or str(payload)
        return False, f"API 返回失败 status_code={status}: {msg}", False

    collects_flag = payload.get("collects_flag")
    if collects_flag is False:
        return True, "取消收藏成功（API）", False
    if collects_flag is True:
        return False, "API 返回成功但 collects_flag 仍为 true", False

    return False, f"API 返回缺少 collects_flag，无法确认结果：{payload}", False


def _parse_digg_response(payload: object) -> tuple[bool, str]:
    """Parse /commit/item/digg response for unlike."""
    if not isinstance(payload, dict):
        return False, f"API 返回不是 JSON 对象：{payload!r}"

    status = payload.get("status_code")
    if status != 0:
        msg = payload.get("status_msg") or payload.get("message") or str(payload)
        return False, f"API 返回失败 status_code={status}: {msg}"

    digg_state = payload.get("is_digg", payload.get("digg_status"))
    if digg_state in {1, True}:
        return False, f"API 返回成功但仍显示已喜欢：{payload}"

    return True, "取消喜欢成功（API）"


def _uncollect_by_api(page, aweme_id: str, timeout_ms: int) -> UncollectResult:
    """
    Use Douyin's own web collect endpoint inside an authenticated browser page.
    The in-page security SDK signs the request by adding msToken/a_bogus/fp.
    """
    logger.info("Try API uncollect via /aweme/v1/web/aweme/collect/")
    body = _collect_body(aweme_id, action=0)
    raw = page.evaluate(UNCOLLECT_BY_API_JS, {"body": body})
    if not isinstance(raw, dict):
        return UncollectResult(False, f"API 调用返回异常结构：{raw!r}")
    if not raw.get("ok"):
        return UncollectResult(
            False,
            f"API HTTP 失败 status={raw.get('http_status')}: {(raw.get('text') or '')[:300]}",
        )

    success, message, already = _parse_collect_response(raw.get("payload"))
    return UncollectResult(
        success=success,
        message=message if success else f"{message}; raw={(raw.get('text') or '')[:300]}",
        already_uncollected=already,
    )


def _unlike_by_api(page, aweme_id: str, timeout_ms: int) -> UncollectResult:
    """
    Use Douyin's web digg endpoint inside an authenticated browser page.
    The in-page security SDK signs the request by adding msToken/a_bogus/fp.
    """
    logger.info("Try API unlike via /aweme/v1/web/commit/item/digg/")
    body = _digg_body(aweme_id, digg_type=0)
    raw = page.evaluate(DIGG_BY_API_JS, {"body": body})
    if not isinstance(raw, dict):
        return UncollectResult(False, f"API 调用返回异常结构：{raw!r}")
    if not raw.get("ok"):
        return UncollectResult(
            False,
            f"API HTTP 失败 status={raw.get('http_status')}: {(raw.get('text') or '')[:300]}",
        )

    success, message = _parse_digg_response(raw.get("payload"))
    return UncollectResult(
        success=success,
        message=message if success else f"{message}; raw={(raw.get('text') or '')[:300]}",
    )


def _uncollect_by_page_click(page, aweme_id: str, timeout_ms: int) -> UncollectResult:
    """Last-resort fallback that opens the video page and clicks the collect button."""
    url = VIDEO_PAGE_URL_TEMPLATE.format(aweme_id=aweme_id)
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        button_handle = page.evaluate_handle(FIND_COLLECT_BUTTON_JS)
        is_null = page.evaluate("h => h === null", button_handle)
        if is_null:
            page.wait_for_timeout(2000)
            button_handle = page.evaluate_handle(FIND_COLLECT_BUTTON_JS)
            is_null = page.evaluate("h => h === null", button_handle)

        if is_null:
            stats = page.evaluate('''
                () => ({
                    url: window.location.href,
                    title: document.title,
                    svg_path_count: document.querySelectorAll('svg path').length,
                    tabindex0_count: document.querySelectorAll('[tabindex="0"]').length,
                    has_m4379: Array.from(document.querySelectorAll('svg path')).some(p => (p.getAttribute('d') || '').includes('M43.79')),
                    body_text_preview: document.body.innerText.slice(0, 200),
                })
            ''')
            return UncollectResult(
                success=False,
                message=(
                    f"找不到收藏按钮。最终 URL={stats.get('url')}，"
                    f"title={stats.get('title')}，has_m4379={stats.get('has_m4379')}，"
                    f"text={stats.get('body_text_preview', '')[:100]!r}"
                ),
            )

        state = page.evaluate(GET_COLLECTED_STATE_JS, button_handle)
        logger.info("Fallback page-click detected collected state: {}", state)
        if state is False:
            return UncollectResult(True, "已经是未收藏状态（页面兜底）", already_uncollected=True)

        try:
            page.bring_to_front()
        except Exception:
            pass
        page.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})", button_handle)
        page.wait_for_timeout(500)

        for label, action in (
            ("focus+Enter", lambda: (button_handle.focus(), page.keyboard.press("Enter"))),
            ("focus+Space", lambda: (button_handle.focus(), page.keyboard.press("Space"))),
        ):
            logger.info("Fallback click try: {}", label)
            try:
                action()
                page.wait_for_timeout(2400)
            except Exception as e:
                logger.warning("{} failed: {}", label, e)
            if page.evaluate(GET_COLLECTED_STATE_JS, button_handle) is False:
                return UncollectResult(True, f"取消收藏成功（{label}）")

        box = page.evaluate(
            "el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }",
            button_handle,
        )
        click_x = box["x"] + box["w"] / 2
        click_y = box["y"] + box["h"] / 2
        logger.info("Fallback click try: mouse at ({:.0f}, {:.0f})", click_x, click_y)
        page.mouse.click(click_x, click_y)
        page.wait_for_timeout(2500)
        new_state = page.evaluate(GET_COLLECTED_STATE_JS, button_handle)
        if new_state is False:
            return UncollectResult(True, "取消收藏成功（mouse click）")

        return UncollectResult(
            False,
            f"API 失败后页面兜底也没切换状态，new_state={new_state}。",
        )
    except Exception as e:
        logger.exception("Page-click fallback failed for {}: {}", aweme_id, e)
        return UncollectResult(success=False, message=f"异常：{e}")


def uncollect_many(
    aweme_ids: Sequence[str],
    cdp_endpoint: Optional[str] = None,
    dry_run: bool = False,
    timeout_ms: int = 25000,
    allow_page_fallback: bool = False,
    headless: bool = True,
    hide_window: bool = True,
    browser_channel: Optional[str] = None,
    profile_path: Optional[Path] = None,
) -> list[UncollectResult]:
    ids = [str(x).strip() for x in aweme_ids if str(x).strip()]
    if not ids:
        return []

    logger.info(
        "Uncollect batch: count={}, dry_run={}, page_fallback={}, bridge={}",
        len(ids), dry_run, allow_page_fallback, "cdp" if cdp_endpoint else "profile",
    )

    try:
        with _api_context(cdp_endpoint, headless, hide_window, browser_channel, profile_path) as (context, context_kind):
            page, created = _prepare_api_page(context, timeout_ms)
            bridge_kind = "new" if created else "existing"
            try:
                if dry_run:
                    webdriver = page.evaluate("navigator.webdriver")
                    message = (
                        f"[DRY RUN] API 模式就绪，context={context_kind}，bridge_page={bridge_kind}，"
                        f"navigator.webdriver={webdriver!r}，未发送取消收藏请求。"
                    )
                    return [UncollectResult(True, message) for _ in ids]

                results: list[UncollectResult] = []
                for aweme_id in ids:
                    api_result = _uncollect_by_api(page, aweme_id, timeout_ms=timeout_ms)
                    if api_result.success or not allow_page_fallback:
                        results.append(api_result)
                        continue

                    logger.warning(
                        "API uncollect failed for {}: {}. Falling back to page click.",
                        aweme_id,
                        api_result.message,
                    )
                    results.append(_uncollect_by_page_click(page, aweme_id, timeout_ms))
                return results
            finally:
                if created:
                    try:
                        page.close()
                    except Exception:
                        pass
    except Exception as e:
        logger.exception("Uncollect batch failed: {}", e)
        return [UncollectResult(False, f"异常：{e}") for _ in ids]


def uncollect_one(
    aweme_id: str,
    cdp_endpoint: Optional[str] = None,
    dry_run: bool = False,
    timeout_ms: int = 25000,
    allow_page_fallback: bool = False,
    headless: bool = True,
    hide_window: bool = True,
    browser_channel: Optional[str] = None,
    profile_path: Optional[Path] = None,
) -> UncollectResult:
    logger.info("Uncollect attempt: aweme_id={}, dry_run={}", aweme_id, dry_run)
    results = uncollect_many(
        [aweme_id],
        cdp_endpoint=cdp_endpoint,
        dry_run=dry_run,
        timeout_ms=timeout_ms,
        allow_page_fallback=allow_page_fallback,
        headless=headless,
        hide_window=hide_window,
        browser_channel=browser_channel,
        profile_path=profile_path,
    )
    if not results:
        return UncollectResult(False, "没有传入 aweme_id")
    return results[0]


def unlike_many(
    aweme_ids: Sequence[str],
    cdp_endpoint: Optional[str] = None,
    dry_run: bool = False,
    timeout_ms: int = 25000,
    headless: bool = True,
    hide_window: bool = True,
    browser_channel: Optional[str] = None,
    profile_path: Optional[Path] = None,
) -> list[UncollectResult]:
    ids = [str(x).strip() for x in aweme_ids if str(x).strip()]
    if not ids:
        return []

    logger.info(
        "Unlike batch: count={}, dry_run={}, bridge={}",
        len(ids), dry_run, "cdp" if cdp_endpoint else "profile",
    )

    try:
        with _api_context(cdp_endpoint, headless, hide_window, browser_channel, profile_path) as (context, context_kind):
            page, created = _prepare_api_page(context, timeout_ms)
            bridge_kind = "new" if created else "existing"
            try:
                if dry_run:
                    webdriver = page.evaluate("navigator.webdriver")
                    message = (
                        f"[DRY RUN] API 模式就绪，context={context_kind}，bridge_page={bridge_kind}，"
                        f"navigator.webdriver={webdriver!r}，未发送取消喜欢请求。"
                    )
                    return [UncollectResult(True, message) for _ in ids]

                return [
                    _unlike_by_api(page, aweme_id, timeout_ms=timeout_ms)
                    for aweme_id in ids
                ]
            finally:
                if created:
                    try:
                        page.close()
                    except Exception:
                        pass
    except Exception as e:
        logger.exception("Unlike batch failed: {}", e)
        return [UncollectResult(False, f"异常：{e}") for _ in ids]


def unlike_one(
    aweme_id: str,
    cdp_endpoint: Optional[str] = None,
    dry_run: bool = False,
    timeout_ms: int = 25000,
    headless: bool = True,
    hide_window: bool = True,
    browser_channel: Optional[str] = None,
    profile_path: Optional[Path] = None,
) -> UncollectResult:
    logger.info("Unlike attempt: aweme_id={}, dry_run={}", aweme_id, dry_run)
    results = unlike_many(
        [aweme_id],
        cdp_endpoint=cdp_endpoint,
        dry_run=dry_run,
        timeout_ms=timeout_ms,
        headless=headless,
        hide_window=hide_window,
        browser_channel=browser_channel,
        profile_path=profile_path,
    )
    if not results:
        return UncollectResult(False, "没有传入 aweme_id")
    return results[0]
