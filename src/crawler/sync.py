"""
增量同步：把一次抓取的结果落地到 favorites 表。

规则：
- 新 id：INSERT。first_seen_at = last_seen_at = now。
  * 如果 db 现在还没有任何记录（首次全量），分配 discovery_index = 0,1,2... 按抓取顺序。
  * 如果 db 已经有数据（后续增量），新条目的 favorited_at = now（这是真实收藏时间），
    discovery_index 在最大值上继续递增。
- 已存在的 id：UPDATE last_seen_at = now。其余字段不动（用户写的 user_note 不能被覆盖）。
- 本次没出现但 db 里有的 id：标记 is_removed = 1。不物理删除。
- 写一行 crawl_runs。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from loguru import logger

from src.content.kinds import get_content_kind
from src.db import get_connection, transaction
from src.models import CrawlResult, Favorite
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


REMOVAL_SAFETY_RATIO = 0.25
REMOVAL_SAFETY_MIN_COUNT = 20


class SuspiciousRemovalError(RuntimeError):
    """Raised when a crawl would mark an unusually large active set as removed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _max_discovery_index(conn, table: str, user_id: str = DEFAULT_USER_ID) -> int:
    row = conn.execute(
        f"SELECT MAX(discovery_index) AS m FROM {table} WHERE user_id = ?",
        (normalize_user_id(user_id),),
    ).fetchone()
    return (row["m"] or -1) if row else -1


def _removal_state(conn, table: str, user_id: str = DEFAULT_USER_ID) -> dict[str, bool]:
    rows = conn.execute(
        f"SELECT id, is_removed FROM {table} WHERE user_id = ?",
        (normalize_user_id(user_id),),
    ).fetchall()
    return {r["id"]: bool(r["is_removed"]) for r in rows}


def _is_suspicious_removal(
    removed_count: int,
    active_existing_count: int,
    *,
    removal_safety_ratio: float,
    removal_safety_min_count: int,
) -> bool:
    if active_existing_count <= 0 or removed_count < removal_safety_min_count:
        return False
    return (removed_count / active_existing_count) > removal_safety_ratio


