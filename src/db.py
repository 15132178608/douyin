"""
SQLite 连接管理 + schema 初始化。

设计要点：
- 单文件 db，整个工具的全部数据都在这里。
- sqlite-vec 通过 extension 加载，提供向量检索能力。
- FTS5 是 sqlite 内置虚拟表，中文要在写入端用 jieba 预切词。
- 任何外部模块拿连接都走 get_connection()，不直接 sqlite3.connect。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import sqlite_vec
from loguru import logger

from src.config import settings
from src.tenancy import DEFAULT_USER_ID


def _adapt_datetime(value: datetime) -> str:
    return value.isoformat()


def _convert_timestamp(value: bytes) -> datetime:
    text = value.decode("utf-8").strip()
    if not text:
        raise ValueError("empty TIMESTAMP value")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)


# ============================================================
# Schema
# ============================================================

SCHEMA_SQL = """
-- 私有云用户。default 用户承接本地单人模式的历史数据。
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    douyin_nickname TEXT,
    douyin_unique_id TEXT,
    douyin_sec_uid TEXT,
    douyin_avatar_url TEXT,
    douyin_profile_updated_at TIMESTAMP,
    created_at     TIMESTAMP NOT NULL,
    disabled_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code_hash          TEXT PRIMARY KEY,
    created_by_user_id TEXT,
    claimed_by_user_id TEXT,
    max_uses           INTEGER NOT NULL DEFAULT 1,
    used_count         INTEGER NOT NULL DEFAULT 0,
    expires_at         TIMESTAMP,
    created_at         TIMESTAMP NOT NULL,
    disabled_at        TIMESTAMP,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id),
    FOREIGN KEY (claimed_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    revoked_at  TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);

-- 主表：每条收藏一行
CREATE TABLE IF NOT EXISTS favorites (
    user_id          TEXT NOT NULL DEFAULT 'default',
    id               TEXT NOT NULL,
    title            TEXT,
    description      TEXT,
    author           TEXT,
    author_id        TEXT,
    video_url        TEXT,
    cover_url        TEXT,
    duration_ms      INTEGER,
    favorited_at     TIMESTAMP,
    first_seen_at    TIMESTAMP NOT NULL,
    last_seen_at     TIMESTAMP NOT NULL,
    last_recalled_at TIMESTAMP,
    user_note        TEXT,
    raw_json         TEXT,
    is_removed       INTEGER NOT NULL DEFAULT 0,
    discovery_index  INTEGER,
    video_tags       TEXT,
    llm_tags         TEXT,
    video_created_at TIMESTAMP,
    digg_count       INTEGER,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_fav_favorited_at  ON favorites(user_id, favorited_at DESC);
CREATE INDEX IF NOT EXISTS idx_fav_last_recalled ON favorites(user_id, last_recalled_at);
CREATE INDEX IF NOT EXISTS idx_fav_is_removed    ON favorites(user_id, is_removed);

-- 主表：每条点赞/喜欢一行。独立于 favorites，避免两个模块的 removed/category/note 状态互相污染。
CREATE TABLE IF NOT EXISTS likes (
    user_id          TEXT NOT NULL DEFAULT 'default',
    id               TEXT NOT NULL,
    title            TEXT,
    description      TEXT,
    author           TEXT,
    author_id        TEXT,
    video_url        TEXT,
    cover_url        TEXT,
    duration_ms      INTEGER,
    liked_at         TIMESTAMP,
    first_seen_at    TIMESTAMP NOT NULL,
    last_seen_at     TIMESTAMP NOT NULL,
    last_recalled_at TIMESTAMP,
    user_note        TEXT,
    raw_json         TEXT,
    is_removed       INTEGER NOT NULL DEFAULT 0,
    discovery_index  INTEGER,
    video_tags       TEXT,
    llm_tags         TEXT,
    video_created_at TIMESTAMP,
    digg_count       INTEGER,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_like_liked_at      ON likes(user_id, liked_at DESC);
CREATE INDEX IF NOT EXISTS idx_like_last_recalled ON likes(user_id, last_recalled_at);
CREATE INDEX IF NOT EXISTS idx_like_is_removed    ON likes(user_id, is_removed);

-- 召回日志
CREATE TABLE IF NOT EXISTS recall_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL DEFAULT 'default',
    favorite_id  TEXT NOT NULL,
    recalled_at  TIMESTAMP NOT NULL,
    channel      TEXT,
    user_action  TEXT,
    FOREIGN KEY (user_id, favorite_id) REFERENCES favorites(user_id, id)
);

CREATE INDEX IF NOT EXISTS idx_recall_favorite ON recall_log(user_id, favorite_id);
CREATE INDEX IF NOT EXISTS idx_recall_time     ON recall_log(user_id, recalled_at DESC);

-- 点赞/喜欢召回日志
CREATE TABLE IF NOT EXISTS like_recall_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL DEFAULT 'default',
    like_id     TEXT NOT NULL,
    recalled_at TIMESTAMP NOT NULL,
    channel     TEXT,
    user_action TEXT,
    FOREIGN KEY (user_id, like_id) REFERENCES likes(user_id, id)
);

