"""
向量检索 + 全文检索 混合，RRF 融合。

为什么混合：
- 向量擅长语义（"做饭" 能召回 "美食"、"菜谱"）
- FTS5 擅长精确（搜某个作者名、特定 hashtag）
- 两者互补；只用一个总有漏召的情况

RRF (Reciprocal Rank Fusion)：
- 每个检索器各返回 top K 排序结果
- 一条文档 d 在第 i 个检索器里排第 rank_i 名
- 融合分 = Σ_i 1 / (60 + rank_i)，60 是经验常数
- 优点：不要求两个检索器分数可比，只看排名
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import jieba
import sqlite_vec
from loguru import logger

from src.content.kinds import get_content_kind
from src.db import get_connection
from src.embedding.encoder import get_encoder
from src.tenancy import DEFAULT_USER_ID, normalize_user_id, split_scoped_item_id


# 每个检索器各取多少个候选
PER_ENGINE_TOP_K = 50

# RRF 常数，惯例 60
RRF_K = 60

# 最终返回的条数
FINAL_TOP_K = 20

# RRF 权重：向量检索更适合语义搜索，给它高一点的权重
VEC_WEIGHT = 1.6
FTS_WEIGHT = 1.0

# 向量距离阈值：bge-m3 normalized L2，1.4 ≈ 余弦相似度 0；
# < 1.2 ≈ 余弦 > 0.28（有点相关）；< 1.0 ≈ 余弦 > 0.5（比较相关）
# 太严会漏掉真正语义相关的，太松噪音多。1.25 是经验值
VEC_DISTANCE_THRESHOLD = 1.25

# 中文常见停用词（不算作有效搜索词）
# 这里只放最常见的，避免搜 "我那时候 emo" 把 "我"、"那"、"时候" 都匹配
CHINESE_STOPWORDS = {
    "的", "了", "和", "与", "是", "在", "也", "都", "就", "还", "要",
    "有", "没", "不", "我", "你", "他", "她", "它", "这", "那", "些",
    "啊", "吧", "呢", "吗", "嘛", "哦", "呀",
    "把", "被", "给", "对", "向", "从", "为", "以", "到",
    "上", "下", "里", "中", "之", "其", "如", "于",
    "a", "an", "the", "of", "in", "on", "at", "to", "for",
}


@dataclass
class SearchHit:
    id: str
    title: Optional[str]
    author: Optional[str]
    raw_json: Optional[str]
    video_url: Optional[str]
    cover_url: Optional[str]
    user_note: Optional[str]
    favorited_at: Optional[str]
    first_seen_at: Optional[str]
    video_created_at: Optional[str]
    last_recalled_at: Optional[str]
    score: float                # RRF 融合分
    vec_rank: Optional[int]     # 在向量检索里的排名（1-based），None 表示没命中
    fts_rank: Optional[int]     # 在 FTS 里的排名（1-based），None 表示没命中


def _vec_search(query: str, top_k: int = PER_ENGINE_TOP_K,
                distance_threshold: float = VEC_DISTANCE_THRESHOLD,
                content_kind: str = "favorites",
                user_id: str = DEFAULT_USER_ID) -> list[tuple[str, float]]:
    """向量检索，返回 [(id, distance), ...]，按 distance 升序（越近越前）。
    超过距离阈值的会被砍掉（明显不相关的不进入 top）。"""
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    encoder = get_encoder()
    q_emb = encoder.encode([query])[0]
    q_blob = sqlite_vec.serialize_float32(q_emb.tolist())

    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id, distance FROM {kind.vector_table}
        WHERE embedding MATCH ?
        ORDER BY distance
        LIMIT ?
        """,
        (q_blob, top_k * 5),
    ).fetchall()
    # 过滤掉距离过远的（明显不相关）
    filtered = []
    for r in rows:
        row_user_id, _item_id = split_scoped_item_id(r["id"], fallback_user_id=DEFAULT_USER_ID)
        if row_user_id != user_id:
            continue
        if r["distance"] <= distance_threshold:
            filtered.append((r["id"], r["distance"]))
        if len(filtered) >= top_k:
            break
    if len(filtered) < len(rows):
        logger.debug(
            "Vec search: dropped {} far-away results (threshold={})",
            len(rows) - len(filtered), distance_threshold,
        )
    return filtered


def _clean_tokens(tokens: list[str]) -> list[str]:
    """过滤停用词、空白、单字符标点。"""
    cleaned: list[str] = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if t.lower() in CHINESE_STOPWORDS:
            continue
        # 单个 ASCII 标点
        if len(t) == 1 and not t.isalnum() and not ("一" <= t <= "鿿"):
            continue
        cleaned.append(t)
    return cleaned


