"""
"被遗忘的宝贝" 选取算法 + 同期对照。

主选取 pick()：
- 候选池：is_removed=0 且 last_recalled_at 在 cooldown 之外
- 权重 = log(age_days + 7) × note_boost × sqrt(log(digg_count + 10))
  * age_days 优先用 favorited_at，否则用 discovery_index 线性插值
  * note_boost = 3 if 有 user_note else 1（用户写过备注 = 显式在乎）
  * sqrt(log(digg_count)) 是温和的"社会热度"加成（睡眠爆款）
- 最终 selection 阶段同作者最多 1 条，避免某个 up 主刷屏

回忆角 pick_anniversary() / pick_milestone()：
- anniversary：视频本身在 N 年前的这周发布过（N ∈ 1..3）
- milestone：你正好 30/90/180/365/730 天前收藏的（±4 天窗口）
- 各最多 1 条，没有满足条件就返回空列表

调用方负责（cli.py 里做）：
- 把三种 pick 的 id 合并，调 mark_recalled()
- 渲染邮件
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from loguru import logger

from src.content.kinds import get_content_kind
from src.config import settings
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


# 首次抓取那 973 条都没 favorited_at，用 discovery_index 估龄时假设的"散布天数"。
# 即：discovery_index=0（最老）→ 视为 ASSUMED_FIRST_CRAWL_SPREAD_DAYS 天前收藏；
#     discovery_index=max → 视为很接近 first_seen_at（30 天前）收藏。
ASSUMED_FIRST_CRAWL_SPREAD_DAYS = 730  # 2 年


@dataclass
class Candidate:
    id: str
    title: Optional[str]
    author: Optional[str]
    video_url: Optional[str]
    cover_url: Optional[str]
    favorited_at: Optional[datetime]
    first_seen_at: Optional[datetime]
    last_recalled_at: Optional[datetime]
    user_note: Optional[str]
    discovery_index: Optional[int]
    video_created_at: Optional[datetime]
    digg_count: Optional[int]

    # 同期对照命中信息（只在 pick_anniversary / pick_milestone 返回时填）
    anniversary_years: Optional[int] = None      # N（如 1 表示"去年这周"）
    milestone_days: Optional[int] = None         # 30/90/180/365/730

    @property
    def days_since_recall(self) -> int:
        """距离上次被召回多久。从未召回过返回一个大数。"""
        if self.last_recalled_at is None:
            return 9999
        delta = datetime.now(timezone.utc) - _ensure_utc(self.last_recalled_at)
        return max(0, delta.days)

    @property
    def days_since_first_seen(self) -> int:
        if self.first_seen_at is None:
            return 0
        delta = datetime.now(timezone.utc) - _ensure_utc(self.first_seen_at)
        return max(0, delta.days)

    @property
    def days_since_favorited(self) -> int:
        """距离收藏的天数。优先用真实 favorited_at；为空时用 first_seen_at 兜底。"""
        if self.favorited_at is not None:
            delta = datetime.now(timezone.utc) - _ensure_utc(self.favorited_at)
            return max(0, delta.days)
        return self.days_since_first_seen


def _ensure_utc(dt: datetime) -> datetime:
    """SQLite 读出来的 datetime 是 naive 的，按 UTC 解读。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_datetime(value: object) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Could not parse datetime value: {}", text)
        return None


# ============================================================
# 龄估算：把"应该多老"统一映射到一个 days 数字
# ============================================================

