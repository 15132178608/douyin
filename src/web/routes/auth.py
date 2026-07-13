"""Login, session, and Douyin account-binding routes."""
from __future__ import annotations

from pathlib import Path
import re
import threading
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger

from src import accounts
from src.config import settings
from src.web import job_service
from src.web import security as web_security
from src.web.helpers import current_user_id, templates


router = APIRouter()

# Per-user Douyin QR binding state.
_douyin_auth_sessions: dict[str, dict] = {}
_douyin_auth_lock = threading.Lock()


def public_douyin_auth_message(message: str | None) -> str:
    text = (message or "").strip()
    if not text:
        return "授权失败，请重试。"
    if "等待扫码超时" in text or "扫码超时" in text:
        return "扫码超时，请重新生成二维码后再试。"
    if "手机取消" in text or "取消了登录" in text or "登录已取消" in text:
        return "手机取消了登录，请重新扫码。"
    if "二维码已失效" in text or "二维码已过期" in text or "二维码失效" in text or "二维码过期" in text:
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


def should_auto_start_setup_auth(status: dict) -> bool:
    return not bool(status.get("has_profile")) or not bool(status.get("has_any_items"))


def _fetch_douyin_profile_for_user(user_id: str) -> dict:
    from src.crawler.douyin import DouyinCrawler

    with DouyinCrawler(
        headless=True,
        api_mode=True,
        hide_window=True,
        profile_path=accounts.profile_path_for_user(user_id),
    ) as crawler:
        return crawler.get_self_profile()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": ""})


@router.post("/login", response_class=HTMLResponse)
def login_claim_invite(
    request: Request,
    invite_code: str = Form(""),
    display_name: str = Form(""),
    next: str = Form("/"),
):
    client_ip = web_security.login_client_ip(request)
    retry_after = web_security.login_retry_after(client_ip, invite_code)
    if retry_after > 0:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next or "/", "error": "尝试次数过多，请稍后再试。"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    try:
        _user, token = accounts.claim_invite(invite_code, display_name=display_name)
    except accounts.InviteError as exc:
        retry_after = web_security.record_login_failure(client_ip, invite_code)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next or "/", "error": str(exc)},
            status_code=429 if retry_after > 0 else 400,
            headers={"Retry-After": str(retry_after)} if retry_after > 0 else None,
        )
    web_security.clear_login_failures(client_ip, invite_code)
    response = RedirectResponse(next or "/", status_code=303)
    web_security.set_session_cookie(response, token)
    return response


def redirect_with_session_cookie(location: str, user_id: str) -> RedirectResponse:
    token = accounts.create_session(user_id)
    response = RedirectResponse(location, status_code=303)
    web_security.set_session_cookie(response, token)
    return response


@router.get("/logout")
def logout(request: Request):
    accounts.revoke_session(request.cookies.get(settings.session_cookie_name))
    response = RedirectResponse("/login", status_code=303)
    web_security.clear_session_cookie(response)
    return response


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
        with _douyin_auth_lock:
            _douyin_auth_sessions[user_id] = {
                "status": "qr_ready",
                "message": f"请用抖音 App 扫描二维码（有效期约 {ttl}，会自动刷新）",
                "qr_path": str(capture.display_path or capture.path),
            }
        log_timing("qr-ready")

    def on_scan_pending() -> None:
        with _douyin_auth_lock:
            current = _douyin_auth_sessions.get(user_id, {})
            if current.get("status") not in {"confirmed", "success", "failed"}:
                _douyin_auth_sessions[user_id] = {
                    "status": "scan_pending",
                    "message": "手机已扫码，等待你在抖音 App 确认登录...",
                    "qr_path": current.get("qr_path"),
                }
        log_timing("scan-pending")

    def on_login_confirmed() -> None:
        with _douyin_auth_lock:
            current = _douyin_auth_sessions.get(user_id, {})
            if current.get("status") not in {"success", "failed"}:
                _douyin_auth_sessions[user_id] = {
                    "status": "confirmed",
                    "message": "确认完成，正在保存登录状态...",
                    "qr_path": None,
                }
        log_timing("login-confirmed")

    with _douyin_auth_lock:
        _douyin_auth_sessions[user_id] = {
            "status": "starting",
            "message": "正在生成二维码，请稍候...",
            "qr_path": None,
        }
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
                    logger.exception("Could not enqueue first-run jobs for {} after auth: {}", user_id, exc)
                    sync_message = "绑定成功，但自动同步加入队列失败，请打开维护中心检查。"
                with _douyin_auth_lock:
                    _douyin_auth_sessions[user_id] = {
                        "status": "success",
                        "message": f"抖音账号绑定成功！{sync_message}",
                        "qr_path": None,
                    }
                try:
                    profile = crawler.get_self_profile()
                    if profile:
                        accounts.update_douyin_profile(user_id, profile)
                        nickname = profile.get("nickname") or "抖音账号"
                        with _douyin_auth_lock:
                            current = _douyin_auth_sessions.get(user_id, {})
                            if current.get("status") == "success":
                                _douyin_auth_sessions[user_id] = {
                                    "status": "success",
                                    "message": f"{nickname}绑定成功！{sync_message}",
                                    "qr_path": None,
                                }
                    log_timing("profile-refreshed")
                except Exception as exc:
                    logger.warning("Could not refresh Douyin profile for {} after auth: {}", user_id, exc)
        if not result.success:
            with _douyin_auth_lock:
                _douyin_auth_sessions[user_id] = {
                    "status": "failed",
                    "message": public_douyin_auth_message(result.message),
                    "qr_path": None,
                }
    except Exception as exc:
        logger.exception("Douyin auth failed for {}: {}", user_id, exc)
        with _douyin_auth_lock:
            _douyin_auth_sessions[user_id] = {
                "status": "failed",
                "message": public_douyin_auth_message(f"出错了：{exc}"),
                "qr_path": None,
            }


