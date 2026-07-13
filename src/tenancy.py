"""Tenant/user scoping helpers for the private-cloud mode."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from src.config import PROJECT_ROOT, settings


DEFAULT_USER_ID = "default"
_SAFE_USER_DIR = re.compile(r"^[a-z0-9_.-]+$")
_LEGACY_SAFE_USER_DIR = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_DIRS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


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


def _user_dir_name(user_id: str | None) -> str:
    value = normalize_user_id(user_id)
    reserved_base = value.rstrip(".").split(".", 1)[0].lower()
    if (
        len(value) <= 96
        and _SAFE_USER_DIR.fullmatch(value)
        and value not in {".", ".."}
        and not value.endswith(".")
        and reserved_base not in _WINDOWS_RESERVED_DIRS
    ):
        # All new profiles live in a namespace that the legacy sanitizer could
        # never produce. This prevents case-insensitive filesystems from making
        # an old ``Alice`` directory alias a new ``alice`` account.
        return f"~u-{value}"

    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip(".")
    slug = slug[:48].rstrip(".") or "user"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    # ``~`` is deliberately outside _SAFE_USER_DIR. That reserves the whole
    # hashed namespace so an attacker cannot choose a valid raw id that aliases
    # another user's sanitized directory name.
    return f"~h-{slug}-{digest}"


def user_data_dir(user_id: str | None) -> Path:
    return settings.user_data_root / _user_dir_name(user_id)


def _profile_has_data(path: Path) -> bool:
    try:
        # Chromium writes this marker at the user-data root. Merely finding an
        # arbitrary file is not enough to treat an old directory as a reusable
        # authenticated browser profile.
        return path.is_dir() and (path / "Local State").is_file()
    except OSError:
        return False


def _is_safe_legacy_user_dir_name(value: str) -> bool:
    """Return whether pre-config releases could safely have used ``value``."""
    reserved_base = value.rstrip(".").split(".", 1)[0].lower()
    return bool(
        len(value) <= 255
        and _LEGACY_SAFE_USER_DIR.fullmatch(value)
        and value not in {".", ".."}
        and not value.endswith(".")
        and reserved_base not in _WINDOWS_RESERVED_DIRS
    )


def _legacy_user_profile_path(user_id: str) -> Path | None:
    """Find an initialized legacy profile with the exact on-disk id casing."""
    if not _is_safe_legacy_user_dir_name(user_id):
        return None
    legacy_root = PROJECT_ROOT / "data" / "users"
    try:
        root_resolved = legacy_root.resolve()
        for entry in legacy_root.iterdir():
            # On NTFS, Path("Alice") also resolves for "alice". Enumerating and
            # comparing the stored name prevents two case-distinct DB users
            # from adopting the same cookie directory.
            if entry.name != user_id or not entry.is_dir():
                continue
            try:
                entry.resolve().relative_to(root_resolved)
            except (OSError, ValueError):
                return None
            profile = entry / "playwright_profile"
            return profile if _profile_has_data(profile) else None
    except OSError:
        return None
    return None


def user_playwright_profile_path(user_id: str | None) -> Path:
    uid = normalize_user_id(user_id)
    canonical = user_data_dir(uid) / "playwright_profile"
    if _profile_has_data(canonical):
        return canonical

    # Releases before USER_DATA_ROOT was wired into tenancy always wrote safe
    # user ids below the project data directory, even when a custom root was
    # configured. Keep using a populated legacy profile instead of forcing a
    # new QR login; unsafe legacy ids are intentionally not reconstructed.
    legacy_user = _legacy_user_profile_path(uid)
    if legacy_user is not None and legacy_user != canonical:
        return legacy_user

    # The original single-user layout predates per-user directories. The
    # settings loader creates this directory eagerly, so only initialized
    # Chromium profiles count as an existing login profile.
    if uid == DEFAULT_USER_ID:
        legacy_single = settings.playwright_profile_path
        if legacy_single != canonical and _profile_has_data(legacy_single):
            return legacy_single

    return canonical
