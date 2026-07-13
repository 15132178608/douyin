"""Restricted media proxy routes."""
from __future__ import annotations

from fastapi import APIRouter

from src.web import avatar_proxy


router = APIRouter()


@router.get("/avatar-cache")
async def avatar_cache(u: str):
    return await avatar_proxy.cached_avatar_response(u)