def ensure_douyin_auth_started(user_id: str, *, force: bool = False) -> None:
    with _douyin_auth_lock:
        current = _douyin_auth_sessions.get(user_id, {})
        if not force and current.get("status") in (
            "starting", "qr_ready", "scan_pending", "confirmed", "success", "failed"
        ):
            return
        _douyin_auth_sessions[user_id] = {
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
            "douyin_nickname", "douyin_unique_id", "douyin_sec_uid", "douyin_avatar_url"
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


@router.get("/auth", response_class=HTMLResponse)
def auth_page(request: Request):
    user_id = current_user_id(request)
    session = public_auth_session_for_template(_douyin_auth_sessions.get(user_id, {}))
    profile_exists = has_saved_douyin_profile(request.state.current_user)
    douyin_accounts = accounts.list_douyin_accounts()
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "auth_status": session.get("status", "idle"),
            "auth_message": session.get("message", ""),
            "qr_ready": session.get("status") in ("qr_ready", "scan_pending"),
            "profile_exists": profile_exists,
            "douyin_accounts": douyin_accounts,
            "switchable_accounts": [row for row in douyin_accounts if row["id"] != user_id],
            "current_user_id": user_id,
        },
    )


@router.post("/auth/start", response_class=HTMLResponse)
def auth_start(request: Request):
    user_id = current_user_id(request)
    ensure_douyin_auth_started(user_id, force=True)
    session = public_auth_session_for_template(_douyin_auth_sessions.get(user_id, {}))
    return templates.TemplateResponse(
        request,
        "_auth_status.html",
        {
            "auth_status": session.get("status", "starting"),
            "auth_message": session.get("message", "启动中..."),
            "qr_ready": session.get("status") in ("qr_ready", "scan_pending"),
            "profile_exists": has_saved_douyin_profile(request.state.current_user),
        },
    )


@router.post("/auth/add")
def auth_add_account(request: Request):
    user_id = current_user_id(request)
    if has_saved_douyin_profile(request.state.current_user):
        target_user_id = accounts.create_local_douyin_account()["id"]
    else:
        target_user_id = user_id
    ensure_douyin_auth_started(target_user_id, force=True)
    return redirect_with_session_cookie("/auth", target_user_id)


@router.post("/auth/switch")
def auth_switch_account(user_id: str = Form(...)):
    target = accounts.get_user(user_id)
    if target is None or not has_saved_douyin_profile(target):
        raise HTTPException(status_code=404, detail="账号不存在或尚未登录")
    return redirect_with_session_cookie("/auth", target["id"])


@router.get("/auth/status", response_class=HTMLResponse)
def auth_status_fragment(request: Request):
    user_id = current_user_id(request)
    session = public_auth_session_for_template(_douyin_auth_sessions.get(user_id, {}))
    return templates.TemplateResponse(
        request,
        "_auth_status.html",
        {
            "auth_status": session.get("status", "idle"),
            "auth_message": session.get("message", ""),
            "qr_ready": session.get("status") in ("qr_ready", "scan_pending"),
            "profile_exists": has_saved_douyin_profile(request.state.current_user),
        },
    )


@router.post("/auth/profile/refresh", response_class=HTMLResponse)
def auth_profile_refresh(request: Request):
    user_id = current_user_id(request)
    profile_exists = has_saved_douyin_profile(request.state.current_user)
    try:
        profile = _fetch_douyin_profile_for_user(user_id)
        if not profile or not profile.get("nickname"):
            raise RuntimeError("没有从抖音读取到昵称")
        user = accounts.update_douyin_profile(user_id, profile)
        request.state.current_user = user
        profile_exists = has_saved_douyin_profile(user)
        message = "账号资料已刷新。"
        status = "success"
    except Exception as exc:
        logger.warning("Douyin profile refresh failed for {}: {}", user_id, exc)
        err_text = str(exc)
        if "用户未登录" in err_text or "登录态失效" in err_text or "请登录" in err_text:
            message = "登录态已过期，请重新绑定。昵称和头像会在重新绑定成功后自动保存。"
        else:
            message = "这次没有读到昵称和头像。通常是登录态过期或抖音没有返回账号资料，请重新绑定后再同步。"
        status = "failed"
    return templates.TemplateResponse(
        request,
        "_profile_summary.html",
        {
            "profile_exists": profile_exists,
            "profile_refresh_message": message,
            "profile_refresh_status": status,
        },
    )


@router.post("/auth/logout")
def auth_douyin_logout(request: Request):
    user_id = current_user_id(request)
    user = accounts.clear_douyin_profile(user_id)
    request.state.current_user = user
    with _douyin_auth_lock:
        _douyin_auth_sessions[user_id] = {
            "status": "idle",
            "message": "已退出抖音登录。已同步的收藏和喜欢仍保留在本机。",
            "qr_path": None,
        }
    start_douyin_logout_cleanup(user_id)
    return RedirectResponse("/auth", status_code=303)


@router.get("/auth/qr-image")
def auth_qr_image(request: Request):
    user_id = current_user_id(request)
    qr_path_str = _douyin_auth_sessions.get(user_id, {}).get("qr_path")
    if qr_path_str:
        qr_path = Path(qr_path_str)
        if qr_path.exists():
            return FileResponse(
                str(qr_path), media_type="image/png", headers={"Cache-Control": "no-store"}
            )
    return JSONResponse({"error": "no qr"}, status_code=404)
