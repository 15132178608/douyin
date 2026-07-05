"""
Playwright 抓取抖音收藏列表。

策略：
- 用 chromium + 持久化 user-data-dir（PLAYWRIGHT_PROFILE_PATH），首次扫码登录后 cookie 留存。
- 默认在抖音 origin 下直接调用 listcollection 接口分页，不展示收藏页。
- 旧滚动监听模式仍保留为兜底：打开收藏页 URL，监听 listcollection 接口的 XHR Response。
- API 模式按 cursor 翻页；旧模式模拟滚动直到 has_more=0 或连续 N 次没拿到新数据。
- 节流：每次滚动间隔 1.5–3 秒（settings.crawl_sleep_min/max）。
- 失败收敛：连续 3 次抓不到响应 → 退出，标记 partial / failed。

不做的事：
- 不并发，不开多窗口（避免风控）。
- 不下载视频本身。
- 不解析 DOM（抖音 DOM 频繁改，JSON 接口稳定得多）。
"""
from __future__ import annotations

import base64
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode, urlparse

from loguru import logger
from playwright.sync_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from src.config import settings
from src.crawler.parser import extract_response
from src.models import Favorite


# 抖音 PC 端收藏夹页面（默认 Tab）
COLLECTION_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"
LIKES_URL = "https://www.douyin.com/user/self?showTab=like"
DOUYIN_HOME_URL = "https://www.douyin.com/"

# 接口路径片段（用于匹配 XHR）。抖音偶尔会改前缀，但 listcollection 这段相对稳定。
COLLECTION_API_KEYWORD = "/aweme/v1/web/aweme/listcollection/"
COLLECTION_API_PATH = "/aweme/v1/web/aweme/listcollection/"
LIKES_API_KEYWORD = "/aweme/v1/web/aweme/favorite/"
LIKES_API_PATH = "/aweme/v1/web/aweme/favorite/"
USER_PROFILE_SELF_API_PATH = "/aweme/v1/web/user/profile/self/"
COLLECTION_PAGE_SIZE = 20
MAX_API_PAGES = 500
AUTH_QR_API_KEYWORD = "/passport/web/get_qrcode/"
AUTH_QR_REGENERATE_FALLBACK_INTERVAL_S = 45
AUTH_QR_REGENERATE_SAFETY_MARGIN_S = 10
DEFAULT_AUTH_SCREENSHOT_PATH = settings.playwright_profile_path.parent / "auth" / "douyin-login.png"

# 调试时用：URL 含这些关键词的 XHR 也会被打印（但不会被当作 collection 数据处理）
DEBUG_URL_KEYWORDS = ("aweme", "collection", "favorite", "like")


# 注入 stealth JS：覆盖暴露自动化身份的 navigator 属性。
# 抖音用这些属性识别 Playwright/Selenium，匹配后会卡住页面渲染。
STEALTH_INIT_SCRIPT = r"""
// 1. navigator.webdriver = undefined（而不是 true）
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. 伪造 window.chrome
if (!window.chrome) {
    window.chrome = { runtime: {} };
}

// 3. plugins 长度大于 0（Playwright 默认是 0，真实 Chrome 有几个）
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Native Client' },
    ],
});

// 4. languages 必须有
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
});

// 5. permissions 查询不要露馅
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}

// 6. WebGL vendor / renderer 伪造（部分检测会用 WebGL 指纹）
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};
"""


FETCH_COLLECTION_BY_API_JS = """
async ({query, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 30000);
    try {
        const resp = await fetch('/aweme/v1/web/aweme/listcollection/?' + query, {
            method: 'POST',
            credentials: 'include',
            signal: controller.signal,
            headers: {
                'accept': 'application/json, text/plain, */*',
                'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            },
            body: '',
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
            final_url: resp.url,
        };
    } finally {
        clearTimeout(timer);
    }
}
"""


FETCH_LIKES_BY_API_JS = """
async ({query, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 30000);
    try {
        const resp = await fetch('/aweme/v1/web/aweme/favorite/?' + query, {
            method: 'GET',
            credentials: 'include',
            signal: controller.signal,
            headers: {
                'accept': 'application/json, text/plain, */*',
            },
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
            final_url: resp.url,
        };
    } finally {
        clearTimeout(timer);
    }
}
"""


FETCH_SELF_PROFILE_JS = """
async ({query, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 30000);
    try {
        const resp = await fetch('/aweme/v1/web/user/profile/self/?' + query, {
            method: 'GET',
            credentials: 'include',
            signal: controller.signal,
            headers: {
                'accept': 'application/json, text/plain, */*',
            },
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
            final_url: resp.url,
        };
    } finally {
        clearTimeout(timer);
    }
}
"""


SHOW_LOGIN_PANEL_JS = """
async () => {
    if (typeof window.showAccount !== 'function') {
        return {ok: false, reason: 'window.showAccount is not ready'};
    }
    const promise = window.showAccount({
        config: {
            next: 'https://www.douyin.com/',
            loginType: ['LOGIN_SCAN_CODE'],
            loginOnly: true,
            accountApiConfig: {
                aid: 6383,
                host: 'https://login.douyin.com',
                hcSwitch: true,
                language: 'zh',
            },
            isShowTips: false,
        },
        enterMethod: 'recall_auth',
    });
    if (promise && typeof promise.catch === 'function') {
        promise.catch(() => {});
    }
    return {ok: true};
}
"""


REGENERATE_LOGIN_QR_JS = """
async () => {
    const closeCandidates = Array.from(document.querySelectorAll('button, div, span, svg')).filter((el) => {
        const text = (el.innerText || el.textContent || '').trim();
        const aria = (el.getAttribute('aria-label') || '').trim();
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        const looksClose =
            text === '×' ||
            text === 'x' ||
            text === 'X' ||
            aria.includes('关闭') ||
            aria.toLowerCase().includes('close');
        return looksClose &&
            rect.width >= 10 &&
            rect.height >= 10 &&
            rect.width <= 80 &&
            rect.height <= 80 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden';
    });
    const closeTarget = closeCandidates[closeCandidates.length - 1];
    if (closeTarget) {
        closeTarget.click();
        await new Promise((resolve) => setTimeout(resolve, 500));
    }
    if (typeof window.showAccount !== 'function') {
        return {ok: false, reason: 'window.showAccount is not ready after close'};
    }
    const promise = window.showAccount({
        config: {
            next: 'https://www.douyin.com/',
            loginType: ['LOGIN_SCAN_CODE'],
            loginOnly: true,
            accountApiConfig: {
                aid: 6383,
                host: 'https://login.douyin.com',
                hcSwitch: true,
                language: 'zh',
            },
            isShowTips: false,
        },
        enterMethod: 'recall_auth_refresh',
    });
    if (promise && typeof promise.catch === 'function') {
        promise.catch(() => {});
    }
    return {ok: true, closed: Boolean(closeTarget)};
}
"""


