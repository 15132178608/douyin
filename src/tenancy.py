"""Tenant/user scoping helpers for the private-cloud mode."""
from __future__ import annotations

from pathlib import Path
import re

from src.config import PROJECT_ROOT


DEFAULT_USER_ID = "default"


def normalize_user_id(user_id: str | None) -> str:
    value = (user_id or "").strip()
    return value or DEFAULT_USER_ID


def scoped_item_id(user_id: str | None, item_id: str) -> str:
    return f"{normalize_user_id(user_id)}:{item_id}"


def split_scoped_item_id(value: str, fallback_user_id: str | None = None) -> tuple[str, str]:
    if ":" not in value:
        return normalize_user_id(fallback_user_id), value
    user_id, item_id = value.split(":", 1)
    return normalize_user_id(user_id), item_id


def user_data_dir(user_id: str | None) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", normalize_user_id(user_id))
    return PROJECT_ROOT / "data" / "users" / safe


def user_playwright_profile_path(user_id: str | None) -> Path:
    return user_data_dir(user_id) / "playwright_profile"