CREATE INDEX IF NOT EXISTS idx_like_recall_like ON like_recall_log(user_id, like_id);
CREATE INDEX IF NOT EXISTS idx_like_recall_time ON like_recall_log(user_id, recalled_at DESC);

-- 抓取运行记录
CREATE TABLE IF NOT EXISTS crawl_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL DEFAULT 'default',
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    status        TEXT,
    new_count     INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    removed_count INTEGER DEFAULT 0,
    error_message TEXT
);

-- 点赞/喜欢抓取运行记录
CREATE TABLE IF NOT EXISTS like_crawl_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL DEFAULT 'default',
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    status        TEXT,
    new_count     INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    removed_count INTEGER DEFAULT 0,
    error_message TEXT
);

-- 取消收藏审计日志：每次从工具发起的"取消收藏"动作都记一行
CREATE TABLE IF NOT EXISTS uncollect_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL DEFAULT 'default',
    favorite_id  TEXT NOT NULL,
    initiated_at TIMESTAMP NOT NULL,
    finished_at  TIMESTAMP,
    status       TEXT NOT NULL,         -- 'pending' / 'success' / 'failed'
    channel      TEXT,                  -- 'web' / 'cli'
    error_message TEXT,
    FOREIGN KEY (user_id, favorite_id) REFERENCES favorites(user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_uncollect_favorite ON uncollect_log(user_id, favorite_id);
CREATE INDEX IF NOT EXISTS idx_uncollect_time ON uncollect_log(user_id, initiated_at DESC);

-- 取消喜欢审计日志：每次从工具发起的"取消喜欢/点赞"动作都记一行
CREATE TABLE IF NOT EXISTS unlike_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL DEFAULT 'default',
    like_id      TEXT NOT NULL,
    initiated_at TIMESTAMP NOT NULL,
    finished_at  TIMESTAMP,
    status       TEXT NOT NULL,         -- 'pending' / 'success' / 'failed'
    channel      TEXT,                  -- 'web' / 'cli'
    error_message TEXT,
    FOREIGN KEY (user_id, like_id) REFERENCES likes(user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_unlike_like ON unlike_log(user_id, like_id);
CREATE INDEX IF NOT EXISTS idx_unlike_time ON unlike_log(user_id, initiated_at DESC);

-- 后台任务队列：同步、索引、取消收藏都从请求线程移到这里执行。
CREATE TABLE IF NOT EXISTS job_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL DEFAULT 'default',
    kind           TEXT NOT NULL,
    payload_json   TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    next_run_at    TIMESTAMP,
    created_at     TIMESTAMP NOT NULL,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP,
    error_message  TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status, next_run_at, created_at);
CREATE INDEX IF NOT EXISTS idx_job_queue_user ON job_queue(user_id, created_at DESC);

-- 搜索索引 schema 被重建后，需要持久化记录待恢复的用户分区。
-- 标记只会在 vec/FTS 都重新完整写入后完成，避免升级中断造成永久空搜索。
CREATE TABLE IF NOT EXISTS search_reindex_state (
    user_id       TEXT NOT NULL,
    content_kind  TEXT NOT NULL,
    required_at   TIMESTAMP NOT NULL,
    reason        TEXT NOT NULL,
    completed_at  TIMESTAMP,
    PRIMARY KEY (user_id, content_kind),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_search_reindex_pending
    ON search_reindex_state(completed_at, required_at);

-- 登录失败限速状态。持久化到 SQLite，避免通过重启服务绕过限制。
CREATE TABLE IF NOT EXISTS login_rate_limits (
    scope             TEXT NOT NULL,
    subject_hash      TEXT NOT NULL,
    window_started_at TIMESTAMP NOT NULL,
    failed_count      INTEGER NOT NULL DEFAULT 0,
    blocked_until     TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (scope, subject_hash)
);
CREATE INDEX IF NOT EXISTS idx_login_rate_limits_updated
    ON login_rate_limits(updated_at);

-- M5 自动分类
-- account_id 列已就位，目前都填 default。详见 docs/multi-tenant-roadmap.md
CREATE TABLE IF NOT EXISTS categories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL DEFAULT 'default',
    name           TEXT NOT NULL,
    auto_name      TEXT NOT NULL,
    keywords_json  TEXT,
    item_count     INTEGER NOT NULL DEFAULT 0,
    centroid_blob  BLOB,
    algo           TEXT,
    created_at     TIMESTAMP NOT NULL,
    updated_at     TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cat_account ON categories(account_id);

-- 喜欢自动分类。结构与 categories 平行，但独立保存聚类中心和用户改名。
CREATE TABLE IF NOT EXISTS like_categories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL DEFAULT 'default',
    name           TEXT NOT NULL,
    auto_name      TEXT NOT NULL,
    keywords_json  TEXT,
    item_count     INTEGER NOT NULL DEFAULT 0,
    centroid_blob  BLOB,
    algo           TEXT,
    created_at     TIMESTAMP NOT NULL,
    updated_at     TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_like_cat_account ON like_categories(account_id);
"""

VEC_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS favorites_vec USING vec0(
    id TEXT PRIMARY KEY,
    user_id TEXT partition key,
    embedding FLOAT[1024]
);

CREATE VIRTUAL TABLE IF NOT EXISTS likes_vec USING vec0(
    id TEXT PRIMARY KEY,
    user_id TEXT partition key,
    embedding FLOAT[1024]
);
"""

FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS favorites_fts USING fts5(
    id UNINDEXED,
    user_id UNINDEXED,
    title,
    description,
    author,
    user_note,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS likes_fts USING fts5(
    id UNINDEXED,
    user_id UNINDEXED,
    title,
    description,
    author,
    user_note,
    tokenize = 'unicode61'
);
"""


# ============================================================
# 连接
# ============================================================

def _make_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        # Normal request/job code uses one connection per thread. The cross-thread
        # permission is retained so maintenance restore can close every registered
        # connection from the restore request thread after stopping job workers.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


_THREAD_CONN = threading.local()
_CONNECTION_LOCK = threading.Lock()
_OPEN_CONNECTIONS: set[sqlite3.Connection] = set()
_CONNECTION_GENERATION = 0


def _is_connection_open(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


def get_connection() -> sqlite3.Connection:
    conn = getattr(_THREAD_CONN, "conn", None)
    generation = getattr(_THREAD_CONN, "generation", None)
    if (
        conn is None
        or generation != _CONNECTION_GENERATION
        or not _is_connection_open(conn)
    ):
        with _CONNECTION_LOCK:
            conn = _make_connection(settings.db_path)
            _OPEN_CONNECTIONS.add(conn)
            _THREAD_CONN.conn = conn
            _THREAD_CONN.generation = _CONNECTION_GENERATION
        logger.debug("Opened sqlite connection at {}", settings.db_path)
    return conn


def close_connection() -> None:
    """Close all known SQLite connections before replacing the database file."""
    global _CONNECTION_GENERATION
    with _CONNECTION_LOCK:
        connections = list(_OPEN_CONNECTIONS)
        _OPEN_CONNECTIONS.clear()
        _CONNECTION_GENERATION += 1
        if hasattr(_THREAD_CONN, "conn"):
            delattr(_THREAD_CONN, "conn")
        if hasattr(_THREAD_CONN, "generation"):
            delattr(_THREAD_CONN, "generation")

    for conn in connections:
        try:
            conn.close()
        except sqlite3.Error as exc:
            logger.debug("Ignoring sqlite close error during connection reset: {}", exc)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ============================================================
# Schema 初始化
# ============================================================

def _ensure_column(table: str, column: str, decl: str) -> None:
    conn = get_connection()
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        logger.info("Migrating: ALTER TABLE {} ADD COLUMN {} {}", table, column, decl)
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _primary_key_columns(table: str) -> list[str]:
    conn = get_connection()
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    keyed = sorted((row["pk"], row["name"]) for row in rows if row["pk"])
    return [name for _pk, name in keyed]


def _table_columns(table: str) -> set[str]:
    conn = get_connection()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _create_vector_table(table: str) -> None:
    conn = get_connection()
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
            id TEXT PRIMARY KEY,
            user_id TEXT partition key,
            embedding FLOAT[1024]
        )
        """
    )


def _create_fts_table(table: str) -> None:
    conn = get_connection()
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(
            id UNINDEXED,
            user_id UNINDEXED,
            title,
            description,
            author,
            user_note,
            tokenize = 'unicode61'
        )
        """
    )


def _ensure_search_index_schema() -> set[str]:
    conn = get_connection()
    table_specs = {
        "favorites": {
            "content_table": "favorites",
            "vector_table": "favorites_vec",
            "fts_table": "favorites_fts",
        },
        "likes": {
            "content_table": "likes",
            "vector_table": "likes_vec",
            "fts_table": "likes_fts",
        },
    }
    affected: set[str] = set()
    rebuild_vectors: set[str] = set()
    rebuild_fts: set[str] = set()
    for content_kind, spec in table_specs.items():
        if "user_id" not in _table_columns(spec["vector_table"]):
            rebuild_vectors.add(content_kind)
            affected.add(content_kind)
        if "user_id" not in _table_columns(spec["fts_table"]):
            rebuild_fts.add(content_kind)
            affected.add(content_kind)

    if not affected:
        return set()

    now = datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for content_kind in sorted(affected):
            spec = table_specs[content_kind]
            if content_kind in rebuild_vectors:
                logger.info(
                    "Migrating: rebuild {} with user_id partition key",
                    spec["vector_table"],
                )
                conn.execute(f"DROP TABLE IF EXISTS {spec['vector_table']}")
                _create_vector_table(spec["vector_table"])
            if content_kind in rebuild_fts:
                logger.info(
                    "Migrating: rebuild {} with user_id column",
                    spec["fts_table"],
                )
                conn.execute(f"DROP TABLE IF EXISTS {spec['fts_table']}")
                _create_fts_table(spec["fts_table"])

            conn.execute(
                f"""
                INSERT INTO search_reindex_state (
                    user_id, content_kind, required_at, reason, completed_at
                )
                SELECT user_id, ?, ?, 'search_index_schema_rebuilt', NULL
                FROM {spec['content_table']}
                WHERE is_removed = 0
                GROUP BY user_id
                ON CONFLICT(user_id, content_kind) DO UPDATE SET
                    required_at = excluded.required_at,
                    reason = excluded.reason,
                    completed_at = NULL
                """,
                (content_kind, now),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return affected


def list_pending_search_reindexes() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT user_id, content_kind, required_at, reason
        FROM search_reindex_state
        WHERE completed_at IS NULL
        ORDER BY required_at, user_id, content_kind
        """
    ).fetchall()
    return [dict(row) for row in rows]


def search_index_counts(user_id: str, content_kind: str) -> dict[str, int]:
    from src.content.kinds import get_content_kind

    kind = get_content_kind(content_kind)
    uid = str(user_id or DEFAULT_USER_ID)
    conn = get_connection()
    active = int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM {kind.table} WHERE user_id = ? AND is_removed = 0",
            (uid,),
        ).fetchone()["c"]
        or 0
    )
    vector = int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM {kind.vector_table} WHERE user_id = ?",
            (uid,),
        ).fetchone()["c"]
        or 0
    )
    fts = int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM {kind.fts_table} WHERE user_id = ?",
            (uid,),
        ).fetchone()["c"]
        or 0
    )
    return {"active": active, "vector": vector, "fts": fts}