CLICK_LOGIN_BUTTON_JS = """
() => {
    const labels = ['登录', '扫码登录'];
    const nodes = Array.from(document.querySelectorAll('button, div, span, a'));
    const candidates = nodes.filter((el) => {
        const text = (el.innerText || el.textContent || '').trim();
        if (!labels.some((label) => text === label || text.includes(label))) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 20 && rect.height > 20;
    });
    const target = candidates[candidates.length - 1] || candidates[0];
    if (!target) return {ok: false, reason: 'login button not found'};
    target.click();
    return {ok: true, tag: target.tagName, text: (target.innerText || target.textContent || '').trim()};
}
"""


CONFIRM_SCANNED_LOGIN_JS = """
() => {
    const labels = ['一键登录', '确认登录', '授权登录', '同意并登录'];
    const nodes = Array.from(document.querySelectorAll('button, div, span, a'));
    const candidates = nodes.filter((el) => {
        const text = (el.innerText || el.textContent || '').trim();
        if (!labels.some((label) => text === label || text.includes(label))) {
            return false;
        }
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 40 &&
            rect.height > 24 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            Number(style.opacity || '1') > 0;
    });
    const target = candidates[candidates.length - 1] || candidates[0];
    if (!target) return {ok: false, reason: 'confirm login button not found'};
    target.click();
    return {ok: true, tag: target.tagName, text: (target.innerText || target.textContent || '').trim()};
}
"""


def _collection_query(cursor: int = 0, count: int = COLLECTION_PAGE_SIZE) -> str:
    """Build query string for Douyin's web collection API."""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "version_code": "170400",
        "version_name": "17.4.0",
        "update_version_code": "170400",
        "support_h265": "1",
        "support_dash": "1",
        "cookie_enabled": "true",
        "platform": "PC",
        "cursor": str(cursor),
        "count": str(count),
        "publish_video_strategy_type": "2",
    }
    return urlencode(params)


def _likes_query(sec_user_id: str, cursor: int = 0, count: int = COLLECTION_PAGE_SIZE) -> str:
    """Build query string for Douyin's web liked-videos API."""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "version_code": "170400",
        "version_name": "17.4.0",
        "update_version_code": "170400",
        "support_h265": "1",
        "support_dash": "1",
        "cookie_enabled": "true",
        "platform": "PC",
        "sec_user_id": sec_user_id,
        "max_cursor": str(cursor),
        "count": str(count),
        "publish_video_strategy_type": "2",
    }
    return urlencode(params)


def _self_profile_query() -> str:
    """Build query string for the current logged-in Douyin profile."""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "version_code": "170400",
        "version_name": "17.4.0",
        "update_version_code": "170400",
        "cookie_enabled": "true",
        "platform": "PC",
    }
    return urlencode(params)


