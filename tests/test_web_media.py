"""Tests for tenant-scoped media proxy routes."""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

from src.web import app as web_app
from src.web.routes import media


def _request(*, range_header: str | None = None) -> Request:
    headers = [] if range_header is None else [(b"range", range_header.encode("ascii"))]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/favorites/video-42/stream",
            "raw_path": b"/favorites/video-42/stream",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def test_extract_play_urls_orders_h264_by_preference_and_deduplicates() -> None:
    raw_json = json.dumps(
        {
            "video": {
                "play_addr_h264": {
                    "url_list": [
                        "https://video.test/direct",
                        "https://video.test/shared",
                    ]
                },
                "bit_rate": [
                    {
                        "bit_rate": 800,
                        "format": "mp4",
                        "is_h265": False,
                        "play_addr": {
                            "url_list": [
                                "https://video.test/medium",
                                "https://video.test/shared",
                            ]
                        },
                    },
                    {
                        "bit_rate": 1_600,
                        "format": "mp4",
                        "is_h265": False,
                        "play_addr": {"url_list": ["https://video.test/high"]},
                    },
                    {
                        "bit_rate": 3_200,
                        "format": "mp4",
                        "is_h265": True,
                        "play_addr": {"url_list": ["https://video.test/h265"]},
                    },
                    {
                        "bit_rate": 2_400,
                        "format": "webm",
                        "is_h265": False,
                        "play_addr": {"url_list": ["https://video.test/webm"]},
                    },
                ],
                "play_addr": {
                    "url_list": [
                        "https://video.test/fallback",
                        "https://video.test/high",
                    ]
                },
            }
        }
    )

    assert media.extract_play_urls(raw_json) == [
        "https://video.test/direct",
        "https://video.test/shared",
        "https://video.test/high",
        "https://video.test/medium",
        "https://video.test/fallback",
    ]


def test_favorite_and_like_stream_routes_are_owned_by_media_module() -> None:
    expected_paths = {
        "/favorites/{favorite_id}/stream",
        "/likes/{favorite_id}/stream",
    }
    routes = {
        route.path: route
        for route in web_app.app.routes
        if getattr(route, "path", None) in expected_paths
    }

    assert set(routes) == expected_paths
    for route in routes.values():
        assert route.methods == {"GET"}
        assert route.endpoint.__module__ == "src.web.routes.media"


def test_video_stream_row_scopes_query_to_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    expected_row = {"raw_json": "{}", "video_url": "https://video.test/fallback"}

    class _Cursor:
        def fetchone(self):
            return expected_row

    class _Connection:
        def execute(self, sql: str, params: tuple[str, str]):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params
            return _Cursor()

    monkeypatch.setattr(media, "get_connection", lambda: _Connection())

    assert media._video_stream_row("likes", "tenant-b", "video-42") is expected_row
    assert "WHERE user_id = ? AND id = ?" in str(captured["sql"])
    assert captured["params"] == ("tenant-b", "video-42")


@pytest.mark.parametrize(
    ("endpoint", "expected_kind"),
    [
        (media.stream_favorite_video, "favorites"),
        (media.stream_like_video, "likes"),
    ],
)
def test_stream_proxies_range_and_preserves_partial_response_headers(
    monkeypatch: pytest.MonkeyPatch,
    endpoint,
    expected_kind: str,
) -> None:
    row_calls: list[tuple[str, str, str]] = []
    sent_requests = []

    def video_stream_row(content_kind: str, user_id: str, item_id: str):
        row_calls.append((content_kind, user_id, item_id))
        return {
            "raw_json": json.dumps(
                {"video": {"play_addr_h264": {"url_list": ["https://video.test/play"]}}}
            ),
            "video_url": "https://video.test/fallback",
        }

    class _UpstreamResponse:
        status_code = 206
        headers = {
            "content-type": "video/mp4",
            "content-length": "7",
            "content-range": "bytes 10-16/100",
            "x-upstream-secret": "must-not-leak",
        }

        def __init__(self) -> None:
            self.closed = False

        async def aiter_bytes(self, _chunk_size: int):
            yield b"partial"

        async def aclose(self) -> None:
            self.closed = True

    upstream_response = _UpstreamResponse()

    class _AsyncClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["follow_redirects"] is True
            self.closed = False

        async def send(self, request, *, stream: bool):
            assert stream is True
            sent_requests.append(request)
            return upstream_response

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(media, "current_user_id", lambda _request: "tenant-b")
    monkeypatch.setattr(media, "_video_stream_row", video_stream_row)
    monkeypatch.setattr(media.httpx, "AsyncClient", _AsyncClient)

    async def exercise():
        response = await endpoint(
            _request(range_header="bytes=10-16"),
            favorite_id="video-42",
        )
        body = b"".join([chunk async for chunk in response.body_iterator])
        return response, body

    response, body = asyncio.run(exercise())

    assert row_calls == [(expected_kind, "tenant-b", "video-42")]
    assert len(sent_requests) == 1
    assert sent_requests[0].headers["range"] == "bytes=10-16"
    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == "7"
    assert response.headers["content-range"] == "bytes 10-16/100"
    assert "x-upstream-secret" not in response.headers
    assert body == b"partial"
    assert upstream_response.closed is True