def complete_search_reindex(user_id: str, content_kind: str) -> bool:
    counts = search_index_counts(user_id, content_kind)
    if counts["active"] != counts["vector"] or counts["active"] != counts["fts"]:
        return False
    conn = get_connection()
    conn.execute(
        """
        UPDATE search_reindex_state
        SET completed_at = ?
        WHERE user_id = ? AND content_kind = ? AND completed_at IS NULL
        """,
        (datetime.now(timezone.utc), str(user_id or DEFAULT_USER_ID), content_kind),
    )
    return True


def _content_table_create_sql(table: str, time_column: str) -> str:
    return f"""
    CREATE TABLE {table}_tenant_migration (
        user_id          TEXT NOT NULL DEFAULT 'default',
        id               TEXT NOT NULL,
        title            TEXT,
        description      TEXT,
        author           TEXT,
        author_id        TEXT,
        video_url        TEXT,
        cover_url        TEXT,
        duration_ms      INTEGER,
        {time_column}    TIMESTAMP,
        first_seen_at    TIMESTAMP NOT NULL,
        last_seen_at     TIMESTAMP NOT NULL,
        last_recalled_at TIMESTAMP,
        user_note        TEXT,
        raw_json         TEXT,
        is_removed       INTEGER NOT NULL DEFAULT 0,
        discovery_index  INTEGER,
        video_tags       TEXT,
        llm_tags         TEXT,
        video_created_at TIMESTAMP,
        digg_count       INTEGER,
        category_id      INTEGER,
        PRIMARY KEY (user_id, id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """


