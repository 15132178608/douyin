"""
抖音 listcollection 接口响应解析。

设计要点：
- 纯函数，不碰 db、不碰网络。输入 dict，输出 Favorite。方便单测和后续接口变化时维护。
- 字段映射对照真实响应做的（见 spec 第 4 节 M1 + 你 2026-05 抓的样本）。
- 容错：任何字段缺失都不抛异常，只让对应字段为 None / 默认值。
- 一旦抖音改字段，只改这一个文件。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from src.models import Favorite


def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    """安全地走嵌套字典/列表，任何一步取不到就返回 default。"""
    cur = d
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur if cur is not None else default


def _first_url(url_list_owner: Any) -> Optional[str]:
    """从 {url_list: [...]} 这类结构里拿第一个 URL。"""
    if not isinstance(url_list_owner, dict):
        return None
    urls = url_list_owner.get("url_list") or []
    if not urls:
        return None
    return urls[0]


def _folder_tag_names(item: dict) -> list[str]:
    """Extract user-created Douyin collection folder names when the API exposes them."""
    names: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in names:
            names.append(value.strip())

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("name", "folder_name", "title", "collection_name"):
                add(value.get(key))
            for nested_key in ("folder", "folder_info", "collect_folder", "collection"):
                walk(value.get(nested_key))
        elif isinstance(value, list):
            for entry in value:
                walk(entry)

    for key in (
        "collect_folder_list",
        "collection_folder_list",
        "folder_list",
        "folders",
        "collect_folders",
        "collect_info",
        "collection_info",
    ):
        walk(item.get(key))
    return names


def extract_aweme(item: dict) -> Optional[Favorite]:
    """
    把 aweme_list 里的一个 item 转换成 Favorite。
    无法识别（缺 aweme_id）的返回 None。
    """
    aweme_id = item.get("aweme_id")
    if not aweme_id:
        return None

    # ---- 文案 ----
    # desc 是主字段，caption 是备用（实测两者通常相同）
    title = item.get("desc") or item.get("caption") or ""

    # ---- 作者 ----
    author = _safe_get(item, "author", "nickname")
    author_id = _safe_get(item, "author", "sec_uid")

    # ---- 链接 ----
    # share_url 在顶层，share_info.share_url 偶尔为空，所以优先顶层
    video_url = item.get("share_url") or _safe_get(item, "share_info", "share_url")

    # ---- 封面 ----
    # video.cover.url_list[0] 是最稳的静态封面
    cover_url = _first_url(_safe_get(item, "video", "cover"))
    if not cover_url:
        # 退而求其次：origin_cover
        cover_url = _first_url(_safe_get(item, "video", "origin_cover"))

    # ---- 时长 ----
    # video.duration 已经是毫秒（实测 382548 对应 6分22秒视频）
    # 顶层 duration 是秒
    duration_ms: Optional[int] = _safe_get(item, "video", "duration")
    if duration_ms is None:
        secs = item.get("duration")
        if isinstance(secs, (int, float)):
            duration_ms = int(secs * 1000)

    # ---- 平台标签 ----
    # video_tag: [{level, tag_id, tag_name}, ...]
    # 我们存 JSON 字符串，查询时再 json_extract
    video_tag = item.get("video_tag")
    tags: list[dict] = list(video_tag) if isinstance(video_tag, list) else []
    for folder_name in _folder_tag_names(item):
        tags.append({"tag_name": folder_name, "source": "douyin_folder"})
    video_tags_json = json.dumps(tags, ensure_ascii=False) if tags else None

    # ---- 视频发布时间 ----
    # aweme.create_time 是 UNIX 时间戳（秒），用来支持"同期对照"功能
    create_ts = item.get("create_time")
    video_created_at: Optional[datetime] = None
    if isinstance(create_ts, (int, float)) and create_ts > 0:
        try:
            video_created_at = datetime.fromtimestamp(int(create_ts), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            video_created_at = None

    # ---- 点赞数（社会热度信号，给"睡眠爆款"权重用）----
    digg_count = _safe_get(item, "statistics", "digg_count")
    if not isinstance(digg_count, int):
        digg_count = None

    return Favorite(
        id=str(aweme_id),
        title=title or None,
        description=title or None,  # 抖音不区分，留个位
        author=author,
        author_id=author_id,
        video_url=video_url,
        cover_url=cover_url,
        duration_ms=duration_ms,
        favorited_at=None,            # 接口不返回，由 sync 层填
        first_seen_at=None,           # 由 sync 层填
        last_seen_at=None,            # 由 sync 层填
        raw_json=json.dumps(item, ensure_ascii=False),
        is_removed=False,
        video_tags=video_tags_json,
        video_created_at=video_created_at,
        digg_count=digg_count,
    )


def extract_response(payload: dict) -> tuple[list[Favorite], dict]:
    """
    解析 listcollection 接口的一整页响应。
    返回 (favorites_list, page_meta)，page_meta 含 has_more / cursor / status_code，
    供抓取器决定要不要继续翻页。
    """
    aweme_list = payload.get("aweme_list") or []
    favorites: list[Favorite] = []
    for item in aweme_list:
        fav = extract_aweme(item)
        if fav is not None:
            favorites.append(fav)

    page_meta = {
        "status_code": payload.get("status_code"),
        "has_more": payload.get("has_more"),
        "cursor": payload.get("cursor") if payload.get("cursor") is not None else payload.get("max_cursor"),
        "count": len(favorites),
        "disabled_item_ids": payload.get("disabled_item_ids") or [],
    }
    return favorites, page_meta
