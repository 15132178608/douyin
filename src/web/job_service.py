"""Shared Web job orchestration without route ownership."""
from __future__ import annotations

import json

from src import jobs
from src.tenancy import normalize_user_id
from src.web.helpers import get_connection


def has_active_content_job(user_id: str, kind: str, content_kind: str) -> bool:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT payload_json
        FROM job_queue
        WHERE user_id = ?
          AND kind = ?
          AND status IN ('pending', 'running')
        """,
        (normalize_user_id(user_id), kind),
    ).fetchall()
    if kind in {"sync_favorites", "sync_likes"}:
        return bool(rows)
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if (payload.get("content_kind") or "favorites") == content_kind:
            return True
    return False


def enqueue_first_run_jobs(user_id: str) -> list[str]:
    uid = normalize_user_id(user_id)
    first_run_jobs = [
        ("sync_favorites", "favorites", {"content_kind": "favorites", "max_pages": 500}),
        ("sync_likes", "likes", {"content_kind": "likes", "max_pages": 500}),
        ("index", "favorites", {"content_kind": "favorites"}),
        ("index", "likes", {"content_kind": "likes"}),
    ]
    enqueued: list[str] = []
    for kind, content_kind, payload in first_run_jobs:
        if has_active_content_job(uid, kind, content_kind):
            continue
        jobs.enqueue_job(kind, user_id=uid, payload=payload)
        enqueued.append(kind)
    return enqueued
