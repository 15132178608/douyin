from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import db, doctor, maintenance
from src.config import settings


LOG_SPECS = {
    "recall_log": {
        "parent": "favorites",
        "item_column": "favorite_id",
        "time_column": "recalled_at",
        "columns": ("channel", "user_action"),
        "row_id": 7,
    },
    "like_recall_log": {
        "parent": "likes",
        "item_column": "like_id",
        "time_column": "recalled_at",
        "columns": ("channel", "user_action"),
        "row_id": 11,
    },
    "uncollect_log": {
        "parent": "favorites",
        "item_column": "favorite_id",
        "time_column": "initiated_at",
        "columns": ("finished_at", "status", "channel", "error_message"),
        "row_id": 15,
    },
    "unlike_log": {
        "parent": "likes",
        "item_column": "like_id",
        "time_column": "initiated_at",
        "columns": ("finished_at", "status", "channel", "error_message"),
        "row_id": 21,
    },
}


def _legacy_content_sql(
    table: str,
    time_column: str,
    *,
    composite_parent: bool,
    parent_has_user_foreign_key: bool,
) -> str:
    user_column = "user_id TEXT NOT NULL DEFAULT 'default'," if composite_parent else ""
    primary_key = "PRIMARY KEY (user_id, id)" if composite_parent else "PRIMARY KEY (id)"
    user_foreign_key = (
        ", FOREIGN KEY (user_id) REFERENCES users(id)"
        if composite_parent and parent_has_user_foreign_key
        else ""
    )
    return f"""
        CREATE TABLE {table} (
            {user_column}
            id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            author TEXT,
            author_id TEXT,
            video_url TEXT,
            cover_url TEXT,
            duration_ms INTEGER,
            {time_column} TIMESTAMP,
            first_seen_at TIMESTAMP NOT NULL,
            last_seen_at TIMESTAMP NOT NULL,
            last_recalled_at TIMESTAMP,
            user_note TEXT,
            raw_json TEXT,
            is_removed INTEGER NOT NULL DEFAULT 0,
            discovery_index INTEGER,
            video_tags TEXT,
            llm_tags TEXT,
            video_created_at TIMESTAMP,
            digg_count INTEGER,
            category_id INTEGER,
            {primary_key}
            {user_foreign_key}
        )
    """


def _legacy_log_sql(table: str, spec: dict, *, logs_have_user_id: bool) -> str:
    user_column = "user_id TEXT NOT NULL DEFAULT 'default'," if logs_have_user_id else ""
    item_column = spec["item_column"]
    parent = spec["parent"]
    if table in {"recall_log", "like_recall_log"}:
        return f"""
            CREATE TABLE {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {user_column}
                {item_column} TEXT NOT NULL,
                recalled_at TIMESTAMP NOT NULL,
                channel TEXT,
                user_action TEXT,
                FOREIGN KEY ({item_column}) REFERENCES {parent}(id)
            )
        """
    return f"""
        CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {user_column}
            {item_column} TEXT NOT NULL,
            initiated_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            status TEXT NOT NULL,
            channel TEXT,
            error_message TEXT,
            FOREIGN KEY ({item_column}) REFERENCES {parent}(id)
        )
    """


