"""Shared content statistics and background-work presentation state."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from src.content.kinds import get_content_kind
from src.tenancy import DEFAULT_USER_ID, normalize_user_id
from src.web.helpers import get_connection


def kind_prefix(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return "" if kind.key == "favorites" else f"/{kind.key}"


def batch_action_url(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return "/favorites/batch/uncollect" if kind.key == "favorites" else "/likes/batch/unlike"


def batch_export_url(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return "/favorites/batch/export" if kind.key == "favorites" else "/likes/batch/export"


def empty_status_url(content_kind: str = "favorites") -> str:
    prefix = kind_prefix(content_kind)
    return f"{prefix}/empty-status" if prefix else "/empty-status"


def content_context(content_kind: str = "favorites") -> dict:
    kind = get_content_kind(content_kind)
    prefix = kind_prefix(kind.key)
    return {
        "content_kind": kind.key,
        "content_label": kind.label,
        "kind_prefix": prefix,
        "home_url": prefix or "/",
        "search_url": f"{prefix}/search",
        "categories_url": f"{prefix}/categories",
        "authors_url": f"{prefix}/authors",
        "notes_url": f"{prefix}/notes",
        "timeline_url": f"{prefix}/timeline",
        "memories_url": "/memories",
        "jobs_url": "/maintenance",
        "duplicates_url": "/duplicates",
        "batch_action_url": batch_action_url(kind.key),
        "batch_export_url": batch_export_url(kind.key),
    }


def content_stats(
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM {kind.table} WHERE user_id = ? AND is_removed=0",
        (user_id,),
    ).fetchone()["c"]
    indexed = conn.execute(
        f"SELECT COUNT(*) AS c FROM {kind.vector_table} WHERE user_id = ?",
        (user_id,),
    ).fetchone()["c"]
    return {
        "total": total,
        "indexed": indexed,
        **content_context(kind.key),
    }


def _coerce_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _elapsed_seconds(started_at, *, now: datetime | None = None) -> int:
    start = _coerce_datetime(started_at)
    if start is None:
        return 0
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - start).total_seconds()))


def _format_duration_zh(seconds: int | float | None) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return "不到 1 分钟"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    rest = minutes % 60
    if rest == 0:
        return f"{hours} 小时"
    return f"{hours} 小时 {rest} 分钟"


def _content_job_rows(content_kind: str, job_kind: str, user_id: str) -> list[dict]:
    kind = get_content_kind(content_kind)
    rows = get_connection().execute(
        """
        SELECT id, kind, payload_json, status, attempts, max_attempts,
               next_run_at, created_at, started_at, finished_at, error_message
        FROM job_queue
        WHERE user_id = ?
          AND kind = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 30
        """,
        (normalize_user_id(user_id), job_kind),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if (
            job_kind in {"index", "categorize"}
            and (payload.get("content_kind") or "favorites") != kind.key
        ):
            continue
        item["payload"] = payload
        out.append(item)
    return out


def latest_active_content_job(
    content_kind: str,
    job_kind: str,
    user_id: str,
) -> dict | None:
    for job in _content_job_rows(content_kind, job_kind, user_id):
        if job["status"] in {"pending", "running"}:
            return job
    return None


def _sync_estimate_seconds(content_kind: str, user_id: str) -> int:
    kind = get_content_kind(content_kind)
    rows = get_connection().execute(
        f"""
        SELECT started_at, finished_at
        FROM {kind.crawl_runs_table}
        WHERE user_id = ?
          AND status = 'success'
          AND started_at IS NOT NULL
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC
        LIMIT 5
        """,
        (normalize_user_id(user_id),),
    ).fetchall()
    durations: list[int] = []
    for row in rows:
        started = _coerce_datetime(row["started_at"])
        finished = _coerce_datetime(row["finished_at"])
        if started is None or finished is None:
            continue
        duration = int((finished - started).total_seconds())
        if duration > 0:
            durations.append(duration)
    if durations:
        return max(60, round(sum(durations) / len(durations)))
    return 240 if kind.key == "likes" else 120


def _sync_work_state(
    content_kind: str,
    user_id: str,
    job: dict,
    *,
    has_local_items: bool = False,
) -> dict:
    kind = get_content_kind(content_kind)
    refresh_url = empty_status_url(kind.key)
    if has_local_items:
        estimate = max(60, _sync_estimate_seconds(kind.key, user_id))
        if job["status"] == "running":
            elapsed = _elapsed_seconds(job["started_at"])
            percent = min(92, max(12, int((elapsed / estimate) * 100)))
            remaining = max(0, estimate - elapsed)
            eta_text = "快完成了" if remaining <= 20 else f"预计还需 {_format_duration_zh(remaining)}"
        else:
            percent = 5
            eta_text = "预计还需几分钟"
        return {
            "state": "syncing",
            "title": f"正在后台更新{kind.label}",
            "stage": "后台更新进行中",
            "detail": "可以继续浏览，后台会自动同步最新数据并补全搜索索引。",
            "percent": percent,
            "elapsed_text": "",
            "eta_text": eta_text,
            "refresh_url": refresh_url,
            "content_label": kind.label,
        }

    if job["status"] == "pending":
        return {
            "state": "syncing",
            "title": f"正在整理{kind.label}",
            "stage": "等待后台同步",
            "detail": "后台队列正在排队，马上会开始读取抖音数据。",
            "percent": 5,
            "elapsed_text": "",
            "eta_text": "预计还需几分钟",
            "refresh_url": refresh_url,
            "content_label": kind.label,
        }

    elapsed = _elapsed_seconds(job["started_at"])
    estimate = max(60, _sync_estimate_seconds(kind.key, user_id))
    percent = min(92, max(12, int((elapsed / estimate) * 100)))
    remaining = max(0, estimate - elapsed)
    eta_text = "快完成了" if remaining <= 20 else f"预计还需 {_format_duration_zh(remaining)}"
    return {
        "state": "syncing",
        "title": f"正在整理{kind.label}",
        "stage": "正在读取抖音数据",
        "detail": "后台同步完成后会自动出现在这里。同步阶段的进度是根据历史耗时估算的。",
        "percent": percent,
        "elapsed_text": f"已等待 {_format_duration_zh(elapsed)}",
        "eta_text": eta_text,
        "refresh_url": refresh_url,
        "content_label": kind.label,
    }


def _index_work_state(content_kind: str, user_id: str, job: dict, stats: dict) -> dict | None:
    kind = get_content_kind(content_kind)
    total = max(0, int(stats.get("total") or 0))
    indexed = max(0, min(total, int(stats.get("indexed") or 0)))
    if total <= 0 or indexed >= total:
        return None
    percent = max(3, min(99, int((indexed / total) * 100)))
    elapsed = _elapsed_seconds(job["started_at"])
    remaining_items = max(0, total - indexed)
    if indexed > 0 and elapsed > 5:
        remaining_seconds = int(remaining_items * (elapsed / indexed))
    else:
        remaining_seconds = max(30, int(remaining_items * 0.35))
    return {
        "state": "indexing",
        "title": f"正在建立{kind.label}搜索索引",
        "stage": f"{indexed} / {total} 已索引",
        "detail": "数据已经在本地，索引完成后语义搜索会更完整。",
        "percent": percent,
        "elapsed_text": f"已等待 {_format_duration_zh(elapsed)}" if elapsed else "",
        "eta_text": f"预计还需 {_format_duration_zh(remaining_seconds)}",
        "refresh_url": empty_status_url(kind.key),
        "content_label": kind.label,
    }


def content_work_state(
    content_kind: str,
    user_id: str = DEFAULT_USER_ID,
    *,
    stats: dict | None = None,
) -> dict | None:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    sync_kind = "sync_likes" if kind.key == "likes" else "sync_favorites"
    active_sync = latest_active_content_job(kind.key, sync_kind, user_id)
    active_stats = stats or content_stats(kind.key, user_id=user_id)
    if active_sync is not None:
        return _sync_work_state(
            kind.key,
            user_id,
            active_sync,
            has_local_items=int(active_stats.get("total") or 0) > 0,
        )

    active_index = latest_active_content_job(kind.key, "index", user_id)
    if active_index is None:
        return None
    return _index_work_state(kind.key, user_id, active_index, active_stats)


def _content_sync_job_state(content_kind: str, user_id: str = DEFAULT_USER_ID) -> dict:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    job_kind = "sync_likes" if kind.key == "likes" else "sync_favorites"
    rows = _content_job_rows(kind.key, job_kind, user_id)
    if any(row["status"] in {"pending", "running"} for row in rows):
        return {"state": "syncing", "error": "", "job_kind": job_kind}
    for row in rows:
        if row["status"] == "failed":
            return {
                "state": "failed",
                "error": row["error_message"] or "",
                "job_kind": job_kind,
            }
        if row["status"] == "success":
            break
    return {"state": "idle", "error": "", "job_kind": job_kind}


def empty_state_context(content_kind: str, user_id: str = DEFAULT_USER_ID) -> dict:
    kind = get_content_kind(content_kind)
    sync_state = _content_sync_job_state(kind.key, user_id=user_id)
    stats = content_stats(kind.key, user_id=user_id)
    work_state = content_work_state(kind.key, user_id=user_id, stats=stats)
    sync_url = empty_status_url(kind.key)
    maintenance_url = "/maintenance"

    if sync_state["state"] == "syncing":
        return {
            "state": "syncing",
            "title": f"正在整理{kind.label}",
            "body": f"后台同步完成后会自动出现在这里。{kind.label}较多时可能需要几分钟。",
            "content_kind": kind.key,
            "content_label": kind.label,
            "sync_url": sync_url,
            "maintenance_url": maintenance_url,
            "can_start_sync": False,
            "progress": work_state,
        }
    if sync_state["state"] == "failed":
        return {
            "state": "failed",
            "title": f"{kind.label}同步失败",
            "body": "登录态可能过期，或抖音接口暂时没有返回数据。可以重新同步；如果仍失败，到账号管理里重新扫码。",
            "error": sync_state.get("error", ""),
            "content_kind": kind.key,
            "content_label": kind.label,
            "sync_url": sync_url,
            "maintenance_url": maintenance_url,
            "can_start_sync": True,
        }
    return {
        "state": "idle",
        "title": f"还没有同步{kind.label}",
        "body": f"扫码登录后会自动同步；如果这里一直为空，可以手动发起一次{kind.label}同步。",
        "content_kind": kind.key,
        "content_label": kind.label,
        "sync_url": sync_url,
        "maintenance_url": maintenance_url,
        "can_start_sync": True,
    }


def should_show_setup_before_home(status: dict) -> bool:
    return not bool(status.get("has_profile")) or not bool(status.get("has_any_items"))