def _fts_search(
    query: str,
    top_k: int = PER_ENGINE_TOP_K,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[tuple[str, float]]:
    """FTS5 检索，中文 query 先 jieba 切词 + 停用词过滤。返回 [(id, bm25_score), ...]。

    用 AND 连接所有有效 token，要求文档同时包含全部关键词。这比 OR 严格但
    噪音少很多。配合 stopword 过滤，"做菜的" → 只有"做菜"参与匹配。
    """
    tokens = _clean_tokens(list(jieba.cut(query)))
    if not tokens:
        return []
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)

    # 引号包裹防止 token 里有 FTS5 特殊字符
    fts_query = " AND ".join(f'"{t}"' for t in tokens)

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT id, bm25({kind.fts_table}) AS score
            FROM {kind.fts_table}
            WHERE {kind.fts_table} MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, top_k * 5),
        ).fetchall()
        results = [
            (r["id"], r["score"])
            for r in rows
            if split_scoped_item_id(r["id"], fallback_user_id=DEFAULT_USER_ID)[0] == user_id
        ][:top_k]
        # AND 模式可能召不回；如果空，降级到 OR
        if not results and len(tokens) > 1:
            fts_query_or = " OR ".join(f'"{t}"' for t in tokens)
            rows = conn.execute(
                f"""
                SELECT id, bm25({kind.fts_table}) AS score
                FROM {kind.fts_table}
                WHERE {kind.fts_table} MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (fts_query_or, top_k * 5),
            ).fetchall()
            results = [
                (r["id"], r["score"])
                for r in rows
                if split_scoped_item_id(r["id"], fallback_user_id=DEFAULT_USER_ID)[0] == user_id
            ][:top_k]
            logger.debug("FTS AND empty, fell back to OR with {} hits", len(results))
        return results
    except Exception as e:
        logger.warning("FTS search failed for query '{}': {}", fts_query, e)
        return []


def _rrf_fuse(
    vec_ranks: list[tuple[str, float]],
    fts_ranks: list[tuple[str, float]],
    k: int = RRF_K,
    vec_weight: float = VEC_WEIGHT,
    fts_weight: float = FTS_WEIGHT,
) -> list[tuple[str, float, Optional[int], Optional[int]]]:
    """
    输入两个排序列表，做加权 RRF 融合。
    向量比 FTS 权重更高（语义搜索更可靠）。
    返回 [(id, score, vec_rank, fts_rank), ...]，按 score 降序。
    """
    scores: dict[str, float] = {}
    vec_rank_map: dict[str, int] = {}
    fts_rank_map: dict[str, int] = {}

    for rank, (fid, _) in enumerate(vec_ranks, start=1):
        scores[fid] = scores.get(fid, 0.0) + vec_weight / (k + rank)
        vec_rank_map[fid] = rank

    for rank, (fid, _) in enumerate(fts_ranks, start=1):
        scores[fid] = scores.get(fid, 0.0) + fts_weight / (k + rank)
        fts_rank_map[fid] = rank

    items = [
        (fid, s, vec_rank_map.get(fid), fts_rank_map.get(fid))
        for fid, s in scores.items()
    ]
    items.sort(key=lambda x: x[1], reverse=True)
    return items


def _hydrate(
    items: list[tuple[str, float, Optional[int], Optional[int]]],
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[SearchHit]:
    """根据 id 列表回对应内容表拉详细字段。"""
    if not items:
        return []
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    resolved: list[tuple[str, str, float, Optional[int], Optional[int]]] = []
    seen_ids: set[str] = set()
    for raw_id, score, vec_rank, fts_rank in items:
        row_user_id, item_id = split_scoped_item_id(raw_id, fallback_user_id=DEFAULT_USER_ID)
        if row_user_id != user_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        resolved.append((raw_id, item_id, score, vec_rank, fts_rank))
    ids = [x[1] for x in resolved]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id, title, author, video_url, cover_url, user_note,
               raw_json,
               CAST({kind.time_column} AS TEXT) AS favorited_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(video_created_at AS TEXT) AS video_created_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at
        FROM {kind.table}
        WHERE user_id = ? AND id IN ({placeholders}) AND is_removed = 0
        """,
        [user_id, *ids],
    ).fetchall()
    row_map = {r["id"]: r for r in rows}

    hits: list[SearchHit] = []
    for _raw_id, fid, score, vec_rank, fts_rank in resolved:
        r = row_map.get(fid)
        if not r:
            continue  # 可能被取消收藏了
        hits.append(SearchHit(
            id=r["id"],
            title=r["title"],
            author=r["author"],
            raw_json=r["raw_json"],
            video_url=r["video_url"],
            cover_url=r["cover_url"],
            user_note=r["user_note"],
            favorited_at=str(r["favorited_at"]) if r["favorited_at"] else None,
            first_seen_at=str(r["first_seen_at"]) if r["first_seen_at"] else None,
            video_created_at=str(r["video_created_at"]) if r["video_created_at"] else None,
            last_recalled_at=str(r["last_recalled_at"]) if r["last_recalled_at"] else None,
            score=score,
            vec_rank=vec_rank,
            fts_rank=fts_rank,
        ))
    return hits


def search(query: str, top_k: int = FINAL_TOP_K) -> list[SearchHit]:
    """混合检索主入口。"""
    return search_for_kind(query, top_k=top_k, content_kind="favorites")


def search_for_kind(
    query: str,
    top_k: int = FINAL_TOP_K,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[SearchHit]:
    """按内容模块做混合检索。"""
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    query = (query or "").strip()
    if not query:
        return []

    vec_results = _vec_search(query, content_kind=kind.key, user_id=user_id)
    fts_results = _fts_search(query, content_kind=kind.key, user_id=user_id)

    logger.debug(
        "Search '{}' in {}: vec={} hits, fts={} hits",
        query, kind.key, len(vec_results), len(fts_results),
    )

    fused = _rrf_fuse(vec_results, fts_results)[:top_k]
    return _hydrate(fused, content_kind=kind.key, user_id=user_id)
