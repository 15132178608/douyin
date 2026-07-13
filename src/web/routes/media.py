"""Restricted avatar and Douyin video proxy routes."""
from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from src.content.kinds import get_content_kind
from src.web import avatar_proxy
from src.web.helpers import current_user_id, get_connection


router = APIRouter()


@router.get("/avatar-cache")
async def avatar_cache(u: str):
    return await avatar_proxy.cached_avatar_response(u)


def extract_play_urls(raw_json_str: str) -> list[str]:
    """Extract browser-compatible video URLs in descending quality order."""
    try:
        data = json.loads(raw_json_str)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    video = data.get("video", {})
    if not isinstance(video, dict):
        return []
    urls: list[str] = []

    def add(url: str) -> None:
        if url and url not in urls:
            urls.append(url)

    for url in video.get("play_addr_h264", {}).get("url_list", []):
        add(url)

    h264_bitrates = sorted(
        [
            item
            for item in video.get("bit_rate", [])
            if isinstance(item, dict)
            and not item.get("is_h265")
            and item.get("format") == "mp4"
        ],
        key=lambda item: item.get("bit_rate", 0),
        reverse=True,
    )
    for bitrate in h264_bitrates[:2]:
        for url in bitrate.get("play_addr", {}).get("url_list", []):
            add(url)

    for url in video.get("play_addr", {}).get("url_list", []):
        add(url)
    return urls[:6]


def _video_stream_row(content_kind: str, user_id: str, item_id: str):
    kind = get_content_kind(content_kind)
    return get_connection().execute(
        f"SELECT raw_json, video_url FROM {kind.table} WHERE user_id = ? AND id = ?",
        (user_id, item_id),
    ).fetchone()


async def _stream_video_for_kind(
    request: Request,
    content_kind: str,
    item_id: str,
) -> StreamingResponse:
    user_id = current_user_id(request)
    row = await asyncio.to_thread(_video_stream_row, content_kind, user_id, item_id)
    if not row or not row["raw_json"]:
        raise HTTPException(404, "not found")

    play_urls = extract_play_urls(row["raw_json"])
    if not play_urls:
        raise HTTPException(404, "no playable url in raw_json")

    proxy_headers = {
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    range_header = request.headers.get("range")
    if range_header:
        proxy_headers["Range"] = range_header

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(10, read=120),
    )
    response: httpx.Response | None = None
    for url in play_urls:
        try:
            request_upstream = httpx.Request("GET", url, headers=proxy_headers)
            candidate = await client.send(request_upstream, stream=True)
            if candidate.status_code in (200, 206):
                response = candidate
                break
            await candidate.aclose()
        except Exception as exc:
            logger.debug("video proxy attempt failed for {}: {}", url[:60], exc)

    if response is None:
        await client.aclose()
        raise HTTPException(503, "视频链接已过期，请重新同步后再试")

    response_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    for header in ("content-type", "content-length", "content-range"):
        if header in response.headers:
            response_headers[header] = response.headers[header]

    async def body():
        try:
            async for chunk in response.aiter_bytes(65536):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=response.status_code,
        headers=response_headers,
        media_type=response_headers.get("content-type", "video/mp4"),
    )


@router.get("/favorites/{favorite_id}/stream")
async def stream_favorite_video(request: Request, favorite_id: str):
    return await _stream_video_for_kind(request, "favorites", favorite_id)


@router.get("/likes/{favorite_id}/stream")
async def stream_like_video(request: Request, favorite_id: str):
    return await _stream_video_for_kind(request, "likes", favorite_id)