def _extract_self_sec_user_id(payload: object) -> str | None:
    """Extract the logged-in user's sec_uid/sec_user_id from common profile payload shapes."""
    if not isinstance(payload, dict):
        return None

    for key in ("sec_uid", "sec_user_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("user", "user_info", "data"):
        value = _extract_self_sec_user_id(payload.get(key))
        if value:
            return value

    return None


def _first_url_from_avatar(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    urls = value.get("url_list")
    if not isinstance(urls, list):
        return None
    for url in urls:
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _extract_self_profile(payload: object) -> dict:
    """Extract display fields for the currently logged-in Douyin account."""
    if not isinstance(payload, dict):
        return {}

    candidates: list[object] = [payload]
    for key in ("user", "user_info", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            for nested in ("user", "user_info"):
                nested_value = value.get(nested)
                if isinstance(nested_value, dict):
                    candidates.append(nested_value)

    user = next(
        (
            item for item in candidates
            if isinstance(item, dict)
            and any(item.get(k) for k in ("nickname", "unique_id", "sec_uid", "sec_user_id"))
        ),
        None,
    )
    if not isinstance(user, dict):
        return {}

    avatar_url = (
        _first_url_from_avatar(user.get("avatar_thumb"))
        or _first_url_from_avatar(user.get("avatar_medium"))
        or _first_url_from_avatar(user.get("avatar_larger"))
    )
    return {
        "nickname": str(user.get("nickname") or "").strip(),
        "unique_id": str(user.get("unique_id") or user.get("short_id") or "").strip(),
        "sec_uid": str(user.get("sec_uid") or user.get("sec_user_id") or "").strip(),
        "avatar_url": avatar_url or "",
    }


def _is_douyin_page_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "www.douyin.com",
        "www-hj.douyin.com",
    }


def _is_qr_like_box(box: object) -> bool:
    """Return True only for visible QR-sized, square-ish elements."""
    if not isinstance(box, dict):
        return False
    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    if width < 120 or height < 120 or width > 280 or height > 280:
        return False
    ratio = width / height if height else 0
    return 0.80 <= ratio <= 1.25


def _should_refresh_login_screenshot(state: dict) -> bool:
    """Keep the saved file as a QR image; do not overwrite it with post-scan prompts."""
    return bool(state.get("has_qr")) and not bool(state.get("has_one_click_login"))


def _qr_candidate_priority(meta: object, box: object) -> int | None:
    """Lower priority means a more likely QR element."""
    if not _is_qr_like_box(box) or not isinstance(meta, dict):
        return None
    if meta.get("visible") is False:
        return None

    tag = str(meta.get("tag") or "").lower()
    src = str(meta.get("src") or "").lower()
    text = str(meta.get("text") or "").strip()
    class_name = str(meta.get("class") or meta.get("cls") or "").lower()

    if tag == "img" and src.startswith("data:image"):
        return 0
    if tag == "img" and ("qr" in src or "qrcode" in src):
        return 1
    if tag in {"canvas", "svg"} and not text:
        return 2
    if not text and ("qr" in class_name or "code" in class_name):
        return 3
    if not text:
        width = float(box.get("width") or 0)
        height = float(box.get("height") or 0)
        if 150 <= width <= 230 and 150 <= height <= 230:
            return 4
    return None


def _auth_qr_metadata_path(qr_path: Path) -> Path:
    return qr_path.with_suffix(".json")


def _auth_qr_display_path(qr_path: Path, saved_at: float) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(saved_at))
    millis = int((saved_at % 1) * 1000)
    return qr_path.with_name(f"{qr_path.stem}-{stamp}-{millis:03d}{qr_path.suffix}")


def _extract_auth_qr_payload(payload: object) -> tuple[str, int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("QR response has no data object")
    data = payload["data"]
    qrcode = data.get("qrcode")
    if not isinstance(qrcode, str) or not qrcode.strip():
        raise ValueError("QR response has no qrcode image")
    if "," in qrcode and qrcode.strip().lower().startswith("data:image"):
        qrcode = qrcode.split(",", 1)[1]
    expire_time = data.get("expire_time")
    if expire_time is not None:
        try:
            expire_time = int(expire_time)
        except (TypeError, ValueError):
            expire_time = None
    return qrcode.strip(), expire_time


def _auth_qr_ttl_seconds(expire_time: int | None, now: float | None = None) -> int | None:
    if expire_time is None:
        return None
    return max(0, int(expire_time - (now if now is not None else time.time())))


def _next_auth_qr_refresh_time(expire_time: int | None, now: float | None = None) -> float:
    current = now if now is not None else time.time()
    if expire_time is None:
        return current + AUTH_QR_REGENERATE_FALLBACK_INTERVAL_S
    refresh_at = float(expire_time - AUTH_QR_REGENERATE_SAFETY_MARGIN_S)
    return max(current + 5, refresh_at)


def _is_login_required_payload(payload: object) -> bool:
    """Best-effort detection for Douyin API responses that mean auth is missing."""
    if not isinstance(payload, dict):
        return False

    status = payload.get("status_code")
    if status == 0:
        return False

    message = " ".join(
        str(payload.get(key) or "")
        for key in ("status_msg", "message", "prompts", "toast")
    ).lower()
    login_markers = ("login", "登录", "请先登录", "not login", "未登录")
    if any(marker in message for marker in login_markers):
        return True

    if status == 5 and str(payload.get("uid") or "0") == "0":
        return True

    return status in {8, 10008, 10009, 2190009}


@dataclass
class CrawlPage:
    """一次接口响应的解析结果。"""
    favorites: list[Favorite]
    has_more: bool
    cursor: Optional[int]
    raw_payload: dict = field(default_factory=dict)


@dataclass
class AuthResult:
    """后台扫码授权结果。"""
    success: bool
    message: str
    screenshot_path: Optional[Path] = None


@dataclass
class AuthQrCapture:
    """Last QR image captured from Douyin's passport get_qrcode response."""
    path: Path
    saved_at: float
    expire_time: int | None = None
    ttl_seconds: int | None = None
    source: str = "passport_api"
    display_path: Path | None = None


class DouyinCrawler:
    """
    用法：
        with DouyinCrawler() as c:
            for page in c.crawl_collection():
                ...
    """

    def __init__(
        self,
        headless: bool = True,
        scroll_pause_min: float | None = None,
        scroll_pause_max: float | None = None,
        max_idle_scrolls: int = 10,
        cdp_endpoint: str | None = None,
        debug_xhr: bool = False,
        api_mode: bool = True,
        page_size: int = COLLECTION_PAGE_SIZE,
        max_api_pages: int = MAX_API_PAGES,
        hide_window: bool = False,
        browser_channel: str | None = None,
        profile_path: Path | None = None,
    ):
        # 默认后台跑：浏览器仍然启动，但不展示窗口；需要排查时显式 visible/debug。
        self.headless = headless
        self.scroll_pause_min = scroll_pause_min or settings.crawl_sleep_min
        self.scroll_pause_max = scroll_pause_max or settings.crawl_sleep_max
        # 连续多少次滚动都没拿到新响应 → 认为到底了
        self.max_idle_scrolls = max_idle_scrolls
        # CDP 模式：连接已经打开的真实 Chrome（用 --remote-debugging-port=9222 启动）
        # 例：cdp_endpoint="http://localhost:9222"
        self.cdp_endpoint = cdp_endpoint
        # 调试模式：把所有 aweme/collection/favorite 相关 XHR URL 都打印出来
        self.debug_xhr = debug_xhr
        # API 模式：不打开收藏页给用户看、不滚动页面，直接在抖音 origin 下 fetch 翻页。
        self.api_mode = api_mode
        self.page_size = page_size
        self.max_api_pages = max_api_pages
        # 授权时可用 headed Chromium 避免 headless 风控，但把窗口移到屏幕外。
        self.hide_window = hide_window
        self.browser_channel = browser_channel
        self.profile_path = profile_path or settings.playwright_profile_path

        self._pw = None
        self._context: BrowserContext | None = None
        self._browser = None  # CDP 模式下用
        self._page: Page | None = None
        # 按响应顺序攒数据，由 response handler 写入
        self._page_queue: list[CrawlPage] = []
        # 已经处理过的 URL（避免同一个响应被多次解析）
        self._seen_urls: set[str] = set()
        self._auth_qr_capture: AuthQrCapture | None = None
        self._auth_qr_capture_callback: Callable[[AuthQrCapture], None] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def __enter__(self) -> "DouyinCrawler":
        self._pw = sync_playwright().start()

        if self.cdp_endpoint:
            # ====== CDP 模式：连接到用户已打开的真实 Chrome ======
            logger.info("Connecting via CDP to {}", self.cdp_endpoint)
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
            # 用第一个 context（共享用户的 cookie / 登录态）
            if self._browser.contexts:
                self._context = self._browser.contexts[0]
            else:
                self._context = self._browser.new_context()

            # ✨ 关键 bug 修复 ✨
            # CDP 模式下 Playwright 接管的 tab，navigator.webdriver = true，
            # 抖音检测到后会"假装"页面正常工作但 disable lazy-load XHR。
            # 必须在 new_page() 之前注入 stealth init 脚本。
            try:
                self._context.add_init_script(STEALTH_INIT_SCRIPT)
                logger.info("Stealth init script registered for CDP context.")
            except Exception as e:
                logger.warning("Stealth script registration failed: {}", e)

            existing_count = len(self._context.pages)
            logger.info("CDP connected. Existing tabs in browser: {}", existing_count)
            self._context.on("page", self._attach_listeners)
            self._page = self._find_existing_douyin_page() if self.api_mode else None
            if self._page is None:
                self._page = self._context.new_page()
                logger.info("Opened a fresh tab for {}.", "API bridge" if self.api_mode else "monitoring")
            else:
                logger.info("Reusing existing Douyin tab for API bridge.")
            if not self.api_mode:
                self._attach_listeners(self._page)
            if not self.api_mode:
                try:
                    self._page.bring_to_front()
                except Exception:
                    pass
        else:
            # ====== 默认模式：Playwright 自己启动 Chromium + stealth ======
            self.profile_path.mkdir(parents=True, exist_ok=True)
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            if self.hide_window:
                launch_args.extend([
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                    "--start-minimized",
                ])
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_path),
                headless=self.headless,
                channel=self.browser_channel,
                viewport={"width": 1280, "height": 900},
                # 反检测 args
                args=launch_args,
                ignore_default_args=["--enable-automation"],
                # 用真实的 user agent（不让 Playwright 自动给 HeadlessChrome）
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            # 注入 stealth JS（在每个页面、每次导航前都跑一次）
            self._context.add_init_script(STEALTH_INIT_SCRIPT)
            self._page = self._context.new_page()
            if not self.api_mode:
                self._attach_listeners(self._page)
            logger.info("Browser ready (stealth). Profile dir: {}", self.profile_path)

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.cdp_endpoint:
                # CDP 模式不要关浏览器（那是用户自己的 Chrome）
                pass
            elif self._context:
                self._context.close()
        finally:
            if self._pw:
                self._pw.stop()
        logger.info("Browser closed." if not self.cdp_endpoint else "CDP disconnected (your Chrome stays open).")

    # ------------------------------------------------------------------
    # 响应监听
    # ------------------------------------------------------------------
    def _find_existing_douyin_page(self) -> Page | None:
        if self._context is None:
            return None
        for page in self._context.pages:
            try:
                if not page.is_closed() and _is_douyin_page_url(page.url):
                    return page
            except Exception:
                continue
        return None

    def _attach_listeners(self, page: Page) -> None:
        """给一个 page 挂上所有需要的监听器。新 tab、CDP 已存在的 tab 都走这里。

        关键修复：Playwright 通过 connect_over_cdp 连到已经存在的 tab 时，
        默认不会启用 Network 域，导致 response/request 事件不触发。
        我们用 CDP session 显式发一个 Network.enable 把它强制打开。
        """
        page.on("response", self._on_response)
        if self.debug_xhr:
            page.on("request", self._on_request_debug)

        # CDP 模式下，给已有 page 显式启用 Network 域监控
        if self.cdp_endpoint and self._context is not None:
            try:
                cdp_session = self._context.new_cdp_session(page)
                cdp_session.send("Network.enable")
                logger.info("CDP Network.enable sent for page: {}", page.url[:100] if page.url else "(blank)")
            except Exception as e:
                logger.warning("Could not enable Network domain via CDP: {}", e)

        logger.info("Listener attached to page: {}", page.url[:100] if page.url else "(blank)")

    def _on_request_debug(self, request) -> None:
        """调试用：任何 aweme/collection/favorite 相关请求一发起就打印。
        这是比 response 更早的事件，能确认监听器有没有在工作。"""
        try:
            url = request.url
            if any(k in url for k in DEBUG_URL_KEYWORDS):
                logger.info("[REQ ] {} {}", request.method, url.split("?")[0])
        except Exception:
            pass

    def _on_response(self, response: Response) -> None:
        try:
            url = response.url

            # 调试模式：打印所有 aweme/collection 相关 URL，方便排查抖音接口变化
            if self.debug_xhr and any(k in url for k in DEBUG_URL_KEYWORDS):
                short = url.split("?")[0]
                logger.info("[XHR] {} {}", response.status, short)

            if COLLECTION_API_KEYWORD not in url:
                return
            if url in self._seen_urls:
                return
            self._seen_urls.add(url)

            if response.status != 200:
                logger.warning("listcollection responded {} for {}", response.status, url)
                return

            try:
                payload = response.json()
            except Exception as e:
                logger.warning("listcollection body not JSON: {}", e)
                return

            favorites, meta = extract_response(payload)
            self._page_queue.append(
                CrawlPage(
                    favorites=favorites,
                    has_more=bool(meta.get("has_more")),
                    cursor=meta.get("cursor"),
                    raw_payload=payload,
                )
            )
            logger.info(
                "Captured page: items={}, has_more={}, cursor={}",
                len(favorites), meta.get("has_more"), meta.get("cursor"),
            )
        except Exception as e:
            # response handler 里的异常会被 Playwright 吞，自己 log 一下
            logger.exception("on_response error: {}", e)

    # ------------------------------------------------------------------
    # 抓取主流程
    # ------------------------------------------------------------------
    def crawl_collection(self) -> list[Favorite]:
        """
        打开收藏页 → 等首屏接口响应 → 滚动到底。
        返回所有去重后的 Favorite（按抖音返回顺序：新收藏在前）。
        """
        assert self._page is not None, "DouyinCrawler must be used as context manager"

        if self.api_mode:
            return self._crawl_collection_via_api()

        if self.cdp_endpoint:
            # CDP 模式：Playwright 已经开了一个新 stealth tab，主动导航到收藏夹页。
            logger.info("CDP 模式：主动导航 Playwright 新 tab 到 {}", COLLECTION_URL)
            try:
                self._page.goto(COLLECTION_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                logger.warning("Navigate failed (继续监听看看): {}", e)

            # stealth 是否生效的诊断：如果还是 True，说明注入失败
            try:
                is_wd = self._page.evaluate("navigator.webdriver")
                logger.info("Stealth 检查：navigator.webdriver = {} (应为 None / undefined)", is_wd)
            except Exception:
                pass

            logger.info("等首屏 listcollection 接口响应（最多 60s）...")
            # 不再要求用户手动滚——下面用自动滚屏触发懒加载
        else:
            logger.info("Navigating to {}", COLLECTION_URL)
            self._page.goto(COLLECTION_URL, wait_until="domcontentloaded", timeout=60_000)

            # 给用户机会扫码登录（如果 cookie 失效了）。
            logger.info("Waiting for first listcollection response (up to 90s)...")
            if not self._wait_for_first_response(timeout_s=90):
                logger.warning(
                    "未在 90 秒内拿到 listcollection 接口响应。"
                    "如果浏览器停在登录页，请扫码登录后会自动继续。再给 180 秒。"
                )
                if not self._wait_for_first_response(timeout_s=180):
                    self._dump_seen_xhr_summary()
                    raise RuntimeError(
                        "始终没拿到 listcollection 接口响应。"
                        "可能：1) 抖音反自动化检测（试试 --cdp 模式）；"
                        "2) 接口路径变了（用 --debug-xhr 看实际 URL）；"
                        "3) 未登录或网络问题。"
                    )

        # 滚动到底
        idle_scrolls = 0
        while True:
            before = len(self._page_queue)
            self._scroll_one_step()

            time.sleep(random.uniform(self.scroll_pause_min, self.scroll_pause_max))
            after = len(self._page_queue)

            if after > before:
                idle_scrolls = 0
                # 检查最新一页是否说 has_more=0
                last = self._page_queue[-1]
                if not last.has_more:
                    logger.info("has_more=0, reached end of collection.")
                    break
            else:
                idle_scrolls += 1
                logger.debug("No new response after scroll ({}/{})", idle_scrolls, self.max_idle_scrolls)
                if idle_scrolls >= self.max_idle_scrolls:
                    logger.info("Idle threshold hit, stopping.")
                    break

        # 汇总：跨页去重
        all_favorites: dict[str, Favorite] = {}
        for page in self._page_queue:
            for fav in page.favorites:
                # 保留第一次出现（接口返回顺序：新在前）
                if fav.id not in all_favorites:
                    all_favorites[fav.id] = fav
        result = list(all_favorites.values())
        logger.info(
            "Crawl finished: {} pages, {} unique favorites.",
            len(self._page_queue), len(result),
        )
        return result

    def crawl_likes(self) -> list[Favorite]:
        """
        抓取“我喜欢”的视频列表。

        喜欢接口需要当前账号的 sec_user_id，所以这里只支持 API 模式。
        """
        assert self._page is not None, "DouyinCrawler must be used as context manager"

        if not self.api_mode:
            raise RuntimeError("喜欢列表目前只支持后台 API 模式；请不要加 --legacy-scroll。")

        return self._crawl_likes_via_api()

    # ------------------------------------------------------------------
    # API 模式
    # ------------------------------------------------------------------
    def _ensure_api_page_ready(self, url: str = COLLECTION_URL) -> None:
        """Prepare a hidden Douyin-origin page so in-page fetch can use cookies/signing."""
        assert self._page is not None
        try:
            if not _is_douyin_page_url(self._page.url):
                logger.info("Preparing hidden Douyin API bridge: {}", url)
                self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            self._page.wait_for_timeout(1200)
        except Exception as e:
            raise RuntimeError(f"后台抖音 API 环境初始化失败：{e}") from e

    def _fetch_collection_api_page(self, cursor: int, timeout_ms: int = 30_000) -> CrawlPage:
        assert self._page is not None
        query = _collection_query(cursor=cursor, count=self.page_size)
        raw = self._page.evaluate(
            FETCH_COLLECTION_BY_API_JS,
            {"query": query, "timeoutMs": timeout_ms},
        )
        if not isinstance(raw, dict):
            raise RuntimeError(f"收藏列表 API 返回异常结构：{raw!r}")
        if not raw.get("ok"):
            text = (raw.get("text") or "")[:300]
            raise RuntimeError(f"收藏列表 API HTTP 失败 status={raw.get('http_status')}: {text}")

        payload = raw.get("payload")
        if not isinstance(payload, dict):
            text = (raw.get("text") or "")[:300]
            raise RuntimeError(f"收藏列表 API 返回不是 JSON：{text}")
        if _is_login_required_payload(payload):
            msg = payload.get("status_msg") or payload.get("message") or str(payload)
            raise RuntimeError(f"抖音登录态失效，请先运行 `recall auth` 扫码授权。API 返回：{msg}")

        favorites, meta = extract_response(payload)
        page = CrawlPage(
            favorites=favorites,
            has_more=bool(meta.get("has_more")),
            cursor=meta.get("cursor"),
            raw_payload=payload,
        )
        logger.info(
            "Fetched API page: items={}, has_more={}, cursor={}",
            len(page.favorites), page.has_more, page.cursor,
        )
        return page

    def _fetch_self_profile_payload(self, timeout_ms: int = 30_000) -> dict:
        assert self._page is not None
        query = _self_profile_query()
        raw = self._page.evaluate(
            FETCH_SELF_PROFILE_JS,
            {"query": query, "timeoutMs": timeout_ms},
        )
        if not isinstance(raw, dict):
            raise RuntimeError(f"抖音个人资料 API 返回异常结构：{raw!r}")
        if not raw.get("ok"):
            text = (raw.get("text") or "")[:300]
            raise RuntimeError(f"抖音个人资料 API HTTP 失败 status={raw.get('http_status')}: {text}")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            text = (raw.get("text") or "")[:300]
            raise RuntimeError(f"抖音个人资料 API 返回不是 JSON：{text}")
        if _is_login_required_payload(payload):
            msg = payload.get("status_msg") or payload.get("message") or str(payload)
            raise RuntimeError(f"抖音登录态失效，请先运行 `recall auth` 扫码授权。API 返回：{msg}")
        return payload

    def _get_self_sec_user_id(self) -> str:
        payload = self._fetch_self_profile_payload()
        sec_user_id = _extract_self_sec_user_id(payload)
        if not sec_user_id:
            raise RuntimeError("未能从抖音个人资料接口拿到 sec_user_id，暂时无法抓取喜欢列表。")
        return sec_user_id

    def get_self_profile(self) -> dict:
        """Fetch display-only profile fields for the logged-in Douyin account."""
        self._ensure_api_page_ready()
        return _extract_self_profile(self._fetch_self_profile_payload())

    def _fetch_likes_api_page(
        self,
        sec_user_id: str,
        cursor: int,
        timeout_ms: int = 30_000,
    ) -> CrawlPage:
        assert self._page is not None
        query = _likes_query(sec_user_id=sec_user_id, cursor=cursor, count=self.page_size)
        raw = self._page.evaluate(
            FETCH_LIKES_BY_API_JS,
            {"query": query, "timeoutMs": timeout_ms},
        )
        if not isinstance(raw, dict):
            raise RuntimeError(f"喜欢列表 API 返回异常结构：{raw!r}")
        if not raw.get("ok"):
            text = (raw.get("text") or "")[:300]
            raise RuntimeError(f"喜欢列表 API HTTP 失败 status={raw.get('http_status')}: {text}")

        payload = raw.get("payload")
        if not isinstance(payload, dict):
            text = (raw.get("text") or "")[:300]
            raise RuntimeError(f"喜欢列表 API 返回不是 JSON：{text}")
        if _is_login_required_payload(payload):
            msg = payload.get("status_msg") or payload.get("message") or str(payload)
            raise RuntimeError(f"抖音登录态失效，请先运行 `recall auth` 扫码授权。API 返回：{msg}")

        favorites, meta = extract_response(payload)
        page = CrawlPage(
            favorites=favorites,
            has_more=bool(meta.get("has_more")),
            cursor=meta.get("cursor"),
            raw_payload=payload,
        )
        logger.info(
            "Fetched likes API page: items={}, has_more={}, cursor={}",
            len(page.favorites), page.has_more, page.cursor,
        )
        return page

    def _crawl_collection_via_api(self) -> list[Favorite]:
        """Fetch collection pages through Douyin Web API without visible page/scrolling."""
        self._ensure_api_page_ready()

        cursor = 0
        pages: list[CrawlPage] = []
        seen_cursors: set[int] = set()
        for page_no in range(1, self.max_api_pages + 1):
            if cursor in seen_cursors:
                logger.warning("API cursor repeated ({}), stopping to avoid loop.", cursor)
                break
            seen_cursors.add(cursor)

            page = self._fetch_collection_api_page(cursor)
            pages.append(page)
            if not page.has_more:
                logger.info("has_more=0, reached end of collection.")
                break
            if page.cursor is None:
                logger.warning("API response has_more=1 but cursor is missing; stopping.")
                break
            cursor = int(page.cursor)
            time.sleep(random.uniform(self.scroll_pause_min, self.scroll_pause_max))
        else:
            logger.warning("Hit max_api_pages={}, stopping.", self.max_api_pages)

        all_favorites: dict[str, Favorite] = {}
        for page in pages:
            for fav in page.favorites:
                if fav.id not in all_favorites:
                    all_favorites[fav.id] = fav
        result = list(all_favorites.values())
        logger.info("API crawl finished: {} pages, {} unique favorites.", len(pages), len(result))
        return result

    def _crawl_likes_via_api(self) -> list[Favorite]:
        """Fetch liked-video pages through Douyin Web API without visible page/scrolling."""
        self._ensure_api_page_ready(LIKES_URL)
        sec_user_id = self._get_self_sec_user_id()

        cursor = 0
        pages: list[CrawlPage] = []
        seen_cursors: set[int] = set()
        for page_no in range(1, self.max_api_pages + 1):
            if cursor in seen_cursors:
                logger.warning("Likes API cursor repeated ({}), stopping to avoid loop.", cursor)
                break
            seen_cursors.add(cursor)

            page = self._fetch_likes_api_page(sec_user_id, cursor)
            pages.append(page)
            if not page.has_more:
                logger.info("has_more=0, reached end of likes.")
                break
            if page.cursor is None:
                logger.warning("Likes API response has_more=1 but cursor is missing; stopping.")
                break
            cursor = int(page.cursor)
            time.sleep(random.uniform(self.scroll_pause_min, self.scroll_pause_max))
        else:
            logger.warning("Hit max_api_pages={}, stopping.", self.max_api_pages)

        all_likes: dict[str, Favorite] = {}
        for page in pages:
            for fav in page.favorites:
                if fav.id not in all_likes:
                    all_likes[fav.id] = fav
        result = list(all_likes.values())
        logger.info("Likes API crawl finished: {} pages, {} unique likes.", len(pages), len(result))
        return result

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------
    def _attach_auth_qr_capture(self, screenshot_path: Path):
        assert self._page is not None

        def handle_qr_response(response: Response) -> None:
            try:
                if AUTH_QR_API_KEYWORD not in response.url:
                    return
                payload = response.json()
                capture = self._save_auth_qr_payload(payload, screenshot_path)
                logger.info(
                    "Login QR captured from passport API: {}, ttl={}s, expire_time={}",
                    capture.path,
                    capture.ttl_seconds,
                    capture.expire_time,
                )
            except Exception as e:
                logger.warning("Could not capture login QR from API response: {}", e)

        self._page.on("response", handle_qr_response)
        return handle_qr_response

    def _detach_auth_qr_capture(self, handler) -> None:
        if self._page is None or handler is None:
            return
        try:
            self._page.remove_listener("response", handler)
        except Exception:
            pass

    def _save_auth_qr_payload(self, payload: object, screenshot_path: Path) -> AuthQrCapture:
        qrcode, expire_time = _extract_auth_qr_payload(payload)
        image_bytes = base64.b64decode(qrcode, validate=True)
        saved_at = time.time()
        display_path = _auth_qr_display_path(screenshot_path, saved_at)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(image_bytes)
        display_path.write_bytes(image_bytes)

        ttl = _auth_qr_ttl_seconds(expire_time, now=saved_at)
        capture = AuthQrCapture(
            path=screenshot_path,
            saved_at=saved_at,
            expire_time=expire_time,
            ttl_seconds=ttl,
            display_path=display_path,
        )
        self._auth_qr_capture = capture

        metadata = {
            "path": str(screenshot_path),
            "display_path": str(display_path),
            "source": capture.source,
            "saved_at": int(saved_at),
            "expire_time": expire_time,
            "ttl_seconds": ttl,
        }
        _auth_qr_metadata_path(screenshot_path).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if self._auth_qr_capture_callback:
            try:
                self._auth_qr_capture_callback(capture)
            except Exception as e:
                logger.warning("Login QR capture callback failed: {}", e)
        return capture

    def _has_current_auth_qr_capture(self, screenshot_path: Path, since: float = 0) -> bool:
        capture = self._auth_qr_capture
        if capture is None or capture.path != screenshot_path or capture.saved_at < since:
            return False
        ttl = _auth_qr_ttl_seconds(capture.expire_time)
        return ttl is None or ttl > AUTH_QR_REGENERATE_SAFETY_MARGIN_S

    def authorize_by_qr(
        self,
        timeout_s: int = 180,
        panel_timeout_s: int = 60,
        screenshot_path: Path | None = None,
        on_qr_capture: Callable[[AuthQrCapture], None] | None = None,
        on_login_confirmed: Callable[[], None] | None = None,
    ) -> AuthResult:
        """
        Headless QR login flow.

        The browser page is not shown. We save a screenshot containing Douyin's login QR
        and keep polling the API until scanning succeeds and cookies are persisted.
        """
        assert self._page is not None, "DouyinCrawler must be used as context manager"
        auth_started_at = time.perf_counter()
        screenshot_path = screenshot_path or DEFAULT_AUTH_SCREENSHOT_PATH
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        handler = None
        previous_callback = self._auth_qr_capture_callback
        self._auth_qr_capture_callback = on_qr_capture
        login_confirmed_notified = False

        def notify_login_confirmed() -> None:
            nonlocal login_confirmed_notified
            if login_confirmed_notified:
                return
            login_confirmed_notified = True
            if on_login_confirmed is None:
                return
            try:
                on_login_confirmed()
            except Exception as e:
                logger.warning("Login confirmation callback failed: {}", e)

        try:
            if self._has_usable_login_state(timeout_ms=1_000):
                logger.info("QR auth skipped because existing login state is usable.")
                return AuthResult(True, "当前 profile 已有可用登录态，无需扫码。", None)
            logger.info("QR auth login-state check finished in {:.2f}s.", time.perf_counter() - auth_started_at)

            self._auth_qr_capture = None
            handler = self._attach_auth_qr_capture(screenshot_path)
            initial_login_cookie = self._login_cookie_fingerprint()

            try:
                panel_started_at = time.perf_counter()
                self._open_auth_panel(panel_timeout_s=panel_timeout_s)
                logger.info(
                    "QR auth panel opened in {:.2f}s, total {:.2f}s.",
                    time.perf_counter() - panel_started_at,
                    time.perf_counter() - auth_started_at,
                )
            except Exception as e:
                try:
                    self._save_login_screenshot(screenshot_path, allow_page_fallback=True)
                except Exception:
                    pass
                return AuthResult(False, f"打开后台授权页失败：{e}", screenshot_path)

            state = self._auth_visual_state()
            if state.get("has_one_click_login"):
                self._confirm_scanned_login_if_needed()
                notify_login_confirmed()
                time.sleep(0.5)
                if self._has_usable_login_state(timeout_ms=2_000):
                    return AuthResult(True, "扫码后的确认登录已自动完成，登录态已保存。", None)

            if self._has_current_auth_qr_capture(screenshot_path):
                capture = self._auth_qr_capture
                logger.info(
                    "Login QR ready at {}, ttl={}s.",
                    capture.display_path if capture and capture.display_path else screenshot_path,
                    capture.ttl_seconds if capture else None,
                )
            else:
                saved_qr = self._save_login_screenshot(
                    screenshot_path,
                    allow_page_fallback=not bool(state.get("has_one_click_login")),
                )
                logger.info(
                    "Login {} screenshot saved to {}",
                    "QR" if saved_qr else "state",
                    screenshot_path,
                )

            if state.get("has_risk_challenge") and not state.get("has_qr"):
                return AuthResult(
                    False,
                    (
                        "抖音返回了风控/验证码页，未拿到扫码二维码。"
                        "请先关闭 VPN 或切到更稳定的国内网络后重试；"
                        "若仍触发，再用 `recall auth --visible-debug` 人工过一次验证。"
                    ),
                    screenshot_path,
                )

            scan_deadline = time.time() + timeout_s

            next_refresh = _next_auth_qr_refresh_time(
                self._auth_qr_capture.expire_time if self._auth_qr_capture else None
            )
            while time.time() < scan_deadline:
                state = self._auth_visual_state()
                if state.get("has_one_click_login") and self._confirm_scanned_login_if_needed():
                    notify_login_confirmed()
                    time.sleep(0.5)
                    if self._has_usable_login_state(timeout_ms=2_000):
                        return AuthResult(True, "扫码后的确认登录已自动完成，登录态已保存。", screenshot_path)

                current_login_cookie = self._login_cookie_fingerprint()
                if current_login_cookie and current_login_cookie != initial_login_cookie:
                    notify_login_confirmed()
                    if self._is_api_logged_in(timeout_ms=2_000):
                        return AuthResult(True, "扫码授权成功，登录态已保存。", screenshot_path)
                    logger.warning("Login cookie changed but collection API is still unauthenticated; keep waiting.")

                if time.time() >= next_refresh:
                    if _should_refresh_login_screenshot(state):
                        try:
                            capture_started_at = time.time()
                            refresh_timeout = max(5, min(20, int(scan_deadline - time.time())))
                            self._regenerate_login_qr(panel_timeout_s=refresh_timeout)
                            if self._has_current_auth_qr_capture(screenshot_path, since=capture_started_at):
                                capture = self._auth_qr_capture
                                logger.info(
                                    "Login QR regenerated from API: {}, ttl={}s.",
                                    capture.display_path if capture and capture.display_path else screenshot_path,
                                    capture.ttl_seconds if capture else None,
                                )
                            elif self._save_login_screenshot(screenshot_path, allow_page_fallback=False):
                                logger.info("Login QR regenerated and screenshot refreshed: {}", screenshot_path)
                        except Exception as e:
                            logger.warning("Could not regenerate login QR screenshot: {}", e)
                    else:
                        logger.debug("Skip login screenshot refresh for non-QR auth state: {}", state)
                    next_refresh = _next_auth_qr_refresh_time(
                        self._auth_qr_capture.expire_time if self._auth_qr_capture else None
                    )
                time.sleep(0.5)

            return AuthResult(False, f"等待扫码超时（{timeout_s}s）。二维码截图：{screenshot_path}", screenshot_path)
        finally:
            self._detach_auth_qr_capture(handler)
            self._auth_qr_capture_callback = previous_callback

    def _open_auth_panel(self, panel_timeout_s: int = 60) -> None:
        assert self._page is not None
        opened_at = time.perf_counter()
        deadline = time.time() + max(panel_timeout_s, 5)
        nav_timeout_ms = max(5_000, int(max(panel_timeout_s, 5) * 1000))
        try:
            self._page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            logger.info("Auth page domcontentloaded in {:.2f}s.", time.perf_counter() - opened_at)
        except Exception as e:
            logger.warning("Auth page navigation timed out/failed, continuing with current DOM: {}", e)

        last_result = None
        while time.time() < deadline:
            try:
                ready = self._page.evaluate("typeof window.showAccount === 'function'")
                if ready:
                    last_result = self._page.evaluate(SHOW_LOGIN_PANEL_JS)
                    logger.info(
                        "Login panel trigger result after {:.2f}s: {}",
                        time.perf_counter() - opened_at,
                        last_result,
                    )
                    break
            except Exception as e:
                last_result = {"ok": False, "reason": str(e)}
            time.sleep(0.25)

        if not (isinstance(last_result, dict) and last_result.get("ok")):
            logger.warning("Login panel was not triggered: {}", last_result)
            try:
                click_result = self._page.evaluate(CLICK_LOGIN_BUTTON_JS)
                logger.info("Login button fallback result: {}", click_result)
            except Exception as e:
                logger.warning("Login button fallback failed: {}", e)

        visual_started_at = time.perf_counter()
        self._wait_for_auth_visual(timeout_s=max(1, int(deadline - time.time())))
        logger.info(
            "Auth visual wait finished in {:.2f}s, total {:.2f}s.",
            time.perf_counter() - visual_started_at,
            time.perf_counter() - opened_at,
        )

    def _regenerate_login_qr(self, panel_timeout_s: int = 20) -> None:
        assert self._page is not None
        try:
            result = self._page.evaluate(REGENERATE_LOGIN_QR_JS)
            logger.info("Login QR regenerate trigger result: {}", result)
            self._wait_for_auth_visual(timeout_s=max(1, min(panel_timeout_s, 10)))
            if (
                isinstance(result, dict)
                and result.get("ok")
                and result.get("closed")
                and self._auth_visual_state().get("has_qr")
            ):
                return
        except Exception as e:
            logger.warning("In-page QR regenerate failed; will reload auth page: {}", e)

        try:
            self._page.reload(wait_until="domcontentloaded", timeout=max(5_000, panel_timeout_s * 1000))
        except Exception as e:
            logger.warning("Auth page reload for QR regenerate failed, continuing: {}", e)
        self._open_auth_panel(panel_timeout_s=panel_timeout_s)

    def _wait_for_auth_visual(self, timeout_s: int) -> None:
        assert self._page is not None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = self._auth_visual_state()
            if (
                state.get("has_qr")
                or state.get("has_risk_challenge")
                or state.get("has_one_click_login")
            ):
                return
            time.sleep(0.5)

    def _auth_visual_state(self) -> dict:
        assert self._page is not None
        try:
            state = self._page.evaluate(
                """
                () => {
                    const text = document.body ? document.body.innerText : '';
                    const qrCandidates = Array.from(document.querySelectorAll(
                        'canvas, img, svg, [role="img"], [class*="qrcode"], [class*="Qrcode"], [class*="QRCode"], [class*="code"], div'
                    )).filter((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        const ratio = rect.height ? rect.width / rect.height : 0;
                        const tag = el.tagName.toLowerCase();
                        const src = String(el.src || '').toLowerCase();
                        const textInside = (el.innerText || el.textContent || '').trim();
                        const cls = String(el.className || '').toLowerCase();
                        const likelyQr =
                            (tag === 'img' && src.startsWith('data:image')) ||
                            (tag === 'img' && (src.includes('qr') || src.includes('qrcode'))) ||
                            ((tag === 'canvas' || tag === 'svg') && !textInside) ||
                            (!textInside && (cls.includes('qr') || cls.includes('code'))) ||
                            (!textInside && rect.width >= 150 && rect.width <= 230 && rect.height >= 150 && rect.height <= 230);
                        return likelyQr &&
                            rect.width >= 120 &&
                            rect.height >= 120 &&
                            rect.width <= 280 &&
                            rect.height <= 280 &&
                            ratio >= 0.80 &&
                            ratio <= 1.25 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            Number(style.opacity || '1') > 0;
                    });
                    const hasOneClickLogin =
                        text.includes('一键登录') ||
                        text.includes('确认登录') ||
                        text.includes('授权登录') ||
                        text.includes('同意并登录');
                    const lower = text.toLowerCase();
                    const hasRiskChallenge =
                        text.includes('请完成下列验证') ||
                        text.includes('验证码中间页') ||
                        text.includes('安全风险') ||
                        text.includes('图片加载失败') ||
                        lower.includes('captcha');
                    return {
                        text,
                        has_qr: qrCandidates.length > 0,
                        has_one_click_login: hasOneClickLogin,
                        has_risk_challenge: hasRiskChallenge,
                    };
                }
                """
            )
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def _confirm_scanned_login_if_needed(self) -> bool:
        assert self._page is not None
        try:
            result = self._page.evaluate(CONFIRM_SCANNED_LOGIN_JS)
            if isinstance(result, dict) and result.get("ok"):
                logger.info("Login confirmation clicked: {}", result)
                return True
            logger.debug("Login confirmation not clicked: {}", result)
            return False
        except Exception as e:
            logger.debug("Login confirmation click failed: {}", e)
            return False

    def _save_login_screenshot(self, screenshot_path: Path, allow_page_fallback: bool = False) -> bool:
        """Save the visible QR element. Returns True only when a QR-like element was saved."""
        assert self._page is not None
        candidates = [
            "canvas",
            "img",
            "img[src*='qr']",
            "img[src*='qrcode']",
            "svg",
            "[role='img']",
            "[class*='qr']",
            "[class*='code']",
            "[class*='QRCode']",
            "div",
        ]
        best = None
        best_score: tuple[int, float] | None = None
        for selector in candidates:
            try:
                locator = self._page.locator(selector)
                for index in range(min(locator.count(), 300)):
                    item = locator.nth(index)
                    box = item.bounding_box(timeout=1500)
                    meta = item.evaluate(
                        """
                        (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return {
                                tag: el.tagName,
                                src: el.src || '',
                                text: (el.innerText || el.textContent || '').trim(),
                                class: String(el.className || ''),
                                visible: rect.width > 0 &&
                                    rect.height > 0 &&
                                    style.display !== 'none' &&
                                    style.visibility !== 'hidden' &&
                                    Number(style.opacity || '1') > 0,
                            };
                        }
                        """
                    )
                    priority = _qr_candidate_priority(meta, box)
                    if priority is None:
                        continue
                    width = float(box["width"])
                    height = float(box["height"])
                    center_penalty = abs(width - 180) + abs(height - 180)
                    score = (priority, center_penalty)
                    if best_score is None or score < best_score:
                        best = item
                        best_score = score
            except Exception:
                continue
        if best is not None:
            best.screenshot(path=str(screenshot_path), timeout=5_000)
            return True
        if allow_page_fallback:
            self._page.screenshot(path=str(screenshot_path), full_page=False)
        return False

    def _is_api_logged_in(self, timeout_ms: int = 10_000) -> bool:
        try:
            page = self._fetch_collection_api_page(cursor=0, timeout_ms=timeout_ms)
        except Exception as e:
            logger.debug("Login check failed: {}", e)
            return False
        return bool(page.raw_payload.get("status_code") == 0)

    def _has_login_cookies(self) -> bool:
        return bool(self._login_cookie_fingerprint())

    def _login_cookie_fingerprint(self) -> tuple[tuple[str, str], ...]:
        if self._context is None:
            return ()
        try:
            cookies = self._context.cookies([
                "https://www.douyin.com",
                "https://login.douyin.com",
            ])
        except Exception:
            return ()
        interesting = []
        for cookie in cookies:
            name = cookie.get("name")
            if name in {"sessionid", "sessionid_ss", "sid_guard"}:
                interesting.append((str(name), str(cookie.get("value") or "")))
        return tuple(sorted(interesting))

    def _has_usable_login_state(self, timeout_ms: int = 5_000) -> bool:
        if not self._has_login_cookies():
            return False
        try:
            self._ensure_api_page_ready()
        except Exception as e:
            logger.debug("Could not prepare API page for login-state check: {}", e)
            return False
        return self._is_api_logged_in(timeout_ms=timeout_ms)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _wait_for_first_response(self, timeout_s: int) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._page_queue:
                return True
            time.sleep(0.5)
        return False

    def _wait_until_idle(self, idle_seconds: int, max_wait_s: int) -> None:
        """CDP 模式用：等响应停止增长一段时间后认为完成。"""
        deadline = time.time() + max_wait_s
        last_count = len(self._page_queue)
        last_change = time.time()
        while time.time() < deadline:
            cur = len(self._page_queue)
            if cur > last_count:
                last_count = cur
                last_change = time.time()
                logger.info("CDP 模式收到第 {} 页响应，继续等用户滚动...", cur)
            if time.time() - last_change > idle_seconds:
                logger.info("响应已 {} 秒没增长，认为完成。", idle_seconds)
                return
            time.sleep(1.0)

    def _dump_seen_xhr_summary(self) -> None:
        """诊断输出：抓不到 listcollection 时打印我们看到了哪些 URL。"""
        if not self._seen_urls:
            logger.warning("整个会话里一个匹配的 XHR 都没见过。说明 collection 接口压根没被请求 —— 页面可能根本没渲染（反自动化检测）。")

    def _scroll_one_step(self) -> None:
        """模拟人类向下滚屏 ——
        多次小幅 mouse wheel 事件（不是 JS 直接跳，因为抖音的 lazy load
        依赖真实 mousewheel 事件 + IntersectionObserver），最后兜底用
        scrollTo 确保到达底部。"""
        assert self._page is not None
        try:
            # 1. 真实 mouse wheel 事件，6 次小幅滚动模拟连续滚轮
            for _ in range(6):
                self._page.mouse.wheel(0, 700)
                time.sleep(0.15)  # 模拟人类滚轮节奏

            # 2. 兜底：也用 JS 把外层 + 任何内嵌可滚容器都滑到底
            self._page.evaluate(
                """
                window.scrollTo(0, document.body.scrollHeight);
                // 兼容某些把内容放在内层 overflow:auto 容器里的布局
                document.querySelectorAll('*').forEach(el => {
                    if (el.scrollHeight > el.clientHeight + 50) {
                        const s = getComputedStyle(el);
                        if (s.overflowY === 'auto' || s.overflowY === 'scroll') {
                            el.scrollTop = el.scrollHeight;
                        }
                    }
                });
                """
            )
        except PWTimeout:
            logger.warning("scroll evaluate timed out, retrying...")
        except Exception as e:
            logger.warning("scroll error: {}", e)
