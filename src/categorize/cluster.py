"""
M5: 自动分类核心。

算法：
- 默认 KMeans + silhouette：在 [3, min(30, sqrt(N))] 区间扫，取 silhouette score 最高的 K。
  对真实"多主题长尾"数据更稳定（每条都进簇，no surprise）。
- 备选 HDBSCAN：密度自适应，无需 K。但在密度梯度不强的数据上会塞出"一大坨+一堆未分类"。
  少数密度强的爱好类数据可以试试 --algo hdbscan。

命名：
- 每个簇内 (title + author + video_tags + user_note) 拼成长文档
- 跨簇跑 TF-IDF，取每个簇 top-3 关键词拼成 auto_name
- 用户可在 Web UI 手动改成 name

边界：
- N < MIN_FOR_CLUSTERING (30) → 不聚类，直接返回提示信息
- N > SILHOUETTE_SAMPLE_THRESHOLD (2000) → silhouette 用采样近似
- HDBSCAN 的"噪声点"（label = -1）不写 category_id，保持 NULL

多账号纪律：
- 所有 SQL 都带 `WHERE account_id = ?`，目前传 'default'。详见 multi-tenant-roadmap.md
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import sqlite_vec
from loguru import logger

from src.content.kinds import get_content_kind
from src.config import settings
from src.db import get_connection, transaction
from src.tenancy import normalize_user_id, scoped_item_id


# ============================================================
# 算法参数（顶部好调）
# ============================================================

MIN_FOR_CLUSTERING = 30          # 总收藏数 < 这个值时不做聚类
K_MAX_UX_LIMIT = 30              # 类别数硬上限（人浏览不动 50+ 个分类）
K_MIN = 3                        # KMeans 最小簇数
SILHOUETTE_SAMPLE_THRESHOLD = 2000  # 超过这个数，silhouette 计算用采样
SILHOUETTE_SAMPLE_SIZE = 2000

# HDBSCAN 默认参数
# 经验值（基于早期数据集调整）：2% 太严，容易让大量条目进噪声桶。
# 1% + ceil=30 让中等主题（10-30 条）也能成簇
HDBSCAN_MIN_CLUSTER_SIZE_FRACTION = 0.01  # 至少 1% 数据点才算一簇
HDBSCAN_MIN_CLUSTER_SIZE_FLOOR = 5         # 最少 5 个
HDBSCAN_MIN_CLUSTER_SIZE_CEIL = 30         # 最多 30 个（不让大数据集时阈值飙太高）

# 命名时每簇取多少关键词
KEYWORDS_PER_CLUSTER = 5
KEYWORDS_IN_NAME = 3

# "距离太远" 判定：单条新条目到所有现有簇中心都大于这个距离则视为未分类
# bge-m3 normalized embedding，余弦距离 1 - cos ≈ 0.75 时已经很不相关
# 偏宽松能减少"明明像 cluster X 但被丢进未分类"。可在 .env 用 NEW_ITEM_FAR_THRESHOLD 覆盖
NEW_ITEM_FAR_THRESHOLD = 0.75


# ============================================================
# 数据加载
# ============================================================

def _load_embeddings(
    account_id: str = "default",
    content_kind: str = "favorites",
) -> tuple[list[str], np.ndarray]:
    """
    从对应 vec 表拉所有未被删除条目的 embedding。
    返回 (ids, matrix [N, D])。
    """
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT f.id, v.embedding
        FROM {kind.vector_table} v
        JOIN {kind.table} f
          ON v.id = (? || ':' || f.id)
          OR (? = 'default' AND v.id = f.id)
        WHERE f.user_id = ? AND f.is_removed = 0
        """,
        (account_id, account_id, account_id),
    ).fetchall()

    if not rows:
        return [], np.empty((0, 0), dtype=np.float32)

    ids = [r["id"] for r in rows]
    matrix = np.stack([
        np.frombuffer(r["embedding"], dtype=np.float32)
        for r in rows
    ])
    logger.info("Loaded {} {} embeddings (dim={})", len(ids), kind.key, matrix.shape[1])
    return ids, matrix


