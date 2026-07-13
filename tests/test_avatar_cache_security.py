from __future__ import annotations

import asyncio
from pathlib import Path
import ssl

import httpcore
import httpx
import pytest
from fastapi import HTTPException

from src.config import settings
from src.web import avatar_proxy


class _RecordingNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self._response = bytearray(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: 10\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"image-data"
        )
        self.writes: list[bytes] = []
        self.server_hostname: str | None = None
        self.ssl_context: ssl.SSLContext | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del timeout
        payload = bytes(self._response[:max_bytes])
        del self._response[:max_bytes]
        return payload

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.writes.append(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout
        self.ssl_context = ssl_context
        self.server_hostname = server_hostname
        return self


class _RecordingNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.stream = _RecordingNetworkStream()
        self.connections: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout, local_address, socket_options
        self.connections.append((host, port))
        return self.stream

    async def connect_unix_socket(self, *args, **kwargs) -> httpcore.AsyncNetworkStream:
        del args, kwargs
        raise AssertionError("avatar transport must not use a Unix socket")

    async def sleep(self, seconds: float) -> None:
        del seconds


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


def test_download_pins_tcp_ip_but_preserves_host_sni_and_certificate_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = 0

    async def resolve(hostname: str, port: int) -> tuple[str, ...]:
        nonlocal resolve_calls
        resolve_calls += 1
        assert (hostname, port) == ("cdn.example.com", 443)
        return ("93.184.216.34",)

    backend = _RecordingNetworkBackend()

    def new_client() -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(trust_env=False)
        transport._pool._network_backend = backend
        return httpx.AsyncClient(transport=transport, follow_redirects=False)

    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    monkeypatch.setattr(settings, "avatar_max_bytes", 1024)
    monkeypatch.setattr(avatar_proxy, "_resolve_host_addresses", resolve)
    monkeypatch.setattr(avatar_proxy, "_new_client", new_client)

    payload = asyncio.run(avatar_proxy._download_avatar("https://cdn.example.com/a.jpg"))

    assert payload == b"image-data"
    assert resolve_calls == 1
    assert backend.connections == [("93.184.216.34", 443)]
    assert backend.stream.server_hostname == "cdn.example.com"
    assert backend.stream.ssl_context is not None
    assert backend.stream.ssl_context.check_hostname is True
    assert backend.stream.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert b"Host: cdn.example.com\r\n" in b"".join(backend.stream.writes)


def test_allowed_redirects_are_resolved_and_pinned_per_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []
    addresses = {
        "cdn.example.com": ("93.184.216.34",),
        "img.cdn.example.com": ("142.250.72.14",),
    }

    async def resolve(hostname: str, _port: int) -> tuple[str, ...]:
        resolved.append(hostname)
        return addresses[hostname]

    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.url.host,
                request.headers["host"],
                request.extensions["sni_hostname"],
            )
        )
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://img.cdn.example.com/b.png"},
            )
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    monkeypatch.setattr(settings, "avatar_max_bytes", 1024)
    monkeypatch.setattr(settings, "avatar_max_redirects", 2)
    monkeypatch.setattr(avatar_proxy, "_resolve_host_addresses", resolve)
    monkeypatch.setattr(
        avatar_proxy,
        "_new_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
    )

    payload = asyncio.run(avatar_proxy._download_avatar("https://cdn.example.com/a.jpg"))

    assert payload == b"png"
    assert resolved == ["cdn.example.com", "img.cdn.example.com"]
    assert requests == [
        ("93.184.216.34", "cdn.example.com", "cdn.example.com"),
        ("142.250.72.14", "img.cdn.example.com", "img.cdn.example.com"),
    ]


def test_mixed_public_and_private_dns_answers_are_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls = 0

    async def resolve(_hostname: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "127.0.0.1")

    def new_client() -> httpx.AsyncClient:
        nonlocal client_calls
        client_calls += 1
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None))

    monkeypatch.setattr(settings, "avatar_allowed_host_suffixes", "cdn.example.com")
    monkeypatch.setattr(avatar_proxy, "_resolve_host_addresses", resolve)
    monkeypatch.setattr(avatar_proxy, "_new_client", new_client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(avatar_proxy._download_avatar("https://cdn.example.com/a.jpg"))

    assert exc_info.value.status_code == 400
    assert client_calls == 0