def _create_legacy_database(
    path: Path,
    *,
    composite_parent: bool,
    logs_have_user_id: bool,
    parent_has_user_foreign_key: bool = True,
) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            _legacy_content_sql(
                "favorites",
                "favorited_at",
                composite_parent=composite_parent,
                parent_has_user_foreign_key=parent_has_user_foreign_key,
            )
        )
        conn.execute(
            _legacy_content_sql(
                "likes",
                "liked_at",
                composite_parent=composite_parent,
                parent_has_user_foreign_key=parent_has_user_foreign_key,
            )
        )
        for table, spec in LOG_SPECS.items():
            conn.execute(_legacy_log_sql(table, spec, logs_have_user_id=logs_have_user_id))

        for table, time_column in (("favorites", "favorited_at"), ("likes", "liked_at")):
            user_columns = "user_id, " if composite_parent else ""
            user_values = "'default', " if composite_parent else ""
            conn.execute(
                f"""
                INSERT INTO {table} (
                    {user_columns}id, title, {time_column}, first_seen_at, last_seen_at,
                    user_note, is_removed, discovery_index
                ) VALUES (
                    {user_values}?, ?, '2026-01-01', '2026-01-01', '2026-01-02', ?, 0, 1
                )
                """,
                ("fav-1" if table == "favorites" else "like-1", f"legacy {table}", f"{table} note"),
            )

        for table, spec in LOG_SPECS.items():
            item_id = "fav-1" if spec["parent"] == "favorites" else "like-1"
            user_columns = "user_id, " if logs_have_user_id else ""
            user_values = "'default', " if logs_have_user_id else ""
            if table in {"recall_log", "like_recall_log"}:
                conn.execute(
                    f"""
                    INSERT INTO {table} (
                        id, {user_columns}{spec['item_column']}, recalled_at, channel, user_action
                    ) VALUES (?, {user_values}?, '2026-01-03', 'web', 'opened')
                    """,
                    (spec["row_id"], item_id),
                )
            else:
                conn.execute(
                    f"""
                    INSERT INTO {table} (
                        id, {user_columns}{spec['item_column']}, initiated_at, finished_at,
                        status, channel, error_message
                    ) VALUES (?, {user_values}?, '2026-01-03', '2026-01-04',
                              'success', 'web', 'legacy error text')
                    """,
                    (spec["row_id"], item_id),
                )

        conn.executescript(
            """
            CREATE INDEX idx_fav_favorited_at ON favorites(favorited_at DESC);
            CREATE INDEX idx_fav_last_recalled ON favorites(last_recalled_at);
            CREATE INDEX idx_fav_is_removed ON favorites(is_removed);
            CREATE INDEX idx_like_liked_at ON likes(liked_at DESC);
            CREATE INDEX idx_like_last_recalled ON likes(last_recalled_at);
            CREATE INDEX idx_like_is_removed ON likes(is_removed);
            CREATE INDEX idx_recall_favorite ON recall_log(favorite_id);
            CREATE INDEX idx_recall_time ON recall_log(recalled_at DESC);
            CREATE INDEX idx_like_recall_like ON like_recall_log(like_id);
            CREATE INDEX idx_like_recall_time ON like_recall_log(recalled_at DESC);
            CREATE INDEX idx_uncollect_favorite ON uncollect_log(favorite_id);
            CREATE INDEX idx_uncollect_time ON uncollect_log(initiated_at DESC);
            CREATE INDEX idx_unlike_like ON unlike_log(like_id);
            CREATE INDEX idx_unlike_time ON unlike_log(initiated_at DESC);

            CREATE TABLE job_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL,
                payload_json TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                error_message TEXT
            );
            CREATE INDEX idx_job_queue_status ON job_queue(status, created_at);
            CREATE INDEX idx_job_queue_user ON job_queue(user_id);
            INSERT INTO job_queue (
                id, user_id, kind, payload_json, status, attempts, max_attempts,
                created_at, started_at, finished_at, error_message
            ) VALUES (
                31, 'default', 'sync', '{"legacy": true}', 'success', 1, 3,
                '2026-01-05', '2026-01-05', '2026-01-05', NULL
            );
            UPDATE sqlite_sequence SET seq = 41 WHERE name = 'job_queue';
            """
        )
    finally:
        conn.close()


def _foreign_keys(
    conn: sqlite3.Connection,
    table: str,
) -> list[tuple[int, int, str, str, str]]:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return [
        (row["id"], row["seq"], row["table"], row["from"], row["to"])
        for row in sorted(rows, key=lambda row: (row["id"], row["seq"]))
    ]


def _index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall()]