def _load_texts_for(
    ids: list[str],
    content_kind: str = "favorites",
    account_id: str = "default",
) -> dict[str, str]:
    """
    给每条条目拼一段"用于 TF-IDF 命名"的文本：title + author + tags + user_note。
    返回 {id: text}。
    """
    if not ids:
        return {}
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, title, author, video_tags, user_note
        FROM {kind.table}
        WHERE user_id = ? AND id IN ({placeholders})
        """,
        [account_id, *ids],
    ).fetchall()

    out: dict[str, str] = {}
    for r in rows:
        parts: list[str] = []
        if r["title"]:
            parts.append(r["title"])
        if r["author"]:
            parts.append(r["author"])
        # video_tags 是 JSON 数组
        if r["video_tags"]:
            try:
                tags = json.loads(r["video_tags"])
                for t in tags:
                    name = t.get("tag_name") if isinstance(t, dict) else None
                    if name:
                        parts.append(name)
            except Exception:
                pass
        if r["user_note"]:
            parts.append(r["user_note"])
        out[r["id"]] = " ".join(parts)
    return out


# ============================================================
# 算法：HDBSCAN
# ============================================================

def _auto_min_cluster_size(n: int) -> int:
    """根据数据量自动选 HDBSCAN 的 min_cluster_size。"""
    val = int(n * HDBSCAN_MIN_CLUSTER_SIZE_FRACTION)
    val = max(val, HDBSCAN_MIN_CLUSTER_SIZE_FLOOR)
    val = min(val, HDBSCAN_MIN_CLUSTER_SIZE_CEIL)
    return val


def _run_hdbscan(embeddings: np.ndarray) -> np.ndarray:
    """跑 HDBSCAN。返回 labels（-1 表示噪声）。"""
    import hdbscan  # 延迟导入，加载快

    n = embeddings.shape[0]
    min_cluster_size = _auto_min_cluster_size(n)
    logger.info(
        "HDBSCAN: n={}, min_cluster_size={}",
        n, min_cluster_size,
    )

    # bge-m3 是 L2 normalized，余弦距离等价于 (2 - 2*cos)/2 的递增函数，
    # 直接用 euclidean 也是等价排序——HDBSCAN 不带 cosine metric 但
    # 在 normalized 向量上 euclidean 跟 cosine 结果非常相近
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        metric="euclidean",
        cluster_selection_method="eom",
    )
    labels = clusterer.fit_predict(embeddings)
    return labels


# ============================================================
# 算法：KMeans + silhouette
# ============================================================

def _k_candidates(n: int) -> list[int]:
    """生成 K 的候选列表。对数采样，最多试 ~8 个值。"""
    k_max = min(K_MAX_UX_LIMIT, int(math.sqrt(n)))
    if k_max < K_MIN:
        return [K_MIN]
    # 对数采样
    log_min = math.log(K_MIN)
    log_max = math.log(k_max)
    n_candidates = min(8, k_max - K_MIN + 1)
    raw = np.exp(np.linspace(log_min, log_max, n_candidates))
    candidates = sorted(set(int(round(x)) for x in raw))
    return [c for c in candidates if K_MIN <= c <= k_max]


def _run_kmeans_with_silhouette(embeddings: np.ndarray) -> tuple[int, np.ndarray]:
    """跑 KMeans，用 silhouette 自动选 K。返回 (best_k, labels)。"""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    n = embeddings.shape[0]
    candidates = _k_candidates(n)
    logger.info("KMeans: n={}, K candidates: {}", n, candidates)

    # 大数据集 silhouette 采样
    sil_kwargs = {}
    if n > SILHOUETTE_SAMPLE_THRESHOLD:
        sil_kwargs["sample_size"] = SILHOUETTE_SAMPLE_SIZE
        sil_kwargs["random_state"] = 42

    best_k = candidates[0]
    best_score = -1.0
    best_labels: Optional[np.ndarray] = None

    for k in candidates:
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = km.fit_predict(embeddings)
        try:
            score = silhouette_score(embeddings, labels, **sil_kwargs)
        except Exception as e:
            logger.warning("silhouette failed for k={}: {}", k, e)
            continue
        logger.info("  k={}: silhouette={:.4f}", k, score)
        if score > best_score:
            best_score, best_k, best_labels = score, k, labels

    if best_labels is None:
        # 兜底：硬选 K_MIN
        km = KMeans(n_clusters=K_MIN, random_state=42, n_init=10)
        best_labels = km.fit_predict(embeddings)
        best_k = K_MIN

    logger.info("KMeans selected k={} (silhouette={:.4f})", best_k, best_score)
    return best_k, best_labels


# ============================================================
# 命名：TF-IDF
# ============================================================

_TOKEN_RE = re.compile(r"[一-鿿]+|[a-zA-Z0-9_]+")

# 跟 search/hybrid.py 保持一致的中文停用词
_STOPWORDS = {
    "的", "了", "和", "与", "是", "在", "也", "都", "就", "还", "要",
    "有", "没", "不", "我", "你", "他", "她", "它", "这", "那", "些",
    "啊", "吧", "呢", "吗", "嘛", "哦", "呀",
    "把", "被", "给", "对", "向", "从", "为", "以", "到",
    "上", "下", "里", "中", "之", "其", "如", "于",
    "作者", "备注", "分类",
    "a", "an", "the", "of", "in", "on", "at", "to", "for",
}


def _tokenize_for_naming(text: str) -> list[str]:
    """切词：中文 jieba 切。过滤停用词、单字符、纯数字。"""
    import jieba
    tokens = []
    for t in jieba.cut(text):
        t = t.strip()
        if not t:
            continue
        if t in _STOPWORDS:
            continue
        # 单字符中文意义太弱
        if len(t) == 1 and "一" <= t <= "鿿":
            continue
        # 单字符 ASCII / 纯字母也基本是 jieba 把英文名切坏的产物（如 up_A → "a"）
        if len(t) == 1 and t.isascii():
            continue
        # 纯数字也是噪音（视频序号、年份等）
        if t.isdigit():
            continue
        tokens.append(t.lower() if t.isascii() else t)
    return tokens


def _extract_keywords_per_cluster(
    labels: np.ndarray,
    ids: list[str],
    texts_map: dict[str, str],
    top_k: int = KEYWORDS_PER_CLUSTER,
) -> dict[int, list[str]]:
    """
    对每个 cluster（除噪声 -1 外），算 TF-IDF 抽 top-k 关键词。
    返回 {label: [kw1, kw2, ...]}。
    """
    from sklearn.feature_extraction.text import TfidfVectorizer

    cluster_to_ids: dict[int, list[str]] = {}
    for fid, lbl in zip(ids, labels.tolist()):
        if lbl < 0:
            continue
        cluster_to_ids.setdefault(int(lbl), []).append(fid)

    if not cluster_to_ids:
        return {}

    # 每个 cluster 拼成一个长文档
    cluster_labels_sorted = sorted(cluster_to_ids.keys())
    documents = []
    for lbl in cluster_labels_sorted:
        member_texts = [texts_map.get(fid, "") for fid in cluster_to_ids[lbl]]
        # 先切词，再用空格连接，喂给 TfidfVectorizer
        all_tokens: list[str] = []
        for t in member_texts:
            all_tokens.extend(_tokenize_for_naming(t))
        documents.append(" ".join(all_tokens))

    # TF-IDF：每个 cluster 是一个 doc。token 已经空格分好，自定义 token_pattern
    vec = TfidfVectorizer(
        token_pattern=r"\S+",
        max_df=0.85,  # 在 85%+ 的簇里都出现的词压低权重
        min_df=1,
    )
    try:
        X = vec.fit_transform(documents)
    except ValueError as e:
        # 万一一个有效 token 都没有
        logger.warning("TF-IDF failed: {}", e)
        return {lbl: [] for lbl in cluster_labels_sorted}

    features = vec.get_feature_names_out()

    out: dict[int, list[str]] = {}
    for i, lbl in enumerate(cluster_labels_sorted):
        row = X.getrow(i).toarray().flatten()
        # 取 top-k 索引
        top_idx = np.argsort(row)[::-1][:top_k]
        kws = [features[j] for j in top_idx if row[j] > 0]
        out[lbl] = kws
    return out


def _build_name(keywords: list[str]) -> str:
    """关键词列表 → 展示名。简单地拼前 N 个，用 ' · ' 隔开。"""
    if not keywords:
        return "(无关键词)"
    top = keywords[:KEYWORDS_IN_NAME]
    return " · ".join(top)


# ============================================================
# 中心点 + 单条增量归类
# ============================================================

def _compute_centroids(
    labels: np.ndarray,
    embeddings: np.ndarray,
) -> dict[int, np.ndarray]:
    """对每个 cluster (>=0) 算中心向量。返回 {label: centroid}。"""
    out: dict[int, np.ndarray] = {}
    for lbl in np.unique(labels):
        if lbl < 0:
            continue
        members = embeddings[labels == lbl]
        out[int(lbl)] = members.mean(axis=0)
    return out


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """1 - cosine_similarity. a 和 b 不要求 normalized。"""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


# ============================================================
# 落 db
# ============================================================

def _wipe_old_categories(account_id: str = "default", content_kind: str = "favorites") -> None:
    """重聚类前清掉旧的 category 表 + 解绑所有条目的 category_id。"""
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    with transaction() as tx:
        tx.execute(
            f"UPDATE {kind.table} SET category_id = NULL "
            "WHERE user_id = ? AND category_id IS NOT NULL ",
            (account_id,),
        )
        tx.execute(
            f"DELETE FROM {kind.category_table} WHERE account_id = ?",
            (account_id,),
        )


def _write_categories(
    cluster_to_keywords: dict[int, list[str]],
    centroids: dict[int, np.ndarray],
    cluster_to_ids: dict[int, list[str]],
    algo: str,
    account_id: str = "default",
    content_kind: str = "favorites",
) -> dict[int, int]:
    """
    把每个 cluster 写进对应 categories 表，把对应条目的 category_id 设上。
    返回 {original_cluster_label: new_category_id}。
    """
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    now = datetime.now(timezone.utc)
    label_to_dbid: dict[int, int] = {}

    with transaction() as tx:
        for lbl in sorted(cluster_to_keywords.keys()):
            kws = cluster_to_keywords[lbl]
            auto_name = _build_name(kws)
            members = cluster_to_ids.get(lbl, [])
            item_count = len(members)

            centroid_blob = sqlite_vec.serialize_float32(centroids[lbl].tolist())

            cur = tx.execute(
                f"""
                INSERT INTO {kind.category_table}
                    (account_id, name, auto_name, keywords_json, item_count,
                     centroid_blob, algo, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    auto_name,                # name 初始 == auto_name，未来用户改了再分叉
                    auto_name,
                    json.dumps(kws, ensure_ascii=False),
                    item_count,
                    centroid_blob,
                    algo,
                    now,
                    now,
                ),
            )
            cat_id = cur.lastrowid
            label_to_dbid[lbl] = cat_id

            # 把这个簇的所有 favorite 关联过去
            if members:
                placeholders = ",".join("?" for _ in members)
                tx.execute(
                    f"UPDATE {kind.table} SET category_id = ? WHERE user_id = ? AND id IN ({placeholders})",
                    (cat_id, account_id, *members),
                )

    return label_to_dbid


