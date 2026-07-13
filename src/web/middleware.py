"""HTTP middleware for current-user resolution."""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse

from src import accounts
from src.config import settings
from src.tenancy import normalize_user_id


AUTH_PUBLIC_PATHS = {"/login", "/logout"}


async def attach_current_user(request: Request, call_next):
    if request.url.path in AUTH_PUBLIC_PATHS:
        return await call_next(request)
    token = request.cookies.get(settings.session_cookie_name)
    user = await asyncio.to_thread(accounts.user_from_session, token)
    if user is None and settings.web_auth_required:
        next_url = quote(
            str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")
        )
        return RedirectResponse(f"/login?next={next_url}", status_code=303)
    if user is None:
        user = await asyncio.to_thread(accounts.ensure_default_user)
    request.state.current_user = user
    request.state.user_id = normalize_user_id(user["id"])
    return await call_next(request)
