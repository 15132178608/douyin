"""Restricted remote avatar cache used by the Web UI."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
from urllib.parse import unquote, urljoin, urlparse

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

from src.config import settings


_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _allowed_avatar_hosts() -> tuple[str, ...]:
    return tuple(
        item.strip().lower().lstrip(".")
        for item in str(settings.avatar_allowed_host_suffixes or "").split(",")
        if item.strip()
    )


def _host_is_allowed(hostname: str) -> bool:
    host = hostname.rstrip(".").lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _allowed_avatar_hosts())


async def _resolve_host_addresses(hostname: str, port: int) -> set[str]:
    rows = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    return {str(row[4][0]) for row in rows}


async def _validate_avatar_url(value: str) -> str:
    if len(value) > 4096:
        raise HTTPException(status_code=400, detail="invalid avatar url")
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid avatar url") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _host_is_allowed(hostname)
    ):
        raise HTTPException(status_code=400, detail="invalid avatar url")
    try:
        addresses = await _resolve_host_addresses(hostname, port or 443)
    except OSError as exc:
        raise HTTPException(status_code=502, detail="avatar host unavailable") from exc
    if not addresses:
        raise HTTPException(status_code=502, detail="avatar host unavailable")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise HTTPException(status_code=400, detail="invalid avatar host")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid avatar host") from exc
    return value


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False)


async def _download_avatar(remote_url: str) -> bytes:
    max_bytes = max(1, int(settings.avatar_max_bytes))
    max_redirects = max(0, int(settings.avatar_max_redirects))
    current_url = remote_url
    async with _new_client() as client:
        for redirect_count in range(max_redirects + 1):
            current_url = await _validate_avatar_url(current_url)
            try:
                async with client.stream(
                    "GET",
                    current_url,
                    headers={"Accept": "image/*", "User-Agent": "DouyinRecall/1"},
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location or redirect_count >= max_redirects:
                            raise HTTPException(status_code=502, detail="avatar redirect rejected")
                        current_url = urljoin(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise HTTPException(status_code=502, detail="avatar fetch failed") from exc
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if not content_type.startswith("image/"):
                        raise HTTPException(status_code=415, detail="avatar response is not an image")
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise HTTPException(status_code=413, detail="avatar is too large")
                        except ValueError:
                            pass
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise HTTPException(status_code=413, detail="avatar is too large")
                    return bytes(payload)
            except HTTPException:
                raise
            except httpx.RequestError as exc:
                raise HTTPException(status_code=502, detail="avatar fetch failed") from exc
    raise HTTPException(status_code=502, detail="avatar redirect rejected")


def _write_cache_atomically(cache_path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=cache_path.parent,
            prefix=f".{cache_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, cache_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


async def cached_avatar_response(encoded_url: str) -> FileResponse:
    remote_url = unquote(encoded_url or "")
    await _validate_avatar_url(remote_url)
    parsed = urlparse(remote_url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        suffix = ".img"
    cache_dir = settings.avatar_cache_dir
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    cache_path = cache_dir / f"{hashlib.sha256(remote_url.encode('utf-8')).hexdigest()}{suffix}"
    max_bytes = max(1, int(settings.avatar_max_bytes))
    if cache_path.exists() and cache_path.stat().st_size > max_bytes:
        await asyncio.to_thread(cache_path.unlink)
    if not cache_path.exists():
        payload = await _download_avatar(remote_url)
        await asyncio.to_thread(_write_cache_atomically, cache_path, payload)
    return FileResponse(cache_path)