# ============================================================
# 公共入口
# ============================================================

@dataclass
class CategorizeResult:
    total_items: int
    clustered_items: int       # 进了某个簇的
    noise_items: int           # HDBSCAN 噪声 / 未分类
    n_clusters: int
    algo: str
    auto_k: Optional[int]       # KMeans 才有
    skipped_reason: Optional[str] = None


def categorize_all(
    algo: str = "hdbscan",
    force_k: Optional[int] = None,
    account_id: str = "default",
    content_kind: str = "favorites",
) -> CategorizeResult:
    """
    全量重聚类。清旧 categories 表 + 重置 favorites.category_id + 写新结果。

    algo: 'hdbscan' (默认) | 'kmeans'
    force_k: 仅 kmeans 生效；指定后跳过 silhouette 选 K
    """
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    ids, embeddings = _load_embeddings(account_id=account_id, content_kind=kind.key)
    n = len(ids)

    if n < MIN_FOR_CLUSTERING:
        return CategorizeResult(
            total_items=n,
            clustered_items=0,
            noise_items=n,
            n_clusters=0,
            algo=algo,
            auto_k=None,
            skipped_reason=f"{kind.label}数 {n} 条，少于阈值 {MIN_FOR_CLUSTERING}，不聚类",
        )

    # 跑聚类
    auto_k: Optional[int] = None
    if algo == "kmeans":
        if force_k is not None:
            from sklearn.cluster import KMeans
            km = KMeans(n_clusters=force_k, random_state=42, n_init=10)
            labels = km.fit_predict(embeddings)
            auto_k = force_k
        else:
            auto_k, labels = _run_kmeans_with_silhouette(embeddings)
    elif algo == "hdbscan":
        labels = _run_hdbscan(embeddings)
    else:
        raise ValueError(f"unknown algo: {algo}")

    # 整理 cluster -> ids
    cluster_to_ids: dict[int, list[str]] = {}
    for fid, lbl in zip(ids, labels.tolist()):
        cluster_to_ids.setdefault(int(lbl), []).append(fid)
    noise_count = len(cluster_to_ids.get(-1, []))
    real_clusters = {k: v for k, v in cluster_to_ids.items() if k >= 0}

    if not real_clusters:
        return CategorizeResult(
            total_items=n,
            clustered_items=0,
            noise_items=n,
            n_clusters=0,
            algo=algo,
            auto_k=auto_k,
            skipped_reason="所有点都被判为噪声。试试 --algo kmeans。",
        )

    logger.info(
        "Clustering done: {} clusters + {} noise points",
        len(real_clusters), noise_count,
    )

    # TF-IDF 命名
    texts_map = _load_texts_for(ids, content_kind=kind.key, account_id=account_id)
    cluster_keywords = _extract_keywords_per_cluster(
        labels, ids, texts_map,
        top_k=KEYWORDS_PER_CLUSTER,
    )

    # 中心向量
    centroids = _compute_centroids(labels, embeddings)

    # 落 db（全量重写）
    _wipe_old_categories(account_id=account_id, content_kind=kind.key)
    _write_categories(
        cluster_keywords, centroids, real_clusters,
        algo=algo, account_id=account_id, content_kind=kind.key,
    )

    return CategorizeResult(
        total_items=n,
        clustered_items=n - noise_count,
        noise_items=noise_count,
        n_clusters=len(real_clusters),
        algo=algo,
        auto_k=auto_k,
    )


