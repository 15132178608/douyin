"""Per-item note, category, export, removal, and tracking routes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from src import jobs
from src.categorize import cluster as cluster_mod
from src.content.kinds import get_content_kind
from src.web import content_items, content_state
from src.web.helpers import current_user_id, get_connection, templates


router = APIRouter()
_uncollect_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 备注编辑（HTMX 片段）
# ---------------------------------------------------------------------------

@router.get("/favorites/{favorite_id}/note/edit", response_class=HTMLResponse)
def note_edit_form(request: Request, favorite_id: str):
    return _note_edit_form_for_kind(request, "favorites", favorite_id)


@router.get("/likes/{favorite_id}/note/edit", response_class=HTMLResponse)
def like_note_edit_form(request: Request, favorite_id: str):
    return _note_edit_form_for_kind(request, "likes", favorite_id)


def _note_edit_form_for_kind(request: Request, content_kind: str, favorite_id: str):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    item = content_items.fetch_item(favorite_id, kind.key, user_id=user_id)
    if not item:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
    return templates.TemplateResponse(request, "_note_edit.html", {"item": item})


@router.get("/favorites/{favorite_id}/note/view", response_class=HTMLResponse)
def note_view(request: Request, favorite_id: str):
    return _note_view_for_kind(request, "favorites", favorite_id)


@router.get("/likes/{favorite_id}/note/view", response_class=HTMLResponse)
def like_note_view(request: Request, favorite_id: str):
    return _note_view_for_kind(request, "likes", favorite_id)


def _note_view_for_kind(request: Request, content_kind: str, favorite_id: str):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    item = content_items.fetch_item(favorite_id, kind.key, user_id=user_id)
    if not item:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
    return templates.TemplateResponse(request, "_note_view.html", {"item": item})


@router.patch("/favorites/{favorite_id}/note", response_class=HTMLResponse)
def note_save(request: Request, favorite_id: str, note: str = Form("")):
    return _note_save_for_kind(request, "favorites", favorite_id, note)


@router.patch("/likes/{favorite_id}/note", response_class=HTMLResponse)
def like_note_save(request: Request, favorite_id: str, note: str = Form("")):
    return _note_save_for_kind(request, "likes", favorite_id, note)


def _note_save_for_kind(request: Request, content_kind: str,
                              favorite_id: str, note: str = ""):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    note = (note or "").strip()
    conn = get_connection()
    res = conn.execute(
        f"UPDATE {kind.table} SET user_note = ? WHERE user_id = ? AND id = ?",
        (note if note else None, user_id, favorite_id),
    )
    if res.rowcount == 0:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)

    # 让搜索立刻能命中新备注
    try:
        from src.embedding.indexer import index_one
        index_one(favorite_id, content_kind=kind.key, user_id=user_id)
    except Exception as e:
        # 索引失败不影响保存
        logger.warning("Re-index failed for {}: {}", favorite_id, e)

    item = content_items.fetch_item(favorite_id, kind.key, user_id=user_id)
    return templates.TemplateResponse(request, "_note_view.html", {"item": item})


@router.post("/favorites/{favorite_id}/category", response_class=HTMLResponse)
def favorite_category_save(
    request: Request,
    favorite_id: str,
    category_id: str = Form(""),
    batch_select: str = Form(""),
):
    return _category_save_for_kind(request, "favorites", favorite_id, category_id, batch_select)


@router.post("/likes/{favorite_id}/category", response_class=HTMLResponse)
def like_category_save(
    request: Request,
    favorite_id: str,
    category_id: str = Form(""),
    batch_select: str = Form(""),
):
    return _category_save_for_kind(request, "likes", favorite_id, category_id, batch_select)


def _category_save_for_kind(
    request: Request,
    content_kind: str,
    favorite_id: str,
    category_id: str,
    batch_select: str = "",
):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    target_id = None
    if str(category_id or "").strip():
        try:
            target_id = int(category_id)
        except ValueError:
            return HTMLResponse("<div class='card-error'>分类参数无效</div>", status_code=400)
    ok = cluster_mod.move_item_to_category(
        favorite_id,
        target_id,
        account_id=user_id,
        content_kind=kind.key,
    )
    if not ok:
        return HTMLResponse("<div class='card-error'>移动分类失败</div>", status_code=400)
    item = content_items.fetch_item(favorite_id, kind.key, user_id=user_id)
    if item is None:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
    return templates.TemplateResponse(
        request,
        "_card.html",
        {
            "item": item,
            "category_options": content_items.category_options(kind.key, user_id=user_id),
            "batch_action_url": content_state.batch_action_url(kind.key),
            "batch_export_url": content_state.batch_export_url(kind.key),
            "show_batch_select": str(batch_select).lower() in {"1", "true", "yes", "on"},
        },
    )


# ---------------------------------------------------------------------------
# 取消收藏（在抖音端真的取消）
# ---------------------------------------------------------------------------

@router.post("/favorites/batch/uncollect", response_class=HTMLResponse)
def batch_uncollect_favorites(request: Request, ids: list[str] = Form([])):
    return _batch_remove_for_kind(request, "favorites", ids)


@router.post("/likes/batch/unlike", response_class=HTMLResponse)
def batch_unlike_likes(request: Request, ids: list[str] = Form([])):
    return _batch_remove_for_kind(request, "likes", ids)


@router.post("/favorites/batch/export")
def batch_export_favorites(request: Request, ids: list[str] = Form([])):
    return _batch_export_for_kind(request, "favorites", ids)


@router.post("/likes/batch/export")
def batch_export_likes(request: Request, ids: list[str] = Form([])):
    return _batch_export_for_kind(request, "likes", ids)


def _batch_export_for_kind(request: Request, content_kind: str, ids: list[str]):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    unique_ids = [item_id for item_id in dict.fromkeys(ids or []) if item_id][:500]
    if not unique_ids:
        return HTMLResponse("<div class='empty'>没有选择条目。</div>", status_code=400)

    placeholders = ",".join("?" for _ in unique_ids)
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT id, title, description, author, author_id, video_url, cover_url,
               duration_ms, CAST({kind.time_column} AS TEXT) AS saved_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(last_seen_at AS TEXT) AS last_seen_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at,
               user_note, raw_json, discovery_index, video_tags, llm_tags,
               CAST(video_created_at AS TEXT) AS video_created_at,
               digg_count, category_id
        FROM {kind.table}
        WHERE user_id = ? AND is_removed = 0 AND id IN ({placeholders})
        """,
        [user_id, *unique_ids],
    ).fetchall()
    rows_by_id = {row["id"]: row for row in rows}
    items = [
        {key: row[key] for key in row.keys()}
        for item_id in unique_ids
        if (row := rows_by_id.get(item_id)) is not None
    ]
    exported_at = datetime.now(timezone.utc)
    payload = {
        "exported_at": exported_at.isoformat(),
        "content_kind": kind.key,
        "content_label": kind.label,
        "count": len(items),
        "items": items,
    }
    filename = f"douyin-{kind.key}-selected-{exported_at.strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _batch_remove_for_kind(request: Request, content_kind: str, ids: list[str]):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    unique_ids = [item_id for item_id in dict.fromkeys(ids or []) if item_id][:50]
    if not unique_ids:
        return HTMLResponse("<div class='empty'>没有选择条目。</div>", status_code=400)

    conn = get_connection()
    removed = 0
    now = datetime.now(timezone.utc)
    with _uncollect_lock:
        for item_id in unique_ids:
            row = conn.execute(
                f"SELECT id, is_removed FROM {kind.table} WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
            if row is None or row["is_removed"]:
                continue
            if kind.key == "likes":
                cur = conn.execute(
                    "INSERT INTO unlike_log (user_id, like_id, initiated_at, status, channel) "
                    "VALUES (?, ?, ?, 'pending', 'web-batch')",
                    (user_id, item_id, now),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO uncollect_log (user_id, favorite_id, initiated_at, status, channel) "
                    "VALUES (?, ?, ?, 'pending', 'web-batch')",
                    (user_id, item_id, now),
                )
            jobs.enqueue_job(
                "uncollect",
                user_id=user_id,
                payload={
                    "content_kind": kind.key,
                    "aweme_id": item_id,
                    "log_id": cur.lastrowid,
                },
            )
            conn.execute(
                f"UPDATE {kind.table} SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
                (datetime.now(timezone.utc), user_id, item_id),
            )
            removed += 1
    return HTMLResponse(f"<div class='empty'>已加入后台队列：{removed} 条{kind.label}</div>")


@router.post("/favorites/{favorite_id}/uncollect", response_class=HTMLResponse)
def uncollect_favorite(request: Request, favorite_id: str):
    """
    后台复用本地登录态调用抖音 Web 收藏接口取消收藏。
    成功返回空内容（HTMX 把卡片 remove 掉）；失败返回错误片段。
    """
    conn = get_connection()
    user_id = current_user_id(request)
    with _uncollect_lock:
        row = conn.execute(
            "SELECT id, title, is_removed FROM favorites WHERE user_id = ? AND id = ?",
            (user_id, favorite_id),
        ).fetchone()
        if row is None:
            return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
        if row["is_removed"]:
            return HTMLResponse("", status_code=200)

        now = datetime.now(timezone.utc)
        cur = conn.execute(
            "INSERT INTO uncollect_log (user_id, favorite_id, initiated_at, status, channel) "
            "VALUES (?, ?, ?, 'pending', 'web')",
            (user_id, favorite_id, now),
        )
        log_id = cur.lastrowid
        jobs.enqueue_job(
            "uncollect",
            user_id=user_id,
            payload={
                "content_kind": "favorites",
                "aweme_id": favorite_id,
                "log_id": log_id,
            },
        )
        conn.execute(
            "UPDATE favorites SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
            (datetime.now(timezone.utc), user_id, favorite_id),
        )
        return HTMLResponse("", status_code=200)


@router.post("/likes/{favorite_id}/unlike", response_class=HTMLResponse)
def unlike_like(request: Request, favorite_id: str):
    """
    后台复用本地登录态调用抖音 Web 点赞接口取消喜欢。
    成功返回空内容（HTMX 把卡片 remove 掉）；失败返回错误片段。
    """
    conn = get_connection()
    user_id = current_user_id(request)
    with _uncollect_lock:
        row = conn.execute(
            "SELECT id, title, is_removed FROM likes WHERE user_id = ? AND id = ?",
            (user_id, favorite_id),
        ).fetchone()
        if row is None:
            return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
        if row["is_removed"]:
            return HTMLResponse("", status_code=200)

        now = datetime.now(timezone.utc)
        cur = conn.execute(
            "INSERT INTO unlike_log (user_id, like_id, initiated_at, status, channel) "
            "VALUES (?, ?, ?, 'pending', 'web')",
            (user_id, favorite_id, now),
        )
        log_id = cur.lastrowid
        jobs.enqueue_job(
            "uncollect",
            user_id=user_id,
            payload={
                "content_kind": "likes",
                "aweme_id": favorite_id,
                "log_id": log_id,
            },
        )
        conn.execute(
            "UPDATE likes SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
            (datetime.now(timezone.utc), user_id, favorite_id),
        )
        return HTMLResponse("", status_code=200)


# ---------------------------------------------------------------------------
# 点击跟踪
# ---------------------------------------------------------------------------

@router.post("/track/open/{favorite_id}")
def track_open(request: Request, favorite_id: str):
    return _track_open_for_kind(request, "favorites", favorite_id)


@router.post("/likes/track/open/{favorite_id}")
def likes_track_open(request: Request, favorite_id: str):
    return _track_open_for_kind(request, "likes", favorite_id)


def _track_open_for_kind(request: Request, content_kind: str, favorite_id: str):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    now = datetime.now(timezone.utc)
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        conn.execute(
            f"UPDATE {kind.table} SET last_recalled_at = ? WHERE user_id = ? AND id = ?",
            (now, user_id, favorite_id),
        )
        if kind.key == "favorites":
            conn.execute(
                "INSERT INTO recall_log (user_id, favorite_id, recalled_at, channel, user_action) "
                "VALUES (?, ?, ?, 'search', 'opened')",
                (user_id, favorite_id, now),
            )
        else:
            conn.execute(
                "INSERT INTO like_recall_log (user_id, like_id, recalled_at, channel, user_action) "
                "VALUES (?, ?, ?, 'search', 'opened')",
                (user_id, favorite_id, now),
            )
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        logger.exception("track_open failed for {}: {}", favorite_id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})
