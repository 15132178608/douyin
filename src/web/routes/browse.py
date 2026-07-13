"""Content browsing, search, timeline, and discovery routes."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src import onboarding
from src.content.kinds import get_content_kind
from src.tenancy import DEFAULT_USER_ID, normalize_user_id
from src.web import content_items, content_state
from src.web.authors import cached_author_avatar_url_from_raw_json
from src.web.helpers import current_user_id, get_connection, templates


router = APIRouter()
HOME_PAGE_SIZE = 32
MIN_HOME_PAGE_SIZE = 8
MAX_HOME_PAGE_SIZE = 80
PAGINATION_WINDOW_SIZE = 7


@router.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request):
    user_id = current_user_id(request)
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            f.id,
            COALESCE(f.title, l.title) AS title,
            COALESCE(f.author, l.author) AS author,
            COALESCE(f.video_url, l.video_url) AS video_url,
            COALESCE(f.cover_url, l.cover_url) AS cover_url,
            f.user_note,
            COALESCE(f.raw_json, l.raw_json) AS raw_json,
            f.category_id,
            CAST(f.favorited_at AS TEXT) AS favorited_at,
            CAST(f.first_seen_at AS TEXT) AS first_seen_at,
            CAST(f.video_created_at AS TEXT) AS video_created_at,
            CAST(f.last_recalled_at AS TEXT) AS last_recalled_at
        FROM favorites f
        JOIN likes l
          ON l.user_id = f.user_id
         AND l.id = f.id
         AND l.is_removed = 0
        WHERE f.user_id = ?
          AND f.is_removed = 0
        ORDER BY COALESCE(f.favorited_at, f.first_seen_at) DESC
        LIMIT 200
        """,
        (user_id,),
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "duplicates.html",
        {
            "request": request,
            "page": "duplicates",
            "items": [content_items.row_to_item(r, "favorites") for r in rows],
            "category_options": content_items.category_options("favorites", user_id=user_id),
            **content_state.content_stats("favorites", user_id=user_id),
        },
    )


def _candidate_to_item(candidate, content_kind: str = "favorites") -> dict:
    kind = get_content_kind(content_kind)
    prefix = "/favorites" if kind.key == "favorites" else f"/{kind.key}"
    return {
        "id": candidate.id,
        "title": candidate.title,
        "author": candidate.author,
        "author_avatar_url": None,
        "video_url": candidate.video_url,
        "cover_url": candidate.cover_url,
        "user_note": candidate.user_note,
        "category_id": None,
        "favorited_at": str(candidate.favorited_at) if candidate.favorited_at else None,
        "first_seen_at": str(candidate.first_seen_at) if candidate.first_seen_at else None,
        "video_created_at": str(candidate.video_created_at) if candidate.video_created_at else None,
        "last_recalled_at": str(candidate.last_recalled_at) if candidate.last_recalled_at else None,
        "content_kind": kind.key,
        "content_label": kind.label,
        "note_url_prefix": prefix,
        "track_url": f"{content_state.kind_prefix(kind.key)}/track/open/{candidate.id}",
    }


@router.get("/memories", response_class=HTMLResponse)
def memories_page(request: Request):
    from src.recall import selector

    user_id = current_user_id(request)
    anniversaries = selector.pick_anniversary(limit=3, seed=1, user_id=user_id)
    ann_ids = {c.id for c in anniversaries}
    milestones = selector.pick_milestone(limit=3, exclude_ids=ann_ids, seed=2, user_id=user_id)
    return templates.TemplateResponse(
        request,
        "memories.html",
        {
            "request": request,
            "page": "memories",
            "anniversaries": [_candidate_to_item(c) for c in anniversaries],
            "milestones": [_candidate_to_item(c) for c in milestones],
            **content_state.content_stats("favorites", user_id=user_id),
            "category_options": content_items.category_options("favorites", user_id=user_id),
            "batch_action_url": content_state.batch_action_url("favorites"),
            "batch_export_url": content_state.batch_export_url("favorites"),
        },
    )