def assign_one(
    favorite_id: str,
    account_id: str = "default",
    content_kind: str = "favorites",
) -> Optional[int]:
    """
    给单条条目算"最近的现有 cluster"。
    如果距离所有 centroid 都 > NEW_ITEM_FAR_THRESHOLD，归"未分类"（不动 category_id）。
    返回 category_id 或 None。
    """
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    # 读 embedding
    row = conn.execute(
        f"SELECT embedding FROM {kind.vector_table} WHERE id = ?",
        (scoped_item_id(account_id, favorite_id),),
    ).fetchone()
    if not row:
        logger.debug("assign_one: {} {} no embedding yet", kind.key, favorite_id)
        return None
    emb = np.frombuffer(row["embedding"], dtype=np.float32)

    # 读所有 category centroid
    cats = conn.execute(
        f"SELECT id, centroid_blob FROM {kind.category_table} "
        "WHERE account_id = ? AND centroid_blob IS NOT NULL",
        (account_id,),
    ).fetchall()
    if not cats:
        return None

    best_cid: Optional[int] = None
    best_dist = float("inf")
    for c in cats:
        c_emb = np.frombuffer(c["centroid_blob"], dtype=np.float32)
        d = _cosine_distance(emb, c_emb)
        if d < best_dist:
            best_dist = d
            best_cid = c["id"]

    if best_dist > NEW_ITEM_FAR_THRESHOLD:
        logger.info(
            "assign_one: {} too far from all clusters (dist={:.3f}), leaving NULL",
            favorite_id, best_dist,
        )
        return None

    conn.execute(
        f"UPDATE {kind.table} SET category_id = ? WHERE user_id = ? AND id = ?",
        (best_cid, account_id, favorite_id),
    )
    # category 行 item_count 加 1
    conn.execute(
        f"UPDATE {kind.category_table} SET item_count = item_count + 1, updated_at = ? WHERE id = ? AND account_id = ?",
        (datetime.now(timezone.utc), best_cid, account_id),
    )
    logger.debug("assigned {} -> category {} (dist={:.3f})",
                 favorite_id, best_cid, best_dist)
    return best_cid