def _estimate_age_days(
    c: Candidate,
    max_first_crawl_di: Optional[int],
) -> int:
    """
    估算"这条收藏到现在多少天"。
    - 有 favorited_at：直接算
    - 没有（首抓 973 条）：用 discovery_index 在 [今天, 今天-ASSUMED_SPREAD] 之间线性插值
      * di=0 → 视为 ASSUMED_SPREAD 天前
      * di=max → 视为 first_seen_at 那天（不再加额外天数）
    """
    if c.favorited_at is not None:
        return c.days_since_favorited

    base = c.days_since_first_seen  # first_seen_at 到今天
    if max_first_crawl_di is None or max_first_crawl_di <= 0 or c.discovery_index is None:
        # 没法插值，给一个保守的"挺老"值
        return base + ASSUMED_FIRST_CRAWL_SPREAD_DAYS // 2

    # di=0 → 加 ASSUMED_SPREAD；di=max → 加 0
    ratio = 1.0 - (c.discovery_index / max_first_crawl_di)
    extra = int(ASSUMED_FIRST_CRAWL_SPREAD_DAYS * ratio)
    return base + max(0, extra)


# ============================================================
# 候选池
# ============================================================

def _row_to_candidate(row) -> Candidate:
    return Candidate(
        id=row["id"],
        title=row["title"],
        author=row["author"],
        video_url=row["video_url"],
        cover_url=row["cover_url"],
        favorited_at=_parse_datetime(row["favorited_at"]),
        first_seen_at=_parse_datetime(row["first_seen_at"]),
        last_recalled_at=_parse_datetime(row["last_recalled_at"]),
        user_note=row["user_note"],
        discovery_index=row["discovery_index"],
        video_created_at=_parse_datetime(row["video_created_at"]),
        digg_count=row["digg_count"],
    )