def _parse_category_filter(category: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """
    把 URL ?category=... 参数解析成 (clause, param)。
    支持的取值：
      - None / ''         → ('','')
      - 'uncategorized'   → ('AND category_id IS NULL', None)
      - '<int>'           → ('AND category_id = ?', int)
    返回 (extra_where, param)。extra_where 可能为空串，param 可能为 None。
    """
    if not category:
        return "", None
    if category == "uncategorized":
        return "AND category_id IS NULL", None
    try:
        cid = int(category)
        return "AND category_id = ?", cid
    except ValueError:
        return "", None


def _parse_author_filter(author: Optional[str]) -> tuple[str, Optional[str]]:
    """把 URL ?author=... 解析成 SQL clause + 参数。"""
    if not author:
        return "", None
    return "AND author = ?", author


def _normalize_page_size(page_size: int | None = None) -> int:
    try:
        size = int(page_size or HOME_PAGE_SIZE)
    except (TypeError, ValueError):
        size = HOME_PAGE_SIZE
    return max(MIN_HOME_PAGE_SIZE, min(size, MAX_HOME_PAGE_SIZE))


def _recent_favorites(limit: int = HOME_PAGE_SIZE, category: Optional[str] = None,
                      author: Optional[str] = None,
                      offset: int = 0,
                      user_id: str = DEFAULT_USER_ID,
                      content_kind: str = "favorites") -> list[dict]:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    cat_where, cat_param = _parse_category_filter(category)
    auth_where, auth_param = _parse_author_filter(author)
    params: list = [user_id]
    if cat_param is not None:
        params.append(cat_param)
    if auth_param is not None:
        params.append(auth_param)
    params.extend([limit, offset])

    rows = conn.execute(
        f"""
        SELECT id, title, author, video_url, cover_url, user_note,
               raw_json, category_id,
               CAST({kind.time_column} AS TEXT) AS favorited_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(video_created_at AS TEXT) AS video_created_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at
        FROM {kind.table}
        WHERE user_id = ? AND is_removed = 0 {cat_where} {auth_where}
        ORDER BY COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [content_items.row_to_item(r, kind.key) for r in rows]


def _home_page_load_url(
    content_kind: str,
    offset: int,
    page_size: int,
    category: Optional[str] = None,
    author: Optional[str] = None,
) -> str:
    kind = get_content_kind(content_kind)
    path = f"{content_state.kind_prefix(kind.key)}/page" if kind.key != "favorites" else "/page"
    params: dict[str, object] = {"offset": offset, "page_size": page_size}
    if category:
        params["category"] = category
    if author:
        params["author"] = author
    return f"{path}?{urlencode(params)}"


def _home_page_url(
    content_kind: str,
    page_number: int,
    page_size: int,
    category: Optional[str] = None,
    author: Optional[str] = None,
) -> str:
    kind = get_content_kind(content_kind)
    path = content_state.kind_prefix(kind.key) or "/"
    params: dict[str, object] = {"p": page_number, "page_size": page_size}
    if category:
        params["category"] = category
    if author:
        params["author"] = author
    return f"{path}?{urlencode(params)}"


def _pagination_items(
    content_kind: str,
    page_number: int,
    total_pages: int,
    page_size: int,
    category: Optional[str] = None,
    author: Optional[str] = None,
) -> list[dict]:
    safe_total = max(1, int(total_pages or 1))
    safe_page = max(1, min(int(page_number or 1), safe_total))
    window_size = min(PAGINATION_WINDOW_SIZE, safe_total)
    start_page = max(1, safe_page - window_size // 2)
    end_page = start_page + window_size - 1
    if end_page > safe_total:
        end_page = safe_total
        start_page = max(1, end_page - window_size + 1)

    page_numbers = sorted({1, safe_total, *range(start_page, end_page + 1)})
    items: list[dict] = []
    previous_page = 0
    for number in page_numbers:
        if number - previous_page > 1:
            items.append({"type": "ellipsis"})
        items.append(
            {
                "type": "page",
                "number": number,
                "url": _home_page_url(
                    content_kind,
                    number,
                    page_size=page_size,
                    category=category,
                    author=author,
                ),
                "is_current": number == safe_page,
            }
        )
        previous_page = number
    return items


def _count_favorites(
    content_kind: str,
    category: Optional[str] = None,
    author: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
) -> int:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    cat_where, cat_param = _parse_category_filter(category)
    auth_where, auth_param = _parse_author_filter(author)
    params: list = [user_id]
    if cat_param is not None:
        params.append(cat_param)
    if auth_param is not None:
        params.append(auth_param)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM {kind.table}
        WHERE user_id = ? AND is_removed = 0 {cat_where} {auth_where}
        """,
        params,
    ).fetchone()
    return int(row["c"] or 0)


def _favorite_page(
    content_kind: str,
    page_number: int = 1,
    page_size: int | None = None,
    category: Optional[str] = None,
    author: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    normalized_page_size = _normalize_page_size(page_size)
    total_count = _count_favorites(kind.key, category=category, author=author, user_id=user_id)
    total_pages = max(1, (total_count + normalized_page_size - 1) // normalized_page_size)
    safe_page = max(1, min(int(page_number or 1), total_pages))
    safe_offset = (safe_page - 1) * normalized_page_size
    rows = _recent_favorites(
        normalized_page_size + 1,
        category=category,
        author=author,
        offset=safe_offset,
        user_id=user_id,
        content_kind=kind.key,
    )
    items = rows[:normalized_page_size]
    has_more = safe_page < total_pages
    next_offset = safe_offset + len(items)
    return {
        "items": items,
        "has_more": has_more,
        "next_offset": next_offset,
        "page_size": normalized_page_size,
        "page_number": safe_page,
        "total_pages": total_pages,
        "total_count": total_count,
        "prev_page_url": (
            _home_page_url(
                kind.key,
                safe_page - 1,
                page_size=normalized_page_size,
                category=category,
                author=author,
            )
            if safe_page > 1
            else None
        ),
        "next_page_url": (
            _home_page_url(
                kind.key,
                safe_page + 1,
                page_size=normalized_page_size,
                category=category,
                author=author,
            )
            if has_more
            else None
        ),
        "first_page_url": _home_page_url(
            kind.key,
            1,
            page_size=normalized_page_size,
            category=category,
            author=author,
        ),
        "last_page_url": _home_page_url(
            kind.key,
            total_pages,
            page_size=normalized_page_size,
            category=category,
            author=author,
        ),
        "pagination_items": _pagination_items(
            kind.key,
            safe_page,
            total_pages,
            page_size=normalized_page_size,
            category=category,
            author=author,
        ),
        "load_more_url": (
            _home_page_load_url(
                kind.key,
                next_offset,
                page_size=normalized_page_size,
                category=category,
                author=author,
            )
            if has_more
            else None
        ),
        "category_options": content_items.category_options(kind.key, user_id=user_id),
        "batch_action_url": content_state.batch_action_url(kind.key),
        "batch_export_url": content_state.batch_export_url(kind.key),
    }


def _favorite_page_from_offset(
    content_kind: str,
    offset: int = 0,
    page_size: int | None = None,
    category: Optional[str] = None,
    author: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    safe_offset = max(0, int(offset or 0))
    normalized_page_size = _normalize_page_size(page_size)
    rows = _recent_favorites(
        normalized_page_size + 1,
        category=category,
        author=author,
        offset=safe_offset,
        user_id=user_id,
        content_kind=kind.key,
    )
    items = rows[:normalized_page_size]
    has_more = len(rows) > normalized_page_size
    next_offset = safe_offset + len(items)
    return {
        "items": items,
        "has_more": has_more,
        "next_offset": next_offset,
        "page_size": normalized_page_size,
        "load_more_url": (
            _home_page_load_url(
                kind.key,
                next_offset,
                page_size=normalized_page_size,
                category=category,
                author=author,
            )
            if has_more
            else None
        ),
        "category_options": content_items.category_options(kind.key, user_id=user_id),
        "batch_action_url": content_state.batch_action_url(kind.key),
        "batch_export_url": content_state.batch_export_url(kind.key),
    }


def _category_label(
    category: Optional[str],
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> Optional[str]:
    """根据 URL 参数返回当前过滤的"类别名"，用于页面顶部小标签。"""
    kind = get_content_kind(content_kind)
    if not category:
        return None
    if category == "uncategorized":
        return "未分类"
    try:
        cid = int(category)
        conn = get_connection()
        row = conn.execute(
            f"SELECT name FROM {kind.category_table} WHERE id = ? AND account_id = ?",
            (cid, normalize_user_id(user_id)),
        ).fetchone()
        return row["name"] if row else f"分类 #{cid}"
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 首页 + 搜索
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def index(request: Request, category: Optional[str] = None,
                author: Optional[str] = None, p: int = 1,
                page_size: int = HOME_PAGE_SIZE):
    return _index_for_kind(
        request,
        "favorites",
        category=category,
        author=author,
        p=p,
        page_size=page_size,
    )


@router.get("/likes", response_class=HTMLResponse)
def likes_index(request: Request, category: Optional[str] = None,
                      author: Optional[str] = None, p: int = 1,
                      page_size: int = HOME_PAGE_SIZE):
    return _index_for_kind(
        request,
        "likes",
        category=category,
        author=author,
        p=p,
        page_size=page_size,
    )


def _index_for_kind(request: Request, content_kind: str,
                          category: Optional[str] = None,
                          author: Optional[str] = None,
                          p: int = 1,
                          page_size: int = HOME_PAGE_SIZE):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    onboarding_status = onboarding.get_onboarding_status(user_id)
    if content_state.should_show_setup_before_home(onboarding_status):
        return RedirectResponse("/setup", status_code=303)
    cat_label = _category_label(category, kind.key, user_id=user_id)
    page_data = _favorite_page(
        kind.key,
        page_number=p,
        page_size=page_size,
        category=category,
        author=author,
        user_id=user_id,
    )
    items = page_data["items"]
    if author and not items:
        empty_msg = f"作者「{author}」下没有条目"
    elif cat_label and not items:
        empty_msg = f"分类「{cat_label}」下没有条目"
    elif not items:
        empty_msg = ""
    else:
        empty_msg = ""
    stats = content_state.content_stats(kind.key, user_id=user_id)
    ctx = {
        **stats,
        "page": "home",
        **page_data,
        "empty_msg": empty_msg,
        "empty_state": (
            None
            if author or cat_label or items
            else content_state.empty_state_context(kind.key, user_id=user_id)
        ),
        "work_state": None if not items else content_state.content_work_state(kind.key, user_id=user_id, stats=stats),
        "current_category": category,
        "current_category_label": cat_label,
        "current_author": author,
        "current_author_label": f"@{author}" if author else None,
        "onboarding_status": onboarding_status,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


def _empty_status_for_kind(request: Request, content_kind: str):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    page_data = _favorite_page(kind.key, user_id=user_id)
    stats = content_state.content_stats(kind.key, user_id=user_id)
    ctx = {
        **stats,
        "page": "home",
        **page_data,
        "empty_msg": "",
        "empty_state": None if page_data["items"] else content_state.empty_state_context(kind.key, user_id=user_id),
        "work_state": (
            content_state.content_work_state(kind.key, user_id=user_id, stats=stats)
            if page_data["items"] else None
        ),
        "current_category": None,
        "current_category_label": None,
        "current_author": None,
        "current_author_label": None,
        "onboarding_status": onboarding.get_onboarding_status(user_id),
    }
    return templates.TemplateResponse(request, "_grid.html", ctx)


@router.get("/empty-status", response_class=HTMLResponse)
def favorites_empty_status(request: Request):
    return _empty_status_for_kind(request, "favorites")


@router.get("/likes/empty-status", response_class=HTMLResponse)
def likes_empty_status(request: Request):
    return _empty_status_for_kind(request, "likes")


@router.get("/page", response_class=HTMLResponse)
def index_page(request: Request, offset: int = 0,
                     page_size: int = HOME_PAGE_SIZE,
                     category: Optional[str] = None,
                     author: Optional[str] = None):
    return _index_page_for_kind(
        request,
        "favorites",
        offset=offset,
        page_size=page_size,
        category=category,
        author=author,
    )


@router.get("/likes/page", response_class=HTMLResponse)
def likes_index_page(request: Request, offset: int = 0,
                           page_size: int = HOME_PAGE_SIZE,
                           category: Optional[str] = None,
                           author: Optional[str] = None):
    return _index_page_for_kind(
        request,
        "likes",
        offset=offset,
        page_size=page_size,
        category=category,
        author=author,
    )


def _index_page_for_kind(request: Request, content_kind: str,
                               offset: int = 0,
                               page_size: int = HOME_PAGE_SIZE,
                               category: Optional[str] = None,
                               author: Optional[str] = None):
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    page_data = _favorite_page_from_offset(
        kind.key,
        offset=offset,
        page_size=page_size,
        category=category,
        author=author,
        user_id=user_id,
    )
    return templates.TemplateResponse(
        request,
        "_grid_page.html",
        {
            **content_state.content_context(kind.key),
            **page_data,
        },
    )


@router.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", category: Optional[str] = None):
    return _search_for_kind(request, "favorites", q=q, category=category)


@router.get("/likes/search", response_class=HTMLResponse)
def likes_search(request: Request, q: str = "", category: Optional[str] = None):
    return _search_for_kind(request, "likes", q=q, category=category)


def _search_for_kind(request: Request, content_kind: str, q: str = "",
                           category: Optional[str] = None):
    """HTMX 调用，返回 _grid.html 片段。"""
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    q = (q or "").strip()
    cat_where, cat_param = _parse_category_filter(category)

    if not q:
        page_data = _favorite_page(kind.key, category=category, user_id=user_id)
        items = page_data["items"]
        if category:
            cat_label = _category_label(category, kind.key, user_id=user_id) or category
            empty_msg = f"分类「{cat_label}」下还没东西"
        else:
            empty_msg = "输入关键词搜搜看"
    else:
        from src.search.hybrid import search_for_kind as do_search
        hits = do_search(q, top_k=24, content_kind=kind.key, user_id=user_id)
        # 如果带了 category，二次过滤 hits（让分类页里的搜索只看这一类）
        if category:
            allowed_ids = set()
            conn = get_connection()
            params = [cat_param] if cat_param is not None else []
            rows = conn.execute(
                f"SELECT id FROM {kind.table} WHERE user_id = ? AND is_removed = 0 {cat_where}",
                [user_id, *params],
            ).fetchall()
            allowed_ids = {r["id"] for r in rows}
            hits = [h for h in hits if h.id in allowed_ids]

        items = []
        for h in hits:
            d = content_items.row_to_item({
                "id": h.id, "title": h.title, "author": h.author,
                "video_url": h.video_url, "cover_url": h.cover_url,
                "user_note": h.user_note,
                "raw_json": h.raw_json,
                "category_id": None,
                "favorited_at": h.favorited_at,
                "first_seen_at": h.first_seen_at,
                "video_created_at": h.video_created_at,
                "last_recalled_at": h.last_recalled_at,
            }, kind.key)
            d["score"] = h.score
            d["vec_rank"] = h.vec_rank
            d["fts_rank"] = h.fts_rank
            items.append(d)
        empty_msg = f'"{q}" 没找到相关{kind.label}。'

    return templates.TemplateResponse(
        request,
        "_grid.html",
        {
            "items": items,
            "empty_msg": empty_msg,
            "category_options": content_items.category_options(kind.key, user_id=user_id),
            "batch_action_url": content_state.batch_action_url(kind.key),
            "batch_export_url": content_state.batch_export_url(kind.key),
            **(page_data if not q else {}),
        },
    )


# ---------------------------------------------------------------------------
# 时间轴
# ---------------------------------------------------------------------------

@router.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request, category: Optional[str] = None):
    return _timeline_for_kind(request, "favorites", category=category)


@router.get("/likes/timeline", response_class=HTMLResponse)
def likes_timeline(request: Request, category: Optional[str] = None):
    return _timeline_for_kind(request, "likes", category=category)


def _timeline_for_kind(request: Request, content_kind: str,
                             category: Optional[str] = None):
    """
    按年-月分组：
    - 有 favorited_at 的：按真实时间分组
    - 没有的（首批全量抓取）：单独分组在最后，按 discovery_index 倒序

    支持 ?category=<id> 或 ?category=uncategorized 过滤。
    """
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    conn = get_connection()
    cat_where, cat_param = _parse_category_filter(category)
    params = [user_id]
    if cat_param is not None:
        params.append(cat_param)

    rows = conn.execute(
        f"""
        SELECT id, title, author, video_url, cover_url, user_note,
               raw_json,
               CAST({kind.time_column} AS TEXT) AS favorited_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(video_created_at AS TEXT) AS video_created_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at,
               discovery_index
        FROM {kind.table}
        WHERE user_id = ? AND is_removed = 0 {cat_where}
        ORDER BY COALESCE(favorited_at, '1970-01-01') DESC, discovery_index DESC
        """,
        params,
    ).fetchall()

    # 分组
    sections: list[dict] = []
    current_key: Optional[str] = None
    current_section: Optional[dict] = None
    unknown_items: list[dict] = []

    for r in rows:
        item = content_items.row_to_item(r, kind.key)
        fav_at = r["favorited_at"]
        if fav_at is None:
            unknown_items.append(item)
            continue

        if isinstance(fav_at, datetime):
            dt = fav_at
        else:
            try:
                dt = datetime.fromisoformat(str(fav_at).replace(" ", "T"))
            except Exception:
                unknown_items.append(item)
                continue

        key = f"{dt.year}-{dt.month:02d}"
        if key != current_key:
            current_section = {
                "key": key,
                "label": f"{dt.year} 年 {dt.month} 月",
                "items": [],
            }
            sections.append(current_section)
            current_key = key
        current_section["items"].append(item)

    if unknown_items:
        sections.append({
            "key": "unknown",
            "label": "时间未知（首次抓取，按发现顺序）",
            "items": unknown_items,
        })

    ctx = {
        **content_state.content_stats(kind.key, user_id=user_id),
        "page": "timeline",
        "sections": sections,
        "has_unknown": bool(unknown_items),
        "unknown_count": len(unknown_items),
        "current_category": category,
        "current_category_label": _category_label(category, kind.key, user_id=user_id),
    }
    return templates.TemplateResponse(request, "timeline.html", ctx)


# ---------------------------------------------------------------------------
# 按作者
# ---------------------------------------------------------------------------

@router.get("/authors", response_class=HTMLResponse)
def authors_index(request: Request):
    return _authors_index_for_kind(request, "favorites")


@router.get("/likes/authors", response_class=HTMLResponse)
def likes_authors_index(request: Request):
    return _authors_index_for_kind(request, "likes")


def _authors_index_for_kind(request: Request, content_kind: str):
    """列出所有作者 + 各自条目数。"""
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
    conn = get_connection()
    rows = conn.execute(
        f"""
        WITH active AS (
            SELECT
                author,
                raw_json,
                COUNT(*) OVER (PARTITION BY author) AS count,
                ROW_NUMBER() OVER (
                    PARTITION BY author
                    ORDER BY COALESCE({kind.time_column}, first_seen_at) DESC, discovery_index DESC
                ) AS rn
            FROM {kind.table}
            WHERE user_id = ? AND is_removed = 0 AND author IS NOT NULL AND TRIM(author) <> ''
        )
        SELECT author, count, raw_json
        FROM active
        WHERE rn = 1
        ORDER BY count DESC, author ASC
        """,
        (user_id,),
    ).fetchall()
    authors = []
    for r in rows:
        author = dict(r)
        raw_json = author.pop("raw_json", None)
        author["avatar_url"] = cached_author_avatar_url_from_raw_json(raw_json)
        authors.append(author)
    return templates.TemplateResponse(request, "authors.html", {
        **content_state.content_stats(kind.key, user_id=user_id),
        "page": "authors",
        "authors": authors,
    })


# ---------------------------------------------------------------------------
# 有备注（你显式标记过的）
# ---------------------------------------------------------------------------

@router.get("/notes", response_class=HTMLResponse)
def notes_index(request: Request):
    return _notes_index_for_kind(request, "favorites")


@router.get("/likes/notes", response_class=HTMLResponse)
def likes_notes_index(request: Request):
    return _notes_index_for_kind(request, "likes")


def _notes_index_for_kind(request: Request, content_kind: str):
    """列出所有你写过备注的条目。"""
    kind = get_content_kind(content_kind)
    user_id = current_user_id(request)
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
        WHERE user_id = ? AND is_removed = 0
          AND user_note IS NOT NULL
          AND TRIM(user_note) <> ''
        ORDER BY COALESCE(favorited_at, first_seen_at) DESC, discovery_index DESC
        """,
        (user_id,),
    ).fetchall()
    items = [content_items.row_to_item(r, kind.key) for r in rows]
    return templates.TemplateResponse(request, "notes.html", {
        **content_state.content_stats(kind.key, user_id=user_id),
        "page": "notes",
        "items": items,
        "empty_msg": "还没写过备注。在卡片下方点「添加备注」即可记录原因。",
    })