def rename_category(
    category_id: int,
    new_name: str,
    account_id: str = "default",
    content_kind: str = "favorites",
) -> bool:
    """用户改类名。auto_name 保持不动，便于"恢复默认"。"""
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    res = conn.execute(
        f"UPDATE {kind.category_table} SET name = ?, updated_at = ? "
        "WHERE id = ? AND account_id = ?",
        (new_name, datetime.now(timezone.utc), category_id, account_id),
    )
    return res.rowcount > 0


def _refresh_counts_for_categories(
    category_ids: list[int],
    account_id: str = "default",
    content_kind: str = "favorites",
) -> None:
    if not category_ids:
        return
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    now = datetime.now(timezone.utc)
    for cid in sorted({int(c) for c in category_ids if c is not None}):
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM {kind.table}
            WHERE user_id = ? AND category_id = ? AND is_removed = 0
            """,
            (account_id, cid),
        ).fetchone()
        conn.execute(
            f"UPDATE {kind.category_table} SET item_count = ?, updated_at = ? "
            "WHERE account_id = ? AND id = ?",
            (int(row["c"] or 0), now, account_id, cid),
        )


def merge_categories(
    target_category_id: int,
    source_category_id: int,
    account_id: str = "default",
    content_kind: str = "favorites",
) -> bool:
    """Move all active and historical items from source into target, then remove source."""
    if int(target_category_id) == int(source_category_id):
        return False
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id
        FROM {kind.category_table}
        WHERE account_id = ? AND id IN (?, ?)
        """,
        (account_id, int(target_category_id), int(source_category_id)),
    ).fetchall()
    if len(rows) != 2:
        return False
    conn.execute(
        f"UPDATE {kind.table} SET category_id = ? WHERE user_id = ? AND category_id = ?",
        (int(target_category_id), account_id, int(source_category_id)),
    )
    conn.execute(
        f"DELETE FROM {kind.category_table} WHERE account_id = ? AND id = ?",
        (account_id, int(source_category_id)),
    )
    _refresh_counts_for_categories([int(target_category_id)], account_id=account_id, content_kind=kind.key)
    return True


