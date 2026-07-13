"""Restricted remote avatar cache used by the Web UI."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _AvatarTarget:
    url: httpx.URL
    hostname: str
    addresses: tuple[str, ...]


def _normalized_hostname(value: str) -> str:
    host = value.rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _allowed_avatar_hosts() -> tuple[str, ...]:
    hosts: list[str] = []
    for item in str(settings.avatar_allowed_host_suffixes or "").split(","):
        hostname = _normalized_hostname(item.strip().lstrip("."))
        if hostname:
            hosts.append(hostname)
    return tuple(hosts)


def _host_is_allowed(hostname: str) -> bool:
    host = _normalized_hostname(hostname)
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _allowed_avatar_hosts())


async def _resolve_host_addresses(hostname: str, port: int) -> tuple[str, ...]:
    rows = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    return tuple(dict.fromkeys(str(row[4][0]) for row in rows))


def _validated_public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid avatar host") from exc
    mapped_address = getattr(address, "ipv4_mapped", None)
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or mapped_address is not None
    ):
        raise HTTPException(status_code=400, detail="invalid avatar host")
    return str(address)


async def _validate_avatar_url(value: str) -> _AvatarTarget:
    if len(value) > 4096:
        raise HTTPException(status_code=400, detail="invalid avatar url")
    try:
        parsed = urlparse(value)
        hostname = _normalized_hostname(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid avatar url") from exc
    try:
        request_url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise HTTPException(status_code=400, detail="invalid avatar url") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _host_is_allowed(hostname)
        or request_url.scheme != "https"
        or _normalized_hostname(request_url.host) != hostname
    ):
        raise HTTPException(status_code=400, detail="invalid avatar url")
    try:
        addresses = await _resolve_host_addresses(hostname, port or 443)
    except OSError as exc:
        raise HTTPException(status_code=502, detail="avatar host unavailable") from exc
    if not addresses:
        raise HTTPException(status_code=502, detail="avatar host unavailable")
    validated_addresses = tuple(_validated_public_address(address) for address in addresses)
    return _AvatarTarget(
        url=request_url,
        hostname=hostname,
        addresses=tuple(dict.fromkeys(validated_addresses)),
    )


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )


async def _download_avatar(remote_url: str) -> bytes:
    max_bytes = max(1, int(settings.avatar_max_bytes))
    max_redirects = max(0, int(settings.avatar_max_redirects))
    current_url = remote_url
    for redirect_count in range(max_redirects + 1):
        target = await _validate_avatar_url(current_url)
        redirect_url: str | None = None
        last_request_error: httpx.RequestError | None = None
        for address in target.addresses:
            try:
                async with _new_client() as client:
                    async with client.stream(
                        "GET",
                        target.url.copy_with(host=address),
                        headers={
                            "Accept": "image/*",
                            "User-Agent": "DouyinRecall/1",
                            "Host": target.hostname,
                        },
                        extensions={"sni_hostname": target.hostname},
                    ) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location or redirect_count >= max_redirects:
                                raise HTTPException(status_code=502, detail="avatar redirect rejected")
                            redirect_url = urljoin(str(target.url), location)
                            break
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
                last_request_error = exc
        if redirect_url is not None:
            current_url = redirect_url
            continue
        if last_request_error is not None:
            raise HTTPException(status_code=502, detail="avatar fetch failed") from last_request_error
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
    target = await _validate_avatar_url(remote_url)
    suffix = Path(target.url.path).suffix.lower()
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
