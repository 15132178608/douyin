"""
增量索引：把还没编码的 favorites 写进向量表 + 全文检索表。

- 向量：用 sqlite-vec 的 serialize_float32 把 numpy 向量序列化成 BLOB
- 全文：用 jieba 把中文切成空格分隔的 token，再写 FTS5
- 增量：扫 favorites WHERE is_removed=0 AND id NOT IN favorites_vec
- 批处理：32 条一批送 encoder
- 幂等：重跑只编码新增的，已经在 vec/fts 里的会跳过
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

import jieba
import sqlite_vec
from loguru import logger

from src.content.kinds import get_content_kind
from src.db import get_connection, transaction
from src.embedding.encoder import get_encoder
from src.tenancy import DEFAULT_USER_ID, normalize_user_id, scoped_item_id


# 一次塞给 encoder 多少条
BATCH_SIZE = 32


def _extract_tag_names(video_tags_json: Optional[str]) -> list[str]:
    """从 video_tags JSON 里抽出所有 tag_name。"""
    if not video_tags_json:
        return []
    try:
        tags = json.loads(video_tags_json)
        names: list[str] = []
        for t in tags:
            name = t.get("tag_name")
            if name and name.strip():
                names.append(name.strip())
        return names
    except Exception:
        return []


def _build_text(row: dict) -> str:
    """把一条 favorite 拼成 embedding 输入用的文本。
    包含：标题/文案、作者昵称、抖音平台分类标签、用户备注。"""
    parts = []
    if row.get("title"):
        parts.append(row["title"])
    if row.get("author"):
        parts.append(f"作者：{row['author']}")
    tag_names = _extract_tag_names(row.get("video_tags"))
    if tag_names:
        parts.append("分类：" + " / ".join(tag_names))
    if row.get("user_note"):
        parts.append(f"备注：{row['user_note']}")
    if row.get("llm_tags"):
        try:
            tags = json.loads(row["llm_tags"])
        except Exception:
            tags = []
        if tags:
            parts.append("二级标签：" + " / ".join(str(t) for t in tags if t))
    return "\n".join(parts) if parts else "(无内容)"


def _jieba_tokenize(text: Optional[str]) -> str:
    """中文用 jieba 切成空格分隔，FTS5 unicode61 才能正确分词命中。"""
    if not text:
        return ""
    return " ".join(jieba.cut(text))


def find_unindexed_ids(
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[str]:
    """找出 is_removed=0 但对应 vec 表里还没有的 id。"""
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT f.id FROM {kind.table} f
        LEFT JOIN {kind.vector_table} v
          ON v.user_id = ? AND v.id = (? || ':' || f.id)
        WHERE f.user_id = ? AND f.is_removed = 0 AND v.id IS NULL
        ORDER BY f.discovery_index
        """,
        (user_id, user_id, user_id),
    ).fetchall()
    return [r["id"] for r in rows]


def _fetch_rows_by_ids(
    ids: list[str],
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> list[dict]:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT user_id, id, title, description, author, user_note, video_tags, llm_tags
        FROM {kind.table}
        WHERE user_id = ? AND id IN ({placeholders})
        """,
        [user_id, *ids],
    ).fetchall()
    return [dict(r) for r in rows]


def _write_batch(rows: list[dict], embeddings, content_kind: str = "favorites") -> None:
    """把一批 (row, embedding) 写进 vec 和 FTS 表。"""
    kind = get_content_kind(content_kind)
    conn = get_connection()
    with transaction() as tx:
        for row, emb in zip(rows, embeddings):
            index_id = scoped_item_id(row.get("user_id"), row["id"])
            # 1. vec 表
            blob = sqlite_vec.serialize_float32(emb.tolist())
            # sqlite-vec vec0 虚拟表不支持 INSERT OR REPLACE
            # 必须先 DELETE 再 INSERT
            tx.execute(f"DELETE FROM {kind.vector_table} WHERE user_id = ? AND id = ?", (row["user_id"], index_id))
            tx.execute(
                f"INSERT INTO {kind.vector_table} (id, user_id, embedding) VALUES (?, ?, ?)",
                (index_id, row["user_id"], blob),
            )

            # 2. FTS5 表（中文先 jieba 切词）
            # 先删后插，幂等
            tx.execute(f"DELETE FROM {kind.fts_table} WHERE user_id = ? AND id = ?", (row["user_id"], index_id))
            # description 字段塞抖音平台分类标签（"个人管理"、"职场技能" 这种）
            tag_names = _extract_tag_names(row.get("video_tags"))
            desc_with_tags = (row.get("description") or "")
            if tag_names:
                desc_with_tags = desc_with_tags + " " + " ".join(tag_names)
            if row.get("llm_tags"):
                try:
                    llm_tag_names = json.loads(row["llm_tags"])
                except Exception:
                    llm_tag_names = []
                if llm_tag_names:
                    desc_with_tags = desc_with_tags + " " + " ".join(str(t) for t in llm_tag_names if t)
            tx.execute(
                f"""
                INSERT INTO {kind.fts_table} (id, user_id, title, description, author, user_note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    index_id,
                    row["user_id"],
                    _jieba_tokenize(row.get("title")),
                    _jieba_tokenize(desc_with_tags),
                    _jieba_tokenize(row.get("author")),
                    _jieba_tokenize(row.get("user_note")),
                ),
            )


