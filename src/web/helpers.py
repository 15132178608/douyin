"""Shared Web helpers used by route modules and compatibility tests."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi import Request

from src.db import get_connection as _db_get_connection


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def get_connection():
    return _db_get_connection()


def current_user_id(request: Request) -> str:
    from src.tenancy import DEFAULT_USER_ID, normalize_user_id

    return normalize_user_id(getattr(request.state, "user_id", DEFAULT_USER_ID))
