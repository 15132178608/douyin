"""
领域模型（dataclass / pydantic）。

M0 先放一个 Favorite 类型存根，M1 抓取时把抖音 API JSON 映射到这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Favorite:
    """一条收藏。字段对齐 sqlite favorites 表。"""

    id: str
    title: Optional[str] = None
    description: Optional[str] = None
    author: Optional[str] = None
    author_id: Optional[str] = None
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    duration_ms: Optional[int] = None
    favorited_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    last_recalled_at: Optional[datetime] = None
    user_note: Optional[str] = None
    raw_json: Optional[str] = None
    is_removed: bool = False
    discovery_index: Optional[int] = None
    video_tags: Optional[str] = None  # JSON 字符串
    llm_tags: Optional[str] = None  # JSON 字符串，二级标签
    video_created_at: Optional[datetime] = None  # 视频发布到抖音的时间（来自 aweme.create_time）
    digg_count: Optional[int] = None  # 视频点赞数（aweme.statistics.digg_count）

    def searchable_text(self) -> str:
        """供 embedding / FTS 用的拼接文本。"""
        parts = [self.title, self.author, self.description, self.user_note]
        return " ".join(p for p in parts if p)


@dataclass
class CrawlResult:
    """一次抓取的统计结果。"""

    new_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    error_message: Optional[str] = None
    items: list[Favorite] = field(default_factory=list)