def move_item_to_category(
    item_id: str,
    category_id: int | None,
    account_id: str = "default",
    content_kind: str = "favorites",
) -> bool:
    """Move one item into a category, or to the uncategorized bucket when category_id is None."""
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    if category_id is not None:
        cat = conn.execute(
            f"SELECT id FROM {kind.category_table} WHERE account_id = ? AND id = ?",
            (account_id, int(category_id)),
        ).fetchone()
        if cat is None:
            return False
    row = conn.execute(
        f"SELECT category_id FROM {kind.table} WHERE user_id = ? AND id = ?",
        (account_id, item_id),
    ).fetchone()
    if row is None:
        return False
    old_category_id = row["category_id"]
    conn.execute(
        f"UPDATE {kind.table} SET category_id = ? WHERE user_id = ? AND id = ?",
        (int(category_id) if category_id is not None else None, account_id, item_id),
    )
    ids_to_refresh = []
    if old_category_id is not None:
        ids_to_refresh.append(int(old_category_id))
    if category_id is not None:
        ids_to_refresh.append(int(category_id))
    _refresh_counts_for_categories(ids_to_refresh, account_id=account_id, content_kind=kind.key)
    return True


def list_categories(account_id: str = "default", content_kind: str = "favorites") -> list[dict]:
    """返回当前账号仍有未删除条目的 category（按实时活跃数量倒序）。"""
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT
            c.id,
            c.name,
            c.auto_name,
            c.keywords_json,
            COUNT(f.id) AS item_count,
            c.algo,
            c.updated_at
        FROM {kind.category_table} c
        JOIN {kind.table} f
          ON f.category_id = c.id
         AND f.user_id = c.account_id
         AND f.is_removed = 0
        WHERE c.account_id = ?
        GROUP BY
            c.id,
            c.name,
            c.auto_name,
            c.keywords_json,
            c.algo,
            c.updated_at
        ORDER BY item_count DESC, c.id ASC
        """,
        (account_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d["keywords_json"]) if d["keywords_json"] else []
        except Exception:
            d["keywords"] = []
        out.append(d)
    return out


def count_uncategorized(account_id: str = "default", content_kind: str = "favorites") -> int:
    """有多少条目没归类（HDBSCAN 噪声 / 未跑过聚类 / 距离太远）。"""
    kind = get_content_kind(content_kind)
    account_id = normalize_user_id(account_id)
    conn = get_connection()
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM {kind.table} "
        "WHERE user_id = ? AND is_removed = 0 AND category_id IS NULL",
        (account_id,),
    ).fetchone()
    return row["c"] if row else 0