def _base_select(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return f"""
SELECT id, title, author, video_url, cover_url,
       CAST({kind.time_column} AS TEXT) AS favorited_at,
       CAST(first_seen_at AS TEXT) AS first_seen_at,
       CAST(last_recalled_at AS TEXT) AS last_recalled_at,
       user_note,
       discovery_index,
       CAST(video_created_at AS TEXT) AS video_created_at,
       digg_count
"""


def fetch_candidates(
    cooldown_days: Optional[int] = None,
    warmup_days: Optional[int] = None,
    ignore_warmup: bool = False,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[Candidate]:
    """从 db 取出符合"可推荐"条件的全部条目。"""
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    cooldown_days = cooldown_days if cooldown_days is not None else settings.recall_cooldown_days
    warmup_days = warmup_days if warmup_days is not None else settings.recall_warmup_days

    conn = get_connection()
    now = datetime.now(timezone.utc)
    cooldown_threshold = now - timedelta(days=cooldown_days)
    warmup_threshold = now - timedelta(days=warmup_days)

    if ignore_warmup:
        warmup_threshold = now + timedelta(days=1)  # 任何 first_seen_at 都满足

    rows = conn.execute(
        _base_select(kind.key) + f"""
        FROM {kind.table}
        WHERE user_id = ?
          AND is_removed = 0
          AND (last_recalled_at IS NULL OR last_recalled_at < ?)
          AND first_seen_at <= ?
        """,
        (user_id, cooldown_threshold, warmup_threshold),
    ).fetchall()

    candidates = [_row_to_candidate(r) for r in rows]
    logger.info(
        "Candidate pool: {} {} items (cooldown={}d, warmup={}d, ignore_warmup={})",
        len(candidates), kind.key, cooldown_days, warmup_days, ignore_warmup,
    )
    return candidates


def _max_first_crawl_di(
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> Optional[int]:
    """
    首抓批次最大的 discovery_index——所有时间列 IS NULL 的条目里取 max。
    用来给那批做线性插值估龄。
    """
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    row = conn.execute(
        f"SELECT MAX(discovery_index) AS m FROM {kind.table} WHERE user_id = ? AND {kind.time_column} IS NULL",
        (user_id,),
    ).fetchone()
    return row["m"] if row and row["m"] is not None else None


# ============================================================
# 权重函数
# ============================================================

def _compute_weight(c: Candidate, age_days: int) -> float:
    """
    weight = log(age_days + 7) × note_boost × sqrt(log(digg_count + 10))
    """
    # 1. 基础年龄（log 平滑，+7 避免 0）
    age_factor = math.log(age_days + 7)

    # 2. 备注加成（用户显式标记过）
    note_boost = 3.0 if (c.user_note and c.user_note.strip()) else 1.0

    # 3. 睡眠爆款的温和加成（开方让它不要太霸道）
    digg = c.digg_count if c.digg_count and c.digg_count > 0 else 0
    sleeper = math.sqrt(math.log(digg + 10))

    return age_factor * note_boost * sleeper


# ============================================================
# 主选取
# ============================================================

def pick(
    count: int,
    cooldown_days: Optional[int] = None,
    warmup_days: Optional[int] = None,
    ignore_warmup: bool = False,
    seed: Optional[int] = None,
    exclude_ids: Optional[Iterable[str]] = None,
    dedup_author: bool = True,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
    theme: str | None = None,
) -> list[Candidate]:
    """
    选 count 条"被遗忘的宝贝"。
    - 加权随机：log(age) × note_boost × sleeper
    - 同作者最多 1 条（dedup_author=True 时）
    - exclude_ids 里的不参与
    """
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    candidates = fetch_candidates(
        cooldown_days,
        warmup_days,
        ignore_warmup,
        content_kind=kind.key,
        user_id=user_id,
    )
    if not candidates:
        return []

    excluded = set(exclude_ids or ())
    if excluded:
        candidates = [c for c in candidates if c.id not in excluded]
        if not candidates:
            return []
    theme_text = (theme or "").strip().lower()
    if theme_text:
        def matches(c: Candidate) -> bool:
            haystack = " ".join(
                p for p in (c.title, c.author, c.user_note) if p
            ).lower()
            return theme_text in haystack

        candidates = [c for c in candidates if matches(c)]
        if not candidates:
            return []

    if seed is not None:
        random.seed(seed)

    max_di = _max_first_crawl_di(kind.key, user_id=user_id)
    weights: list[float] = []
    for c in candidates:
        age = _estimate_age_days(c, max_di)
        w = _compute_weight(c, age)
        weights.append(max(0.0001, w))

    # 加权抽样（不放回），可选同作者去重
    pool = list(zip(candidates, weights))
    picked: list[Candidate] = []
    seen_authors: set[str] = set()
    target = min(count, len(pool))

    while pool and len(picked) < target:
        total = sum(w for _, w in pool)
        if total <= 0:
            break
        roll = random.uniform(0, total)
        acc = 0.0
        for i, (c, w) in enumerate(pool):
            acc += w
            if roll <= acc:
                # 同作者去重：作者已出现过就跳过这条，但不重抽（直接 pop 放弃）
                if dedup_author and c.author and c.author in seen_authors:
                    pool.pop(i)
                    break
                picked.append(c)
                if c.author:
                    seen_authors.add(c.author)
                pool.pop(i)
                break

    logger.info("pick(): picked {} / requested {} (pool was {})",
                len(picked), count, len(candidates))
    return picked


# ============================================================
# 同期对照：anniversary（视频发布周年）
# ============================================================

# 视频发布与"今天"对齐到第 N 年的允许窗口（±天）
_ANNIVERSARY_WINDOW_DAYS = 4
# 我们关注的"几年前"
_ANNIVERSARY_YEARS = (1, 2, 3)


def pick_anniversary(
    limit: int = 1,
    exclude_ids: Optional[Iterable[str]] = None,
    seed: Optional[int] = None,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[Candidate]:
    """
    挑"视频本身在 N 年前的这周发布"的条目（N ∈ {1,2,3}）。
    随机抽 limit 条，命中越近的年份优先（先在 1 年里抽，没抽到再 2 年）。
    """
    excluded = set(exclude_ids or ())
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    if seed is not None:
        random.seed(seed)

    conn = get_connection()
    now = datetime.now(timezone.utc)

    results: list[Candidate] = []
    for years in _ANNIVERSARY_YEARS:
        if len(results) >= limit:
            break
        target_date = now - timedelta(days=365 * years)
        lo = target_date - timedelta(days=_ANNIVERSARY_WINDOW_DAYS)
        hi = target_date + timedelta(days=_ANNIVERSARY_WINDOW_DAYS)

        rows = conn.execute(
            _base_select(kind.key) + f"""
            FROM {kind.table}
            WHERE user_id = ?
              AND is_removed = 0
              AND video_created_at IS NOT NULL
              AND video_created_at BETWEEN ? AND ?
            """,
            (user_id, lo, hi),
        ).fetchall()

        # 排除 excluded 和已选中的
        already = {c.id for c in results} | excluded
        pool = [_row_to_candidate(r) for r in rows if r["id"] not in already]
        if not pool:
            continue

        # 在这个年份桶里随机抽，最多补到 limit
        need = limit - len(results)
        chosen = random.sample(pool, min(need, len(pool)))
        for c in chosen:
            c.anniversary_years = years
            results.append(c)

    logger.info("pick_anniversary(): {} hits", len(results))
    return results


# ============================================================
# 同期对照：milestone（你 N 天前收藏的）
# ============================================================

_MILESTONE_DAYS = (30, 90, 180, 365, 730)
_MILESTONE_WINDOW_DAYS = 4


def pick_milestone(
    limit: int = 1,
    exclude_ids: Optional[Iterable[str]] = None,
    seed: Optional[int] = None,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[Candidate]:
    """
    挑"你正好 30/90/180/365/730 天前收藏的"条目（±4 天）。
    只对有真实 favorited_at 的条目有效（首抓的 973 条 favorited_at 是 NULL，不参与）。
    """
    excluded = set(exclude_ids or ())
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    if seed is not None:
        random.seed(seed)

    conn = get_connection()
    now = datetime.now(timezone.utc)

    results: list[Candidate] = []
    for days in _MILESTONE_DAYS:
        if len(results) >= limit:
            break
        target_date = now - timedelta(days=days)
        lo = target_date - timedelta(days=_MILESTONE_WINDOW_DAYS)
        hi = target_date + timedelta(days=_MILESTONE_WINDOW_DAYS)

        rows = conn.execute(
            _base_select(kind.key) + f"""
            FROM {kind.table}
            WHERE user_id = ?
              AND is_removed = 0
              AND {kind.time_column} IS NOT NULL
              AND {kind.time_column} BETWEEN ? AND ?
            """,
            (user_id, lo, hi),
        ).fetchall()

        already = {c.id for c in results} | excluded
        pool = [_row_to_candidate(r) for r in rows if r["id"] not in already]
        if not pool:
            continue

        need = limit - len(results)
        chosen = random.sample(pool, min(need, len(pool)))
        for c in chosen:
            c.milestone_days = days
            results.append(c)

    logger.info("pick_milestone(): {} hits", len(results))
    return results


# ============================================================
# Mark recalled
# ============================================================

def mark_recalled(
    favorite_ids: list[str],
    channel: str = "weekly_digest",
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> None:
    """选完之后调这个，写 recall log + 更新对应内容表 last_recalled_at。"""
    if not favorite_ids:
        return
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    now = datetime.now(timezone.utc)
    conn = get_connection()
    with conn:
        conn.execute("BEGIN")
        try:
            for fid in favorite_ids:
                if kind.key == "favorites":
                    conn.execute(
                        "INSERT INTO recall_log (user_id, favorite_id, recalled_at, channel) "
                        "VALUES (?, ?, ?, ?)",
                        (user_id, fid, now, channel),
                    )
                else:
                    conn.execute(
                        "INSERT INTO like_recall_log (user_id, like_id, recalled_at, channel) "
                        "VALUES (?, ?, ?, ?)",
                        (user_id, fid, now, channel),
                    )
            placeholders = ",".join("?" for _ in favorite_ids)
            conn.execute(
                f"UPDATE {kind.table} SET last_recalled_at = ? WHERE user_id = ? AND id IN ({placeholders})",
                (now, user_id, *favorite_ids),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    logger.info("Marked {} {} as recalled via {}", len(favorite_ids), kind.key, channel)
