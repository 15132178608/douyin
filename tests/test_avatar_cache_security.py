from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from src.config import settings
from src.web import avatar_proxy


def _install_test_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    async def resolve(_hostname: str, _port: int) -> set[str]:
        return {"93.184.216.34"}

    monkeypatch.setattr(avatar_proxy, "_resolve_host_addresses", resolve)
    monkeypatch.setattr(
        avatar_proxy,
        "_new_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
    )


def test_valid_whitelisted_avatar_is_cached_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpeg-data")

    monkeypatch.setattr(settings, "avatar_cache_dir", tmp_path)
    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    monkeypatch.setattr(settings, "avatar_max_bytes", 1024)
    _install_test_transport(monkeypatch, handler)

    first = asyncio.run(avatar_proxy.cached_avatar_response("https://cdn.example.com/a.jpg"))
    second = asyncio.run(avatar_proxy.cached_avatar_response("https://cdn.example.com/a.jpg"))

    assert calls == 1
    assert Path(first.path).read_bytes() == b"jpeg-data"
    assert Path(second.path) == Path(first.path)
    assert not list(tmp_path.glob("*.tmp"))


def test_non_whitelisted_avatar_is_rejected_before_network_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"data")

    monkeypatch.setattr(settings, "avatar_cache_dir", tmp_path)
    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    _install_test_transport(monkeypatch, handler)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(avatar_proxy.cached_avatar_response("https://evil.example/avatar.jpg"))

    assert exc_info.value.status_code == 400
    assert calls == 0
    assert list(tmp_path.iterdir()) == []


def test_avatar_redirect_to_non_whitelisted_host_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/private.jpg"})

    monkeypatch.setattr(settings, "avatar_cache_dir", tmp_path)
    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    _install_test_transport(monkeypatch, handler)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(avatar_proxy.cached_avatar_response("https://cdn.example.com/a.jpg"))

    assert exc_info.value.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_oversized_or_non_image_avatar_is_not_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * 11),
            httpx.Response(200, headers={"content-type": "text/html"}, content=b"not-image"),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    monkeypatch.setattr(settings, "avatar_cache_dir", tmp_path)
    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    monkeypatch.setattr(settings, "avatar_max_bytes", 10)
    _install_test_transport(monkeypatch, handler)

    with pytest.raises(HTTPException) as oversized:
        asyncio.run(avatar_proxy.cached_avatar_response("https://cdn.example.com/large.png"))
    with pytest.raises(HTTPException) as non_image:
        asyncio.run(avatar_proxy.cached_avatar_response("https://cdn.example.com/page.png"))

    assert oversized.value.status_code == 413
    assert non_image.value.status_code == 415
    assert list(tmp_path.iterdir()) == []