def _migration_snapshot(conn: sqlite3.Connection) -> dict:
    return {
        "schema": [
            tuple(row)
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ],
        "logs": {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in LOG_SPECS
        },
        "sequences": [
            tuple(row)
            for row in conn.execute(
                "SELECT name, seq FROM sqlite_sequence "
                "WHERE name IN ('recall_log', 'like_recall_log', 'uncollect_log', 'unlike_log') "
                "ORDER BY name"
            ).fetchall()
        ],
    }


@pytest.mark.parametrize(
    ("composite_parent", "logs_have_user_id", "parent_has_user_foreign_key"),
    ((False, False, False), (True, True, True), (True, True, False)),
    ids=(
        "pre-tenant",
        "composite-parent-single-fk-logs",
        "composite-parent-missing-user-fk",
    ),
)
def test_init_schema_repairs_legacy_content_log_foreign_keys_without_data_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    composite_parent: bool,
    logs_have_user_id: bool,
    parent_has_user_foreign_key: bool,
) -> None:
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(
        db_path,
        composite_parent=composite_parent,
        logs_have_user_id=logs_have_user_id,
        parent_has_user_foreign_key=parent_has_user_foreign_key,
    )
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", db_path)
    try:
        db.init_schema()
        conn = db.get_connection()

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db._primary_key_columns("favorites") == ["user_id", "id"]
        assert db._primary_key_columns("likes") == ["user_id", "id"]
        for table in ("favorites", "likes"):
            assert _foreign_keys(conn, table) == [
                (0, 0, "users", "user_id", "id"),
            ]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%_migration'"
        ).fetchall() == []
        assert _foreign_keys(conn, "job_queue") == [
            (0, 0, "users", "user_id", "id"),
        ]
        queued = conn.execute("SELECT * FROM job_queue WHERE id = 31").fetchone()
        assert queued["user_id"] == "default"
        assert queued["kind"] == "sync"
        assert queued["payload_json"] == '{"legacy": true}'
        assert queued["status"] == "success"
        assert queued["attempts"] == 1
        assert conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'job_queue'"
        ).fetchone()["seq"] == 41

        for table, spec in LOG_SPECS.items():
            assert _foreign_keys(conn, table) == [
                (0, 0, spec["parent"], "user_id", "user_id"),
                (0, 1, spec["parent"], spec["item_column"], "id"),
            ]
            row = conn.execute(f"SELECT * FROM {table}").fetchone()
            assert row["id"] == spec["row_id"]
            assert row["user_id"] == "default"
            assert row[spec["item_column"]] == (
                "fav-1" if spec["parent"] == "favorites" else "like-1"
            )
            if table in {"recall_log", "like_recall_log"}:
                assert row["channel"] == "web"
                assert row["user_action"] == "opened"
            else:
                assert row["status"] == "success"
                assert row["error_message"] == "legacy error text"

        expected_indexes = {
            "idx_fav_favorited_at": ["user_id", "favorited_at"],
            "idx_fav_last_recalled": ["user_id", "last_recalled_at"],
            "idx_fav_is_removed": ["user_id", "is_removed"],
            "idx_like_liked_at": ["user_id", "liked_at"],
            "idx_like_last_recalled": ["user_id", "last_recalled_at"],
            "idx_like_is_removed": ["user_id", "is_removed"],
            "idx_recall_favorite": ["user_id", "favorite_id"],
            "idx_recall_time": ["user_id", "recalled_at"],
            "idx_like_recall_like": ["user_id", "like_id"],
            "idx_like_recall_time": ["user_id", "recalled_at"],
            "idx_uncollect_favorite": ["user_id", "favorite_id"],
            "idx_uncollect_time": ["user_id", "initiated_at"],
            "idx_unlike_like": ["user_id", "like_id"],
            "idx_unlike_time": ["user_id", "initiated_at"],
            "idx_job_queue_status": ["status", "next_run_at", "created_at"],
            "idx_job_queue_user": ["user_id", "created_at"],
        }
        for index_name, columns in expected_indexes.items():
            assert _index_columns(conn, index_name) == columns

        for user_id in ("alice", "bob"):
            conn.execute(
                "INSERT INTO users(id, display_name, created_at) VALUES (?, ?, '2026-02-01')",
                (user_id, user_id),
            )
            for table, item_id in (("favorites", "shared"), ("likes", "shared")):
                conn.execute(
                    f"""
                    INSERT INTO {table}(user_id, id, first_seen_at, last_seen_at)
                    VALUES (?, ?, '2026-02-01', '2026-02-01')
                    """,
                    (user_id, item_id),
                )
            conn.execute(
                "INSERT INTO recall_log(user_id, favorite_id, recalled_at) VALUES (?, 'shared', '2026-02-02')",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO like_recall_log(user_id, like_id, recalled_at) VALUES (?, 'shared', '2026-02-02')",
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO uncollect_log(user_id, favorite_id, initiated_at, status)
                VALUES (?, 'shared', '2026-02-02', 'pending')
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO unlike_log(user_id, like_id, initiated_at, status)
                VALUES (?, 'shared', '2026-02-02', 'pending')
                """,
                (user_id,),
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, spec in LOG_SPECS.items():
            assert conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] > spec["row_id"]

        before_second_init = _migration_snapshot(conn)
        db.close_connection()
        db.init_schema()
        conn = db.get_connection()
        assert _migration_snapshot(conn) == before_second_init
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close_connection()


def test_health_checks_distinguish_live_mismatch_from_migratable_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "foreign-key-mismatch.db"
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", db_path)
    try:
        db.init_schema()
        db.close_connection()
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TABLE uncollect_log")
            conn.execute(
                """
                CREATE TABLE uncollect_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    favorite_id TEXT NOT NULL,
                    initiated_at TIMESTAMP NOT NULL,
                    finished_at TIMESTAMP,
                    status TEXT NOT NULL,
                    channel TEXT,
                    error_message TEXT,
                    FOREIGN KEY (favorite_id) REFERENCES favorites(id)
                )
                """
            )
        finally:
            conn.close()

        doctor_report = doctor._database_check(db_path)
        backup_report = maintenance.validate_sqlite_backup(db_path)

        assert doctor_report["ok"] is False
        assert "外键结构" in doctor_report["message"]
        assert "foreign key mismatch" in doctor_report["details"]["foreign_key_check_error"]
        assert backup_report["ok"] is True
        assert backup_report["schema_migration_required"] is True
        assert backup_report["migration_validation"]["ok"] is True
        assert "foreign key mismatch" in backup_report["foreign_key_check_error"]
        assert backup_report["errors"] == []
    finally:
        db.close_connection()


def test_backup_validation_rejects_migratable_schema_with_orphan_log_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "foreign-key-orphan.db"
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", db_path)
    try:
        db.init_schema()
        db.close_connection()
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TABLE uncollect_log")
            conn.execute(
                """
                CREATE TABLE uncollect_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    favorite_id TEXT NOT NULL,
                    initiated_at TIMESTAMP NOT NULL,
                    finished_at TIMESTAMP,
                    status TEXT NOT NULL,
                    channel TEXT,
                    error_message TEXT,
                    FOREIGN KEY (favorite_id) REFERENCES favorites(id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO uncollect_log(
                    id, user_id, favorite_id, initiated_at, status
                ) VALUES (17, 'default', 'missing-item', '2026-02-02', 'pending')
                """
            )
        finally:
            conn.close()

        report = maintenance.validate_sqlite_backup(db_path)

        assert report["ok"] is False
        assert report["schema_migration_required"] is True
        assert report["migration_validation"]["ok"] is False
        assert any("orphan" in error for error in report["migration_validation"]["errors"])
    finally:
        db.close_connection()


def test_init_schema_rejects_job_queue_rows_for_unknown_users(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "job-queue-unknown-user.db"
    _create_legacy_database(
        db_path,
        composite_parent=True,
        logs_have_user_id=True,
    )
    source = sqlite3.connect(db_path, isolation_level=None)
    try:
        source.execute(
            """
            INSERT INTO job_queue(
                id, user_id, kind, status, attempts, max_attempts, created_at
            ) VALUES (42, 'ghost', 'sync', 'pending', 0, 3, '2026-01-06')
            """
        )
    finally:
        source.close()

    db.close_connection()
    monkeypatch.setattr(settings, "db_path", db_path)
    try:
        with pytest.raises(RuntimeError, match="unknown user"):
            db.init_schema()
    finally:
        db.close_connection()

    unchanged = sqlite3.connect(db_path)
    try:
        assert unchanged.execute(
            "SELECT user_id FROM job_queue WHERE id = 42"
        ).fetchone()[0] == "ghost"
        assert unchanged.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'job_queue_user_migration'"
        ).fetchone()[0] == 0
    finally:
        unchanged.close()


def test_init_schema_rebuilds_log_table_with_composite_and_stale_single_foreign_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "mixed-foreign-keys.db"
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", db_path)
    try:
        db.init_schema()
        db.close_connection()
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """
                INSERT INTO favorites(user_id, id, first_seen_at, last_seen_at)
                VALUES ('default', 'mixed-fk-item', '2026-02-01', '2026-02-01')
                """
            )
            conn.execute("DROP TABLE recall_log")
            conn.execute(
                """
                CREATE TABLE recall_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    favorite_id TEXT NOT NULL,
                    recalled_at TIMESTAMP NOT NULL,
                    channel TEXT,
                    user_action TEXT,
                    FOREIGN KEY (user_id, favorite_id)
                        REFERENCES favorites(user_id, id),
                    FOREIGN KEY (favorite_id) REFERENCES favorites(id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO recall_log(
                    id, user_id, favorite_id, recalled_at, channel, user_action
                ) VALUES (
                    9, 'default', 'mixed-fk-item', '2026-02-02', 'web', 'opened'
                )
                """
            )
        finally:
            conn.close()

        db.init_schema()
        conn = db.get_connection()

        assert _foreign_keys(conn, "recall_log") == [
            (0, 0, "favorites", "user_id", "user_id"),
            (0, 1, "favorites", "favorite_id", "id"),
        ]
        row = conn.execute("SELECT * FROM recall_log").fetchone()
        assert row["id"] == 9
        assert row["user_id"] == "default"
        assert row["favorite_id"] == "mixed-fk-item"
        assert row["recalled_at"].isoformat() == "2026-02-02T00:00:00"
        assert row["channel"] == "web"
        assert row["user_action"] == "opened"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close_connection()


def test_restore_migrates_legacy_backup_without_mutating_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_path = tmp_path / "legacy-backup.db"
    target_path = tmp_path / "recall.db"
    safety_dir = tmp_path / "exports"
    _create_legacy_database(
        backup_path,
        composite_parent=True,
        logs_have_user_id=True,
    )
    source_hash = __import__("hashlib").sha256(backup_path.read_bytes()).hexdigest()
    db.close_connection()
    monkeypatch.setattr(settings, "db_path", target_path)
    try:
        db.init_schema()
        db.close_connection()

        result = maintenance.restore_sqlite_backup(
            backup_path,
            db_path=target_path,
            backup_dir=safety_dir,
            close_connection=db.close_connection,
        )

        assert result.validation["ok"] is True
        assert result.validation["schema_migration_required"] is True
        assert __import__("hashlib").sha256(backup_path.read_bytes()).hexdigest() == source_hash
        conn = sqlite3.connect(target_path)
        conn.row_factory = sqlite3.Row
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            for table, spec in LOG_SPECS.items():
                row = conn.execute(f"SELECT * FROM {table}").fetchone()
                assert row["id"] == spec["row_id"]
                assert row["user_id"] == "default"
                assert row[spec["item_column"]] == (
                    "fav-1" if spec["parent"] == "favorites" else "like-1"
                )
                sequence = conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = ?",
                    (table,),
                ).fetchone()
                assert sequence is not None and sequence[0] >= spec["row_id"]
        finally:
            conn.close()

        db.init_schema()
        conn = db.get_connection()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in LOG_SPECS
        } == {table: 1 for table in LOG_SPECS}
    finally:
        db.close_connection()