def apply_crawl_for_kind(
    content_kind: str,
    favorites: Iterable[Favorite],
    is_first_crawl: bool | None = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    allow_large_removal: bool = False,
    removal_safety_ratio: float = REMOVAL_SAFETY_RATIO,
    removal_safety_min_count: int = REMOVAL_SAFETY_MIN_COUNT,
) -> CrawlResult:
    """
    把一批抓到的视频同步到对应内容模块的 db 表。
    favorites 顺序应该是"最新条目在前"（与抖音接口返回顺序一致）。

    is_first_crawl: 不传则自动判断（db 里有没有数据）。
    """
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    result = CrawlResult()
    now = _now()

    favorites_list = list(favorites)
    existing_state = _removal_state(conn, kind.table, user_id)
    known_existing = set(existing_state)
    active_existing = {id_ for id_, is_removed in existing_state.items() if not is_removed}
    seen_this_run = {fav.id for fav in favorites_list}
    to_remove = active_existing - seen_this_run
    if (
        not allow_large_removal
        and _is_suspicious_removal(
            len(to_remove),
            len(active_existing),
            removal_safety_ratio=removal_safety_ratio,
            removal_safety_min_count=removal_safety_min_count,
        )
    ):
        ratio = len(to_remove) / len(active_existing)
        raise SuspiciousRemovalError(
            "Suspicious crawl removal: this run would mark "
            f"{len(to_remove)}/{len(active_existing)} active {kind.key} "
            f"({ratio:.1%}) as removed. Re-run with explicit confirmation "
            "only if this large deletion is expected."
        )

    # 判断是不是首次抓取
    # 智能判定（修复 partial-first-crawl bug）：
    # 1. db 完全空 → 是首抓
    # 2. 这次"新条目"比 db 里已有的还多 → 上次抓取明显不完整（只抓到一小部分），
    #    本次实际是"首抓的延续"，新条目应当作"已存在但本工具刚见到"，favorited_at 留 NULL
    # 3. 否则 → 正常增量
    if is_first_crawl is None:
        existing_count = len(active_existing)
        if existing_count == 0:
            is_first_crawl = True
        else:
            new_count_estimate = sum(1 for f in favorites_list if f.id not in known_existing)
            if new_count_estimate > existing_count:
                logger.warning(
                    "Detected partial-first-crawl continuation: this run has {} new "
                    "items vs db's {} existing. Treating as first-crawl continuation "
                    "(favorited_at will stay NULL for new inserts).",
                    new_count_estimate, existing_count,
                )
                is_first_crawl = True
            else:
                is_first_crawl = False

    next_discovery_idx = _max_discovery_index(conn, kind.table, user_id) + 1
    logger.info(
        "Syncing {} {} for user {} (first_crawl={}, existing_in_db={})",
        len(favorites_list), kind.key, user_id, is_first_crawl, len(active_existing),
    )

    # 首次抓取时，抖音是"新收藏在前"，但 discovery_index 我们想让"老的"是 0。
    # 所以遍历顺序反转一下：从最老到最新分配序号。
    iter_order = list(reversed(favorites_list)) if is_first_crawl else favorites_list

    with transaction() as tx:
        for fav in iter_order:
            if fav.id in known_existing:
                # 已有：只更新 last_seen_at + 一些可能变化的元数据（标题作者可能改）
                # digg_count 会涨 → 每次都刷新；video_created_at 不变，但用 COALESCE 兜底
                tx.execute(
                    f"""
                    UPDATE {kind.table}
                    SET last_seen_at      = ?,
                        title             = COALESCE(?, title),
                        author            = COALESCE(?, author),
                        cover_url         = COALESCE(?, cover_url),
                        video_url         = COALESCE(?, video_url),
                        video_tags        = COALESCE(?, video_tags),
                        raw_json          = COALESCE(?, raw_json),
                        video_created_at  = COALESCE(video_created_at, ?),
                        digg_count        = COALESCE(?, digg_count),
                        is_removed        = 0
                    WHERE id = ?
                      AND user_id = ?
                    """,
                    (now, fav.title, fav.author, fav.cover_url, fav.video_url,
                     fav.video_tags, fav.raw_json,
                     fav.video_created_at, fav.digg_count, fav.id, user_id),
                )
                result.updated_count += 1
            else:
                # 新插入
                first_action_at = None if is_first_crawl else now
                tx.execute(
                    f"""
                    INSERT INTO {kind.table} (
                        user_id, id, title, description, author, author_id,
                        video_url, cover_url, duration_ms,
                        {kind.time_column}, first_seen_at, last_seen_at,
                        raw_json, is_removed, discovery_index, video_tags,
                        video_created_at, digg_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (user_id, fav.id, fav.title, fav.description, fav.author, fav.author_id,
                     fav.video_url, fav.cover_url, fav.duration_ms,
                     first_action_at, now, now,
                     fav.raw_json, next_discovery_idx, fav.video_tags,
                     fav.video_created_at, fav.digg_count),
                )
                next_discovery_idx += 1
                result.new_count += 1

        # 本次没出现 & db 里非 removed 的：标记为 removed
        if to_remove:
            placeholders = ",".join("?" for _ in to_remove)
            tx.execute(
                f"UPDATE {kind.table} SET is_removed = 1, last_seen_at = ? "
                f"WHERE user_id = ? AND id IN ({placeholders})",
                (now, user_id, *to_remove),
            )
            result.removed_count = len(to_remove)

    logger.info(
        "Sync done: new={}, updated={}, removed={}",
        result.new_count, result.updated_count, result.removed_count,
    )
    return result


def apply_crawl(
    favorites: Iterable[Favorite],
    is_first_crawl: bool | None = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    allow_large_removal: bool = False,
    removal_safety_ratio: float = REMOVAL_SAFETY_RATIO,
    removal_safety_min_count: int = REMOVAL_SAFETY_MIN_COUNT,
) -> CrawlResult:
    """
    把一批抓到的收藏同步到 db。
    favorites 顺序应该是"最新收藏在前"（与抖音接口返回顺序一致）。

    is_first_crawl: 不传则自动判断（db 里有没有数据）。
    """
    return apply_crawl_for_kind(
        "favorites",
        favorites,
        is_first_crawl,
        user_id=user_id,
        allow_large_removal=allow_large_removal,
        removal_safety_ratio=removal_safety_ratio,
        removal_safety_min_count=removal_safety_min_count,
    )


def apply_like_crawl(
    likes: Iterable[Favorite],
    is_first_crawl: bool | None = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    allow_large_removal: bool = False,
    removal_safety_ratio: float = REMOVAL_SAFETY_RATIO,
    removal_safety_min_count: int = REMOVAL_SAFETY_MIN_COUNT,
) -> CrawlResult:
    """把一批抓到的喜欢视频同步到 likes 表。"""
    return apply_crawl_for_kind(
        "likes",
        likes,
        is_first_crawl,
        user_id=user_id,
        allow_large_removal=allow_large_removal,
        removal_safety_ratio=removal_safety_ratio,
        removal_safety_min_count=removal_safety_min_count,
    )


def record_crawl_run_for_kind(
    content_kind: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    result: CrawlResult | None = None,
    error_message: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> int:
    """写一行对应内容模块的 crawl run，返回 rowid。"""
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    cur = conn.execute(
        f"""
        INSERT INTO {kind.crawl_runs_table} (user_id, started_at, finished_at, status,
                                             new_count, updated_count, removed_count,
                                             error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, started_at, finished_at, status,
         result.new_count if result else 0,
         result.updated_count if result else 0,
         result.removed_count if result else 0,
         error_message or (result.error_message if result else None)),
    )
    return cur.lastrowid


def record_crawl_run(
    started_at: datetime,
    finished_at: datetime,
    status: str,
    result: CrawlResult | None = None,
    error_message: str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> int:
    """写一行 crawl_runs，返回 rowid。"""
    return record_crawl_run_for_kind(
        "favorites",
        started_at,
        finished_at,
        status,
        result,
        error_message,
        user_id=user_id,
    )
