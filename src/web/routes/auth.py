"""Login, session, and Douyin account-binding routes."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger

from src import accounts
from src.config import settings
from src.web import douyin_auth
from src.web import security as web_security
from src.web.helpers import current_user_id, templates


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    next_target = web_security.safe_local_redirect_target(next)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_target, "error": ""},
    )


@router.post("/login", response_class=HTMLResponse)
def login_claim_invite(
    request: Request,
    invite_code: str = Form(""),
    display_name: str = Form(""),
    next: str = Form("/"),
):
    next_target = web_security.safe_local_redirect_target(next)
    client_ip = web_security.login_client_ip(request)
    retry_after = web_security.login_retry_after(client_ip, invite_code)
    if retry_after > 0:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next_target, "error": "尝试次数过多，请稍后再试。"},
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
            {"next": next_target, "error": str(exc)},
            status_code=429 if retry_after > 0 else 400,
            headers={"Retry-After": str(retry_after)} if retry_after > 0 else None,
        )
    web_security.clear_login_failures(client_ip, invite_code)
    response = RedirectResponse(next_target, status_code=303)
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


@router.get("/auth", response_class=HTMLResponse)
def auth_page(request: Request):
    user_id = current_user_id(request)
    session = douyin_auth.auth_session_for_template(user_id)
    profile_exists = douyin_auth.has_saved_douyin_profile(request.state.current_user)
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
    douyin_auth.ensure_douyin_auth_started(user_id, force=True)
    session = douyin_auth.auth_session_for_template(user_id)
    return templates.TemplateResponse(
        request,
        "_auth_status.html",
        {
            "auth_status": session.get("status", "starting"),
            "auth_message": session.get("message", "启动中..."),
            "qr_ready": session.get("status") in ("qr_ready", "scan_pending"),
            "profile_exists": douyin_auth.has_saved_douyin_profile(request.state.current_user),
        },
    )


@router.post("/auth/add")
def auth_add_account(request: Request):
    user_id = current_user_id(request)
    if douyin_auth.has_saved_douyin_profile(request.state.current_user):
        target_user_id = accounts.create_local_douyin_account()["id"]
    else:
        target_user_id = user_id
    douyin_auth.ensure_douyin_auth_started(target_user_id, force=True)
    return redirect_with_session_cookie("/auth", target_user_id)


@router.post("/auth/switch")
def auth_switch_account(user_id: str = Form(...)):
    target = accounts.get_user(user_id)
    if target is None or not douyin_auth.has_saved_douyin_profile(target):
        raise HTTPException(status_code=404, detail="账号不存在或尚未登录")
    return redirect_with_session_cookie("/auth", target["id"])


@router.get("/auth/status", response_class=HTMLResponse)
def auth_status_fragment(request: Request):
    user_id = current_user_id(request)
    session = douyin_auth.auth_session_for_template(user_id)
    return templates.TemplateResponse(
        request,
        "_auth_status.html",
        {
            "auth_status": session.get("status", "idle"),
            "auth_message": session.get("message", ""),
            "qr_ready": session.get("status") in ("qr_ready", "scan_pending"),
            "profile_exists": douyin_auth.has_saved_douyin_profile(request.state.current_user),
        },
    )


@router.post("/auth/profile/refresh", response_class=HTMLResponse)
def auth_profile_refresh(request: Request):
    user_id = current_user_id(request)
    profile_exists = douyin_auth.has_saved_douyin_profile(request.state.current_user)
    try:
        profile = douyin_auth.fetch_douyin_profile_for_user(user_id)
        if not profile or not profile.get("nickname"):
            raise RuntimeError("没有从抖音读取到昵称")
        user = accounts.update_douyin_profile(user_id, profile)
        request.state.current_user = user
        profile_exists = douyin_auth.has_saved_douyin_profile(user)
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
    douyin_auth.set_auth_session(
        user_id,
        {
            "status": "idle",
            "message": "已退出抖音登录。已同步的收藏和喜欢仍保留在本机。",
            "qr_path": None,
        },
    )
    douyin_auth.start_douyin_logout_cleanup(user_id)
    return RedirectResponse("/auth", status_code=303)


@router.get("/auth/qr-image")
def auth_qr_image(request: Request):
    user_id = current_user_id(request)
    qr_path = douyin_auth.qr_image_path(user_id)
    if qr_path is not None:
        return FileResponse(
            str(qr_path), media_type="image/png", headers={"Cache-Control": "no-store"}
        )
    return JSONResponse({"error": "no qr"}, status_code=404)
