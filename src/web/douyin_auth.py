"""Douyin QR authorization state and browser-profile lifecycle."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
import threading
import time

from loguru import logger

from src import accounts
from src.web import job_service


_auth_sessions: dict[str, dict] = {}
_auth_lock = threading.Lock()


def public_douyin_auth_message(message: str | None) -> str:
    text = (message or "").strip()
    if not text:
        return "授权失败，请重试。"
    if "等待扫码超时" in text or "扫码超时" in text:
        return "扫码超时，请重新生成二维码后再试。"
    if "手机取消" in text or "取消了登录" in text or "登录已取消" in text:
        return "手机取消了登录，请重新扫码。"
    if any(token in text for token in ("二维码已失效", "二维码已过期", "二维码失效", "二维码过期")):
        return "二维码已失效，请重新生成二维码。"
    if "打开后台授权页失败" in text:
        return "二维码生成失败，请重新生成二维码后再试。"
    if text.startswith("出错了：") or text.startswith("出错了:"):
        return "授权过程出错，请重新生成二维码后再试。"
    cleaned = re.sub(r"二维码截图[:：].*$", "", text).strip()
    cleaned = re.sub(r"[A-Za-z]:\\[^\s，。；;]+", "", cleaned).strip()
    cleaned = cleaned.rstrip("。；;，, ")
    return f"{cleaned}。" if cleaned else "授权失败，请重试。"


def looks_sensitive_for_public_page(message: str | None) -> bool:
    text = message or ""
    return bool(
        "Traceback" in text
        or "command:" in text.lower()
        or "uv run" in text.lower()
        or re.search(r"[A-Za-z]:\\", text)
    )


def public_auth_session_for_template(session: dict | None) -> dict:
    public = dict(session or {})
    status = str(public.get("status") or "idle")
    message = str(public.get("message") or "")
    if status == "failed":
        public["message"] = public_douyin_auth_message(message)
        if looks_sensitive_for_public_page(public["message"]):
            public["message"] = "授权过程出错，请重新生成二维码后再试。"
    elif looks_sensitive_for_public_page(message):
        public["message"] = "授权状态异常，请重新生成二维码后再试。"
    return public


def auth_session_snapshot(user_id: str) -> dict:
    with _auth_lock:
        return dict(_auth_sessions.get(user_id, {}))


def auth_session_for_template(user_id: str) -> dict:
    return public_auth_session_for_template(auth_session_snapshot(user_id))


def set_auth_session(user_id: str, state: dict) -> None:
    with _auth_lock:
        _auth_sessions[user_id] = dict(state)


def clear_auth_session(user_id: str) -> None:
    with _auth_lock:
        _auth_sessions.pop(user_id, None)


def _mutate_auth_session(
    user_id: str,
    mutator: Callable[[dict], dict | None],
) -> bool:
    """Atomically replace one user's state when ``mutator`` allows the transition."""
    with _auth_lock:
        current = dict(_auth_sessions.get(user_id, {}))
        updated = mutator(current)
        if updated is None:
            return False
        _auth_sessions[user_id] = dict(updated)
        return True


def qr_image_path(user_id: str) -> Path | None:
    raw_path = auth_session_snapshot(user_id).get("qr_path")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    return path if path.is_file() else None


def should_auto_start_setup_auth(status: dict) -> bool:
    return not bool(status.get("has_profile")) or not bool(status.get("has_any_items"))


def fetch_douyin_profile_for_user(user_id: str) -> dict:
    from src.crawler.douyin import DouyinCrawler

    with DouyinCrawler(
        headless=True,
        api_mode=True,
        hide_window=True,
        profile_path=accounts.profile_path_for_user(user_id),
    ) as crawler:
        return crawler.get_self_profile()


