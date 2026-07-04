"""Version and GitHub Release update checks for the local Windows tool."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib import metadata
import json
import re
import threading
from typing import Any, Callable
from urllib import request

from src.config import PROJECT_ROOT


DEFAULT_REPO = "15132178608/douyin"
DEFAULT_INSTALLER_ASSET = "DouyinRecallSetup.exe"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 3600

Fetcher = Callable[[str, float], dict[str, Any]]

_CACHE_LOCK = threading.Lock()
_CACHE_STATUS: dict[str, Any] | None = None
_CACHE_STORED_AT: datetime | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_version(version: str | None) -> str:
    text = str(version or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def _version_key(version: str | None) -> tuple[int, int, int]:
    text = _normalize_version(version)
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if not match:
        return (0, 0, 0)
    parts = [int(part or 0) for part in match.groups()]
    return tuple((parts + [0, 0, 0])[:3])


def is_newer_version(latest: str | None, local: str | None) -> bool:
    return _version_key(latest) > _version_key(local)


def read_local_version() -> str:
    try:
        return metadata.version("douyin-recall")
    except metadata.PackageNotFoundError:
        project = PROJECT_ROOT / "pyproject.toml"
        if project.exists():
            match = re.search(
                r'^version\s*=\s*"([^"]+)"',
                project.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if match:
                return match.group(1)
    return "0.0.0"


def _release_api_url(repo: str = DEFAULT_REPO) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _default_fetcher(url: str, timeout: float) -> dict[str, Any]:
    req = request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "douyin-recall-update-check",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _installer_asset(release: dict[str, Any], asset_name: str = DEFAULT_INSTALLER_ASSET) -> dict[str, Any] | None:
    for asset in release.get("assets") or []:
        if asset.get("name") == asset_name:
            return asset
    return None


def get_update_status(
    *,
    local_version: str | None = None,
    repo: str = DEFAULT_REPO,
    fetcher: Fetcher | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return local/latest release status without downloading or installing anything."""
    local = local_version or read_local_version()
    checked_at = _utc_now().isoformat()
    fetch = fetcher or _default_fetcher
    try:
        release = fetch(_release_api_url(repo), timeout)
        latest = _normalize_version(release.get("tag_name") or release.get("name"))
        asset = _installer_asset(release)
        return {
            "local_version": local,
            "latest_version": latest or None,
            "update_available": is_newer_version(latest, local),
            "release_url": release.get("html_url"),
            "asset_name": asset.get("name") if asset else DEFAULT_INSTALLER_ASSET,
            "asset_url": asset.get("browser_download_url") if asset else None,
            "checked_at": checked_at,
            "error": None,
        }
    except Exception as e:
        return {
            "local_version": local,
            "latest_version": None,
            "update_available": False,
            "release_url": f"https://github.com/{repo}/releases",
            "asset_name": DEFAULT_INSTALLER_ASSET,
            "asset_url": None,
            "checked_at": checked_at,
            "error": str(e),
        }


def clear_update_cache() -> None:
    global _CACHE_STATUS, _CACHE_STORED_AT
    with _CACHE_LOCK:
        _CACHE_STATUS = None
        _CACHE_STORED_AT = None


def get_cached_update_status(
    *,
    force: bool = False,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    **kwargs: Any,
) -> dict[str, Any]:
    """Cache remote update checks so the polling maintenance page does not hammer GitHub."""
    global _CACHE_STATUS, _CACHE_STORED_AT
    now = _utc_now()
    with _CACHE_LOCK:
        if (
            not force
            and _CACHE_STATUS is not None
            and _CACHE_STORED_AT is not None
            and now - _CACHE_STORED_AT < timedelta(seconds=max(1, int(ttl_seconds or 1)))
        ):
            return dict(_CACHE_STATUS)

    status = get_update_status(**kwargs)
    with _CACHE_LOCK:
        _CACHE_STATUS = dict(status)
        _CACHE_STORED_AT = now
    return status
