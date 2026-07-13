"""Content category organization, import, merge, and rename routes."""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from src import category_import, jobs
from src.categorize import cluster as cluster_mod
from src.content.kinds import get_content_kind
from src.tenancy import normalize_user_id
from src.web import content_state, job_service
from src.web.helpers import current_user_id, get_connection, templates


router = APIRouter()


_category_import_source_tokens: dict[str, dict[str, str]] = {}
_category_import_source_lock = threading.Lock()
_MAX_CATEGORY_IMPORT_SOURCE_TOKENS = 128


def _remember_category_import_source(candidate, *, user_id: str, content_kind: str) -> dict:
    source_path = str(Path(candidate.path))
    token = hashlib.sha256(
        f"{normalize_user_id(user_id)}\0{content_kind}\0{source_path}".encode("utf-8")
    ).hexdigest()[:24]
    with _category_import_source_lock:
        _category_import_source_tokens[token] = {
            "path": source_path,
            "user_id": normalize_user_id(user_id),
            "content_kind": content_kind,
        }
        while len(_category_import_source_tokens) > _MAX_CATEGORY_IMPORT_SOURCE_TOKENS:
            _category_import_source_tokens.pop(next(iter(_category_import_source_tokens)))
    return {
        "source_token": token,
        "source_name": Path(source_path).name or "旧数据库",
        "category_count": candidate.category_count,
        "match_count": candidate.match_count,
        "source_item_count": candidate.source_item_count,
        "content_kind": candidate.content_kind,
    }


def _resolve_category_import_source(
    *,
    source_token: str | None,
    source_path: str | None,
    user_id: str,
    content_kind: str,
) -> Path | None:
    token = (source_token or "").strip()
    if token:
        with _category_import_source_lock:
            entry = _category_import_source_tokens.get(token)
        if (
            entry
            and entry.get("user_id") == normalize_user_id(user_id)
            and entry.get("content_kind") == content_kind
        ):
            return Path(entry["path"])
        return None

    raw_path = (source_path or "").strip()
    if raw_path:
        return Path(raw_path)
    return None




@router.get("/categories", response_class=HTMLResponse)
def categories_index(request: Request):
    return _categories_index_for_kind(request, "favorites")


@router.get("/likes/categories", response_class=HTMLResponse)
def likes_categories_index(request: Request):
    return _categories_index_for_kind(request, "likes")


def _enqueue_category_organize_jobs(user_id: str, content_kind: str) -> list[int]:
    kind = get_content_kind(content_kind)
    normalized_user_id = normalize_user_id(user_id)
    job_ids: list[int] = []
    if not job_service.has_active_content_job(normalized_user_id, "index", kind.key):
        job_ids.append(
            jobs.enqueue_job(
                "index",
                user_id=normalized_user_id,
                payload={"content_kind": kind.key},
            )
        )
    if not job_service.has_active_content_job(normalized_user_id, "categorize", kind.key):
        job_ids.append(
            jobs.enqueue_job(
                "categorize",
                user_id=normalized_user_id,
                payload={"content_kind": kind.key, "algo": "kmeans"},
            )
        )
    return job_ids


@router.post("/categories/organize", response_class=HTMLResponse)
def organize_categories(request: Request):
    return _organize_categories_for_kind(request, "favorites")


@router.post("/likes/categories/organize", response_class=HTMLResponse)
def likes_organize_categories(request: Request):
    return _organize_categories_for_kind(request, "likes")


@router.post("/categories/import", response_class=HTMLResponse)
def import_categories(
    request: Request,
    source_token: str = Form(""),
    source_path: str = Form(""),
):
    return _import_categories_for_kind(
        request,
        "favorites",
        source_token=source_token,
        source_path=source_path,
    )


@router.post("/likes/categories/import", response_class=HTMLResponse)
def likes_import_categories(
    request: Request,
    source_token: str = Form(""),
    source_path: str = Form(""),
):
    return _import_categories_for_kind(
        request,
        "likes",
        source_token=source_token,
        source_path=source_path,
    )


def _organize_categories_for_kind(request: Request, content_kind: str):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    job_ids = _enqueue_category_organize_jobs(user_id, kind.key)
    message = (
        "已开始在后台整理分类"
        if job_ids
        else "分类整理已经在后台进行中"
    )
    return _categories_index_for_kind(request, kind.key, category_message=message)


def _import_categories_for_kind(
    request: Request,
    content_kind: str,
    *,
    source_token: str = "",
    source_path: str = "",
):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    resolved_source = _resolve_category_import_source(
        source_token=source_token,
        source_path=source_path,
        user_id=user_id,
        content_kind=kind.key,
    )
    if resolved_source is None:
        return _categories_index_for_kind(request, kind.key, category_message="旧数据库选择已失效，请刷新页面后重试。")

    result = category_import.import_categories_from_database(
        resolved_source,
        content_kind=kind.key,
        user_id=user_id,
    )
    if result.imported:
        message = f"已导入 {result.category_count} 个分类，匹配 {result.assigned_item_count} 条{kind.label}"
    elif result.reason == "current_has_categories":
        message = "当前已经有分类，未覆盖已有整理结果。"
    elif result.reason == "no_matching_categories":
        message = "没有找到能匹配当前内容的旧分类。"
    elif result.reason == "source_missing":
        message = "旧数据库不存在，无法导入。"
    else:
        message = "旧分类导入失败。"
    return _categories_index_for_kind(request, kind.key, category_message=message)


