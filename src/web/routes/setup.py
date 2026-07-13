"""First-run setup routes."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from src import onboarding
from src.web import content_state, douyin_auth
from src.web.helpers import current_user_id, templates


router = APIRouter()


def _setup_auth_context(user_id: str, *, profile_exists: bool | None = None) -> dict:
    session = douyin_auth.auth_session_for_template(user_id)
    qr_path = str(session.get("qr_path") or "")
    if profile_exists is None:
        status = onboarding.get_onboarding_status(user_id)
        profile_exists = bool(status.get("profile_path_exists") or status.get("has_profile"))
    return {
        "auth_status": session.get("status", "idle"),
        "auth_message": session.get("message", ""),
        "qr_ready": session.get("status") in ("qr_ready", "scan_pending"),
        "qr_version": hashlib.sha256(qr_path.encode("utf-8")).hexdigest()[:16] if qr_path else "",
        "profile_exists": profile_exists,
    }


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    user_id = current_user_id(request)
    status = onboarding.get_onboarding_status(user_id)
    profile_exists = bool(status.get("profile_path_exists") or status.get("has_profile"))
    if douyin_auth.should_auto_start_setup_auth(status):
        douyin_auth.ensure_douyin_auth_started(user_id)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "page": "setup",
            "status": status,
            **_setup_auth_context(user_id, profile_exists=profile_exists),
            **content_state.content_stats("favorites", user_id=user_id),
        },
    )


@router.post("/setup/auth-start", response_class=HTMLResponse)
def setup_auth_start(request: Request):
    user_id = current_user_id(request)
    douyin_auth.ensure_douyin_auth_started(user_id, force=True)
    return templates.TemplateResponse(
        request, "_setup_auth_status.html", _setup_auth_context(user_id)
    )


@router.get("/setup/auth-status", response_class=HTMLResponse)
def setup_auth_status_fragment(request: Request):
    user_id = current_user_id(request)
    return templates.TemplateResponse(
        request, "_setup_auth_status.html", _setup_auth_context(user_id)
    )


@router.get("/setup/scan-state", response_class=HTMLResponse)
def setup_scan_state_fragment(request: Request, qr: str = "", state: str = ""):
    user_id = current_user_id(request)
    context = _setup_auth_context(user_id)
    if context["auth_status"] == "qr_ready" and qr == context.get("qr_version", ""):
        return Response(status_code=204)
    if context["auth_status"] == "scan_pending" and state == "scan_pending":
        return Response(status_code=204)
    if context["auth_status"] == "confirmed" and state == "confirmed":
        return Response(status_code=204)
    return templates.TemplateResponse(request, "_setup_auth_status.html", context)


@router.get("/setup/status", response_class=HTMLResponse)
def setup_status_fragment(request: Request):
    user_id = current_user_id(request)
    return templates.TemplateResponse(
        request,
        "_setup_status.html",
        {"status": onboarding.get_onboarding_status(user_id)},
    )