def index_all(
    batch_size: int = BATCH_SIZE,
    force: bool = False,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    """
    跑一次增量索引。
    force=True 会重新索引所有 favorites（包括已索引的）。
    返回统计 dict。

    M5: 编码完后，对**增量条目**（不是 force 全量）自动归类到现有最近 cluster。
    全量 force=True 一般跟着 `recall categorize --rebuild` 用，不在这里做单条归类。
    """
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    if force:
        ids = [
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM {kind.table} WHERE user_id = ? AND is_removed=0 ORDER BY discovery_index",
                (user_id,),
            ).fetchall()
        ]
    else:
        ids = find_unindexed_ids(kind.key, user_id=user_id)

    if not ids:
        logger.info("没有需要索引的新条目。")
        return {"indexed": 0, "total_in_db": _count_indexed(kind.key, user_id=user_id)}

    logger.info("即将索引 {} 条 {}...", len(ids), kind.key)

    encoder = get_encoder()
    done = 0
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i : i + batch_size]
        rows = _fetch_rows_by_ids(batch_ids, kind.key, user_id=user_id)
        # 保持顺序对齐
        id_to_row = {r["id"]: r for r in rows}
        rows = [id_to_row[bid] for bid in batch_ids if bid in id_to_row]

        texts = [_build_text(r) for r in rows]
        embeddings = encoder.encode(texts, batch_size=batch_size)

        _write_batch(rows, embeddings, kind.key)
        done += len(rows)
        logger.info("Indexed {}/{}", done, len(ids))

    # M5 增量归类：只对增量编码的（非 force）做，且仅在已有 category 时才有意义
    if not force:
        _auto_assign_categories(ids, kind.key, user_id=user_id)

    return {"indexed": done, "total_in_db": _count_indexed(kind.key, user_id=user_id)}


def _auto_assign_categories(
    ids: list[str],
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> None:
    """
    M5 hook：刚 index 完的新条目，找最近的现有 category 归过去。
    没有 category 表内容时直接跳过（用户还没跑过 `recall categorize`）。
    """
    if not ids:
        return
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    has_categories = conn.execute(
        f"SELECT 1 FROM {kind.category_table} WHERE account_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()
    if not has_categories:
        logger.debug("Skip auto-assign: no categories yet")
        return

    from src.categorize import cluster as cluster_mod

    assigned = 0
    skipped = 0
    for fid in ids:
        cid = cluster_mod.assign_one(fid, account_id=user_id, content_kind=kind.key)
        if cid is not None:
            assigned += 1
        else:
            skipped += 1
    logger.info(
        "Auto-assigned {} new items to existing categories (skipped {} as too far)",
        assigned, skipped,
    )


def _count_indexed(content_kind: str = "favorites", user_id: str = DEFAULT_USER_ID) -> int:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM {kind.vector_table} WHERE user_id = ?",
        (user_id,),
    ).fetchone()["c"]


def index_one(
    favorite_id: str,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> None:
    """
    单条重索引。写完 user_note 后调，让搜索立刻能命中新备注。
    备注变化可能影响分类（备注是命名的关键词来源），但不重跑全局聚类。
    """
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    rows = _fetch_rows_by_ids([favorite_id], kind.key, user_id=user_id)
    if not rows:
        logger.warning("index_one: {} {} not found", kind.key, favorite_id)
        return
    encoder = get_encoder()
    texts = [_build_text(rows[0])]
    embeddings = encoder.encode(texts, batch_size=1)
    _write_batch(rows, embeddings, kind.key)
    logger.debug("Re-indexed {} {}", kind.key, favorite_id)