def _categories_index_for_kind(
    request: Request,
    content_kind: str,
    *,
    category_message: str = "",
):
    """分类总览：网格列出所有类目。"""
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    cats = cluster_mod.list_categories(account_id=user_id, content_kind=kind.key)
    uncat = cluster_mod.count_uncategorized(account_id=user_id, content_kind=kind.key)
    if not category_message and (
        content_state.latest_active_content_job(kind.key, "categorize", user_id)
        or content_state.latest_active_content_job(kind.key, "index", user_id)
    ):
        category_message = "正在后台整理分类"
    category_import_candidate = None
    if not cats and uncat > 0:
        try:
            candidates = category_import.find_category_import_candidates(
                content_kind=kind.key,
                user_id=user_id,
            )
            category_import_candidate = (
                _remember_category_import_source(candidates[0], user_id=user_id, content_kind=kind.key)
                if candidates
                else None
            )
        except Exception as e:
            logger.debug("Could not discover category import candidates for {}: {}", kind.key, e)
    ctx = {
        **content_state.content_stats(kind.key, user_id=user_id),
        "page": "categories",
        "categories": cats,
        "uncategorized_count": uncat,
        "category_message": category_message,
        "category_import_candidate": category_import_candidate,
    }
    return templates.TemplateResponse(request, "categories.html", ctx)


@router.post("/categories/merge", response_class=HTMLResponse)
def merge_category(request: Request, source_id: int = Form(...), target_id: int = Form(...)):
    return _merge_category_for_kind(request, "favorites", source_id=source_id, target_id=target_id)


@router.post("/likes/categories/merge", response_class=HTMLResponse)
def likes_merge_category(request: Request, source_id: int = Form(...), target_id: int = Form(...)):
    return _merge_category_for_kind(request, "likes", source_id=source_id, target_id=target_id)


def _merge_category_for_kind(
    request: Request,
    content_kind: str,
    *,
    source_id: int,
    target_id: int,
):
    user_id = current_user_id(request)
    ok = cluster_mod.merge_categories(
        target_id,
        source_id,
        account_id=user_id,
        content_kind=content_kind,
    )
    if not ok:
        return HTMLResponse("<div class='empty'>分类合并失败。</div>", status_code=400)
    return _categories_index_for_kind(request, content_kind)


@router.get("/categories/{category_id}/name/edit", response_class=HTMLResponse)
def category_name_edit(request: Request, category_id: int):
    return _category_name_edit_for_kind(request, "favorites", category_id)


@router.get("/likes/categories/{category_id}/name/edit", response_class=HTMLResponse)
def likes_category_name_edit(request: Request, category_id: int):
    return _category_name_edit_for_kind(request, "likes", category_id)


def _category_name_edit_for_kind(request: Request, content_kind: str, category_id: int):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    conn = get_connection()
    row = conn.execute(
        f"SELECT id, name, auto_name, item_count FROM {kind.category_table} WHERE id = ? AND account_id = ?",
        (category_id, user_id),
    ).fetchone()
    if not row:
        return HTMLResponse("<div class='empty'>找不到这个分类</div>", status_code=404)
    return templates.TemplateResponse(
        request, "_cat_name_edit.html",
        {"cat": dict(row), **content_state.content_context(kind.key)},
    )


@router.get("/categories/{category_id}/name/view", response_class=HTMLResponse)
def category_name_view(request: Request, category_id: int):
    return _category_name_view_for_kind(request, "favorites", category_id)


@router.get("/likes/categories/{category_id}/name/view", response_class=HTMLResponse)
def likes_category_name_view(request: Request, category_id: int):
    return _category_name_view_for_kind(request, "likes", category_id)


def _category_name_view_for_kind(request: Request, content_kind: str, category_id: int):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    conn = get_connection()
    row = conn.execute(
        f"SELECT id, name, auto_name, item_count FROM {kind.category_table} WHERE id = ? AND account_id = ?",
        (category_id, user_id),
    ).fetchone()
    if not row:
        return HTMLResponse("<div class='empty'>找不到这个分类</div>", status_code=404)
    return templates.TemplateResponse(
        request, "_cat_name_view.html",
        {"cat": dict(row), **content_state.content_context(kind.key)},
    )

@router.patch("/categories/{category_id}/name", response_class=HTMLResponse)
def category_name_save(request: Request, category_id: int, name: str = Form("")):
    return _category_name_save_for_kind(request, "favorites", category_id, name)


@router.patch("/likes/categories/{category_id}/name", response_class=HTMLResponse)
def likes_category_name_save(request: Request, category_id: int, name: str = Form("")):
    return _category_name_save_for_kind(request, "likes", category_id, name)


def _category_name_save_for_kind(request: Request, content_kind: str,
                                       category_id: int, name: str = Form("")):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    ok = cluster_mod.rename_category(category_id, name, account_id=user_id, content_kind=kind.key)
    if not ok:
        return HTMLResponse("<div class='empty'>保存失败（名字不能为空）</div>", status_code=400)
    conn = get_connection()
    row = conn.execute(
        f"SELECT id, name, auto_name, item_count FROM {kind.category_table} WHERE id = ? AND account_id = ?",
        (category_id, user_id),
    ).fetchone()
    return templates.TemplateResponse(
        request, "_cat_name_view.html",
        {"cat": dict(row), **content_state.content_context(kind.key)},
    )