def _ensure_content_table_composite_pk(table: str, time_column: str) -> None:
    if _primary_key_columns(table) == ["user_id", "id"]:
        return
    conn = get_connection()
    logger.info("Migrating: rebuild {} with PRIMARY KEY(user_id, id)", table)
    columns = [
        "user_id", "id", "title", "description", "author", "author_id",
        "video_url", "cover_url", "duration_ms", time_column,
        "first_seen_at", "last_seen_at", "last_recalled_at", "user_note",
        "raw_json", "is_removed", "discovery_index", "video_tags",
        "video_created_at", "digg_count", "category_id", "llm_tags",
    ]
    column_list = ", ".join(columns)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS {table}_tenant_migration")
        conn.execute(_content_table_create_sql(table, time_column))
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {table}_tenant_migration ({column_list})
            SELECT {column_list}
            FROM {table}
            """
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_tenant_migration RENAME TO {table}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_schema() -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (DEFAULT_USER_ID, "本地默认用户", datetime.now(timezone.utc)),
    )
    _ensure_column("users", "douyin_nickname", "TEXT")
    _ensure_column("users", "douyin_unique_id", "TEXT")
    _ensure_column("users", "douyin_sec_uid", "TEXT")
    _ensure_column("users", "douyin_avatar_url", "TEXT")
    _ensure_column("users", "douyin_profile_updated_at", "TIMESTAMP")
    _ensure_column("favorites", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("favorites", "discovery_index", "INTEGER")
    _ensure_column("favorites", "video_tags", "TEXT")
    _ensure_column("favorites", "llm_tags", "TEXT")
    _ensure_column("favorites", "video_created_at", "TIMESTAMP")
    _ensure_column("favorites", "digg_count", "INTEGER")
    _ensure_column("favorites", "category_id", "INTEGER")
    _ensure_column("likes", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("likes", "discovery_index", "INTEGER")
    _ensure_column("likes", "video_tags", "TEXT")
    _ensure_column("likes", "llm_tags", "TEXT")
    _ensure_column("likes", "video_created_at", "TIMESTAMP")
    _ensure_column("likes", "digg_count", "INTEGER")
    _ensure_column("likes", "category_id", "INTEGER")
    _ensure_content_table_composite_pk("favorites", "favorited_at")
    _ensure_content_table_composite_pk("likes", "liked_at")
    _ensure_column("recall_log", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("like_recall_log", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("crawl_runs", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("like_crawl_runs", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("uncollect_log", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("unlike_log", "user_id", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column("job_queue", "next_run_at", "TIMESTAMP")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fav_discovery ON favorites(user_id, discovery_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fav_video_created ON favorites(user_id, video_created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fav_category ON favorites(user_id, category_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_like_discovery ON likes(user_id, discovery_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_like_video_created ON likes(user_id, video_created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_like_category ON likes(user_id, category_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fav_active_order "
        "ON favorites(user_id, is_removed, COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_like_active_order "
        "ON likes(user_id, is_removed, COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fav_active_category_order "
        "ON favorites(user_id, is_removed, category_id, COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_like_active_category_order "
        "ON likes(user_id, is_removed, category_id, COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fav_active_author_order "
        "ON favorites(user_id, is_removed, author, COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_like_active_author_order "
        "ON likes(user_id, is_removed, author, COALESCE(liked_at, first_seen_at) DESC, discovery_index DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status, next_run_at, created_at)")


def init_schema() -> None:
    conn = get_connection()
    logger.info("Initializing schema at {}", settings.db_path)
    conn.executescript(SCHEMA_SQL)
    conn.executescript(VEC_SCHEMA_SQL)
    conn.executescript(FTS_SCHEMA_SQL)
    _migrate_schema()
    _ensure_search_index_schema()
    logger.info("Schema ready.")


def schema_summary() -> list[str]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','virtual') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