def _run_douyin_auth(user_id: str) -> None:
    from src.crawler.douyin import AuthQrCapture, DouyinCrawler

    started_at = time.perf_counter()

    def log_timing(stage: str) -> None:
        logger.info(
            "Douyin setup QR timing for {}: {} at {:.2f}s",
            user_id,
            stage,
            time.perf_counter() - started_at,
        )

    profile_path = accounts.profile_path_for_user(user_id)
    qr_dir = profile_path.parent / "auth"
    qr_dir.mkdir(parents=True, exist_ok=True)
    qr_path = qr_dir / "douyin-login.png"

    def on_qr(capture: AuthQrCapture) -> None:
        ttl = f"{capture.ttl_seconds}s" if capture.ttl_seconds is not None else "?"
        set_auth_session(
            user_id,
            {
                "status": "qr_ready",
                "message": f"请用抖音 App 扫描二维码（有效期约 {ttl}，会自动刷新）",
                "qr_path": str(capture.display_path or capture.path),
            },
        )
        log_timing("qr-ready")

    def on_scan_pending() -> None:
        _mutate_auth_session(
            user_id,
            lambda current: (
                None
                if current.get("status") in {"confirmed", "success", "failed"}
                else {
                    "status": "scan_pending",
                    "message": "手机已扫码，等待你在抖音 App 确认登录...",
                    "qr_path": current.get("qr_path"),
                }
            ),
        )
        log_timing("scan-pending")

    def on_login_confirmed() -> None:
        _mutate_auth_session(
            user_id,
            lambda current: (
                None
                if current.get("status") in {"success", "failed"}
                else {
                    "status": "confirmed",
                    "message": "确认完成，正在保存登录状态...",
                    "qr_path": None,
                }
            ),
        )
        log_timing("login-confirmed")

    set_auth_session(
        user_id,
        {
            "status": "starting",
            "message": "正在生成二维码，请稍候...",
            "qr_path": None,
        },
    )
    log_timing("thread-start")
    try:
        with DouyinCrawler(
            headless=True,
            api_mode=True,
            hide_window=True,
            profile_path=profile_path,
        ) as crawler:
            log_timing("browser-ready")
            result = crawler.authorize_by_qr(
                timeout_s=180,
                screenshot_path=qr_path,
                on_qr_capture=on_qr,
                on_scan_pending=on_scan_pending,
                on_login_confirmed=on_login_confirmed,
            )
            log_timing("auth-flow-finished")
            if result.success:
                try:
                    enqueued = job_service.enqueue_first_run_jobs(user_id)
                    sync_message = (
                        "已自动开始同步收藏和喜欢，随后会生成搜索索引。"
                        if enqueued
                        else "同步和索引任务已在后台队列中。"
                    )
                    log_timing("sync-enqueued")
                except Exception as exc:
                    logger.exception(
                        "Could not enqueue first-run jobs for {} after auth: {}",
                        user_id,
                        exc,
                    )
                    sync_message = "绑定成功，但自动同步加入队列失败，请打开维护中心检查。"
                set_auth_session(
                    user_id,
                    {
                        "status": "success",
                        "message": f"抖音账号绑定成功！{sync_message}",
                        "qr_path": None,
                    },
                )
                try:
                    profile = crawler.get_self_profile()
                    if profile:
                        accounts.update_douyin_profile(user_id, profile)
                        nickname = profile.get("nickname") or "抖音账号"
                        _mutate_auth_session(
                            user_id,
                            lambda current: (
                                {
                                    "status": "success",
                                    "message": f"{nickname}绑定成功！{sync_message}",
                                    "qr_path": None,
                                }
                                if current.get("status") == "success"
                                else None
                            ),
                        )
                    log_timing("profile-refreshed")
                except Exception as exc:
                    logger.warning(
                        "Could not refresh Douyin profile for {} after auth: {}",
                        user_id,
                        exc,
                    )
        if not result.success:
            set_auth_session(
                user_id,
                {
                    "status": "failed",
                    "message": public_douyin_auth_message(result.message),
                    "qr_path": None,
                },
            )
    except Exception as exc:
        logger.exception("Douyin auth failed for {}: {}", user_id, exc)
        set_auth_session(
            user_id,
            {
                "status": "failed",
                "message": public_douyin_auth_message(f"出错了：{exc}"),
                "qr_path": None,
            },
        )


def ensure_douyin_auth_started(user_id: str, *, force: bool = False) -> None:
    with _auth_lock:
        current = _auth_sessions.get(user_id, {})
        if not force and current.get("status") in (
            "starting", "qr_ready", "scan_pending", "confirmed", "success", "failed"
        ):
            return
        _auth_sessions[user_id] = {
            "status": "starting",
            "message": "正在生成二维码，请稍候...",
            "qr_path": None,
        }
        threading.Thread(
            target=_run_douyin_auth,
            args=(user_id,),
            name=f"douyin-auth-{user_id}",
            daemon=True,
        ).start()


def has_saved_douyin_profile(user: dict | None) -> bool:
    if not user:
        return False
    return any(
        bool(user.get(field))
        for field in (
            "douyin_nickname",
            "douyin_unique_id",
            "douyin_sec_uid",
            "douyin_avatar_url",
        )
    )


def _clear_douyin_browser_storage(user_id: str) -> None:
    profile_path = accounts.profile_path_for_user(user_id)
    if not profile_path.exists():
        return
    try:
        from src.crawler.douyin import DouyinCrawler

        with DouyinCrawler(
            headless=True,
            api_mode=True,
            hide_window=True,
            profile_path=profile_path,
        ) as crawler:
            context = crawler._context
            if context is None:
                return
            context.clear_cookies()
            try:
                context.clear_permissions()
            except Exception:
                pass
            page = crawler._page
            if page is None:
                return
            for origin in ("https://www.douyin.com", "https://login.douyin.com"):
                try:
                    page.goto(origin, wait_until="commit", timeout=5_000)
                    page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
                except Exception as exc:
                    logger.debug(
                        "Could not clear Douyin browser storage for {} at {}: {}",
                        user_id,
                        origin,
                        exc,
                    )
    except Exception as exc:
        logger.warning("Could not clear Douyin browser storage for {}: {}", user_id, exc)


def start_douyin_logout_cleanup(user_id: str) -> None:
    threading.Thread(
        target=_clear_douyin_browser_storage,
        args=(user_id,),
        name=f"douyin-logout-cleanup-{user_id}",
        daemon=True,
    ).start()
