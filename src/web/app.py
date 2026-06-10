"""
本地 Web UI：FastAPI + HTMX + Jinja2。

路由：
- GET /                              首页（搜索框 + 最近收藏 20 条）
- GET /search?q=...                  HTMX 局部刷新搜索结果
- GET /timeline?category=ID          按年-月分组的时间轴（可按分类筛）
- GET /categories                    自动分类总览（M5）
- GET /categories/{id}/name/edit     返回分类名编辑表单片段
- GET /categories/{id}/name/view     返回分类名展示片段
- PATCH /categories/{id}/name        保存新分类名
- GET /favorites/{id}/note/edit      返回备注编辑表单片段
- GET /favorites/{id}/note/view      返回备注展示片段（取消编辑用）
- PATCH /favorites/{id}/note         保存备注 + 单条重索引
- GET /authors                       按作者列表（每个作者多少条收藏）
- GET /notes                         所有写过备注的条目
- POST /favorites/{id}/uncollect     从抖音取消收藏（走 CDP）
- POST /likes/{id}/unlike            从抖音取消喜欢/点赞
- POST /track/open/{id}              点开视频时上报
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
from typing import Optional
from urllib.parse import quote, unquote, urlencode, urlparse

import json

import httpx

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from src.categorize import cluster as cluster_mod
from src.content.kinds import get_content_kind
from src.config import settings
from src.db import get_connection, init_schema
from src import accounts
from src import jobs
from src import onboarding
from src.tenancy import DEFAULT_USER_ID, normalize_user_id
from src.web.authors import cached_author_avatar_url_from_raw_json
from src.uncollector.douyin import PersistentUncollectWorker


# 启动时确保 schema OK
init_schema()


_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

app = FastAPI(title="douyin-recall")
_uncollect_lock = asyncio.Lock()
_uncollect_worker = PersistentUncollectWorker()
_user_uncollect_workers: dict[str, PersistentUncollectWorker] = {}
_user_uncollect_workers_lock = threading.Lock()
_job_worker_stop = threading.Event()
_job_worker_thread: threading.Thread | None = None
HOME_PAGE_SIZE = 32
MIN_HOME_PAGE_SIZE = 8
MAX_HOME_PAGE_SIZE = 80
PAGINATION_WINDOW_SIZE = 7
_AUTH_PUBLIC_PATHS = {"/login", "/logout"}

# 每用户抖音扫码绑定状态：{user_id: {status, message, qr_path}}
# status: idle | starting | qr_ready | success | failed
_douyin_auth_sessions: dict[str, dict] = {}
_douyin_auth_lock = threading.Lock()


@app.middleware("http")
async def _attach_current_user(request: Request, call_next):
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS:
        return await call_next(request)

    token = request.cookies.get(settings.session_cookie_name)
    user = accounts.user_from_session(token)
    if user is None and settings.web_auth_required:
        next_url = quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={next_url}", status_code=303)

    if user is None:
        user = accounts.ensure_default_user()
    request.state.current_user = user
    request.state.user_id = normalize_user_id(user["id"])
    return await call_next(request)


@app.on_event("startup")
def _start_background_workers() -> None:
    global _job_worker_thread
    if _job_worker_thread is None or not _job_worker_thread.is_alive():
        _job_worker_stop.clear()
        _job_worker_thread = threading.Thread(
            target=jobs.run_forever,
            kwargs={"stop_event": _job_worker_stop, "poll_interval": 1.0},
            name="recall-job-worker",
            daemon=True,
        )
        _job_worker_thread.start()


@app.on_event("shutdown")
def _shutdown_uncollect_worker() -> None:
    _job_worker_stop.set()
    if _job_worker_thread is not None:
        _job_worker_thread.join(timeout=5)
    _uncollect_worker.close()
    with _user_uncollect_workers_lock:
        workers = list(_user_uncollect_workers.values())
        _user_uncollect_workers.clear()
    for worker in workers:
        worker.close()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _kind_prefix(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return "" if kind.key == "favorites" else f"/{kind.key}"


def _content_context(content_kind: str = "favorites") -> dict:
    kind = get_content_kind(content_kind)
    prefix = _kind_prefix(kind.key)
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
        "jobs_url": "/jobs",
        "duplicates_url": "/duplicates",
        "batch_action_url": _batch_action_url(kind.key),
        "batch_export_url": _batch_export_url(kind.key),
    }


def _stats(content_kind: str = "favorites", user_id: str = DEFAULT_USER_ID) -> dict:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM {kind.table} WHERE user_id = ? AND is_removed=0",
        (user_id,),
    ).fetchone()["c"]
    if user_id == DEFAULT_USER_ID:
        indexed = conn.execute(
            f"SELECT COUNT(*) AS c FROM {kind.vector_table} WHERE id LIKE ? OR id NOT LIKE '%:%'",
            (f"{user_id}:%",),
        ).fetchone()["c"]
    else:
        indexed = conn.execute(
            f"SELECT COUNT(*) AS c FROM {kind.vector_table} WHERE id LIKE ?",
            (f"{user_id}:%",),
        ).fetchone()["c"]
    return {
        "total": total,
        "indexed": indexed,
        **_content_context(kind.key),
    }


def _row_get(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _category_options(content_kind: str = "favorites", user_id: str = DEFAULT_USER_ID) -> list[dict]:
    return [
        {"id": c["id"], "name": c["name"], "item_count": c.get("item_count", 0)}
        for c in cluster_mod.list_categories(account_id=user_id, content_kind=content_kind)
    ]


def _batch_action_url(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return "/favorites/batch/uncollect" if kind.key == "favorites" else "/likes/batch/unlike"


def _batch_export_url(content_kind: str = "favorites") -> str:
    kind = get_content_kind(content_kind)
    return "/favorites/batch/export" if kind.key == "favorites" else "/likes/batch/export"


def _uncollect_worker_for_user(user_id: str) -> PersistentUncollectWorker:
    user_id = normalize_user_id(user_id)
    if user_id == DEFAULT_USER_ID:
        return _uncollect_worker
    with _user_uncollect_workers_lock:
        worker = _user_uncollect_workers.get(user_id)
        if worker is None:
            worker = PersistentUncollectWorker(profile_path=accounts.profile_path_for_user(user_id))
            _user_uncollect_workers[user_id] = worker
        return worker


def _current_user_id(request: Request) -> str:
    return normalize_user_id(getattr(request.state, "user_id", DEFAULT_USER_ID))


def _fetch_douyin_profile_for_user(user_id: str) -> dict:
    from src.crawler.douyin import DouyinCrawler

    with DouyinCrawler(
        headless=True,
        api_mode=True,
        hide_window=True,
        browser_channel="chrome",
        profile_path=accounts.profile_path_for_user(user_id),
    ) as crawler:
        return crawler.get_self_profile()


@app.get("/avatar-cache")
async def avatar_cache(u: str):
    remote_url = unquote(u or "")
    parsed = urlparse(remote_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="invalid avatar url")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        suffix = ".img"
    cache_dir = settings.avatar_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{hashlib.sha256(remote_url.encode('utf-8')).hexdigest()}{suffix}"
    if not cache_path.exists():
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(remote_url)
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
    return FileResponse(cache_path)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": ""},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_claim_invite(
    request: Request,
    invite_code: str = Form(""),
    display_name: str = Form(""),
    next: str = Form("/"),
):
    try:
        _user, token = accounts.claim_invite(invite_code, display_name=display_name)
    except accounts.InviteError as e:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next or "/", "error": str(e)},
            status_code=400,
        )
    response = RedirectResponse(next or "/", status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    accounts.revoke_session(request.cookies.get(settings.session_cookie_name))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name)
    return response


# ---------------------------------------------------------------------------
# 抖音账号绑定（扫码授权）
# ---------------------------------------------------------------------------

def _run_douyin_auth(user_id: str) -> None:
    """后台线程：为指定用户运行抖音扫码授权，持续更新 _douyin_auth_sessions。"""
    from src.crawler.douyin import AuthQrCapture, DouyinCrawler

    profile_path = accounts.profile_path_for_user(user_id)
    qr_dir = profile_path.parent / "auth"
    qr_dir.mkdir(parents=True, exist_ok=True)
    qr_path = qr_dir / "douyin-login.png"

    def on_qr(capture: AuthQrCapture) -> None:
        ttl = f"{capture.ttl_seconds}s" if capture.ttl_seconds is not None else "?"
        with _douyin_auth_lock:
            _douyin_auth_sessions[user_id] = {
                "status": "qr_ready",
                "message": f"请用抖音 App 扫描二维码（有效期约 {ttl}，会自动刷新）",
                "qr_path": str(capture.display_path or capture.path),
            }

    with _douyin_auth_lock:
        _douyin_auth_sessions[user_id] = {
            "status": "starting",
            "message": "正在启动浏览器，请稍候...",
            "qr_path": None,
        }
    try:
        with DouyinCrawler(
            headless=False,
            api_mode=True,
            hide_window=True,
            browser_channel="chrome",
            profile_path=profile_path,
        ) as crawler:
            result = crawler.authorize_by_qr(
                timeout_s=180,
                screenshot_path=qr_path,
                on_qr_capture=on_qr,
            )
            profile = {}
            if result.success:
                try:
                    profile = crawler.get_self_profile()
                except Exception as e:
                    logger.warning("Could not refresh Douyin profile for {} after auth: {}", user_id, e)
        with _douyin_auth_lock:
            if result.success:
                if profile:
                    accounts.update_douyin_profile(user_id, profile)
                _douyin_auth_sessions[user_id] = {
                    "status": "success",
                    "message": f"{profile.get('nickname') or '抖音账号'}绑定成功！现在可以发起同步了。",
                    "qr_path": None,
                }
            else:
                _douyin_auth_sessions[user_id] = {
                    "status": "failed",
                    "message": result.message or "授权失败，请重试",
                    "qr_path": None,
                }
    except Exception as e:
        logger.exception("Douyin auth failed for {}: {}", user_id, e)
        with _douyin_auth_lock:
            _douyin_auth_sessions[user_id] = {
                "status": "failed",
                "message": f"出错了：{e}",
                "qr_path": None,
            }


def _job_queue_summary(user_id: str) -> dict:
    summary = {
        "pending": 0,
        "running": 0,
        "failed": 0,
        "total": 0,
    }
    for job in jobs.list_jobs(user_id=user_id, limit=200):
        summary["total"] += 1
        status = job.get("status")
        if status in summary:
            summary[status] += 1
    summary["needs_attention"] = summary["running"] > 0 or summary["failed"] > 0
    return summary


@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    user_id = _current_user_id(request)
    session = _douyin_auth_sessions.get(user_id, {})
    profile_exists = accounts.profile_path_for_user(user_id).exists()
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "auth_status": session.get("status", "idle"),
            "auth_message": session.get("message", ""),
            "qr_ready": session.get("status") == "qr_ready",
            "profile_exists": profile_exists,
            "job_summary": _job_queue_summary(user_id),
        },
    )


@app.post("/auth/start", response_class=HTMLResponse)
async def auth_start(request: Request):
    user_id = _current_user_id(request)
    with _douyin_auth_lock:
        current = _douyin_auth_sessions.get(user_id, {})
        if current.get("status") not in ("starting", "qr_ready"):
            _douyin_auth_sessions[user_id] = {"status": "starting", "message": "启动中...", "qr_path": None}
            t = threading.Thread(
                target=_run_douyin_auth,
                args=(user_id,),
                name=f"douyin-auth-{user_id}",
                daemon=True,
            )
            t.start()
    session = _douyin_auth_sessions.get(user_id, {})
    profile_exists = accounts.profile_path_for_user(user_id).exists()
    return templates.TemplateResponse(
        request,
        "_auth_status.html",
        {
            "auth_status": session.get("status", "starting"),
            "auth_message": session.get("message", "启动中..."),
            "qr_ready": session.get("status") == "qr_ready",
            "profile_exists": profile_exists,
        },
    )


@app.get("/auth/status", response_class=HTMLResponse)
async def auth_status_fragment(request: Request):
    user_id = _current_user_id(request)
    session = _douyin_auth_sessions.get(user_id, {})
    profile_exists = accounts.profile_path_for_user(user_id).exists()
    return templates.TemplateResponse(
        request,
        "_auth_status.html",
        {
            "auth_status": session.get("status", "idle"),
            "auth_message": session.get("message", ""),
            "qr_ready": session.get("status") == "qr_ready",
            "profile_exists": profile_exists,
        },
    )


@app.post("/auth/profile/refresh", response_class=HTMLResponse)
async def auth_profile_refresh(request: Request):
    user_id = _current_user_id(request)
    profile_exists = accounts.profile_path_for_user(user_id).exists()
    try:
        profile = _fetch_douyin_profile_for_user(user_id)
        if not profile or not profile.get("nickname"):
            raise RuntimeError("没有从抖音读取到昵称")
        user = accounts.update_douyin_profile(user_id, profile)
        request.state.current_user = user
        message = "账号资料已刷新。"
        status = "success"
    except Exception as e:
        logger.warning("Douyin profile refresh failed for {}: {}", user_id, e)
        err_text = str(e)
        if "用户未登录" in err_text or "登录态失效" in err_text or "请登录" in err_text:
            message = "登录态已过期，请重新绑定。昵称和头像会在重新绑定成功后自动保存。"
        else:
            message = "这次没有读到昵称和头像。通常是登录态过期或抖音没有返回账号资料，请重新绑定后再同步。"
        status = "failed"
    return templates.TemplateResponse(
        request,
        "_profile_summary.html",
        {
            "profile_exists": profile_exists,
            "profile_refresh_message": message,
            "profile_refresh_status": status,
        },
    )


@app.get("/auth/qr-image")
async def auth_qr_image(request: Request):
    user_id = _current_user_id(request)
    session = _douyin_auth_sessions.get(user_id, {})
    qr_path_str = session.get("qr_path")
    if qr_path_str:
        qr_path = Path(qr_path_str)
        if qr_path.exists():
            return FileResponse(str(qr_path), media_type="image/png",
                                headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "no qr"}, status_code=404)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    user_id = _current_user_id(request)
    session = _douyin_auth_sessions.get(user_id, {})
    status = onboarding.get_onboarding_status(user_id)
    profile_exists = bool(status.get("profile_path_exists") or status.get("has_profile"))
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "page": "setup",
            "status": status,
            "auth_status": session.get("status", "idle"),
            "auth_message": session.get("message", ""),
            "qr_ready": session.get("status") == "qr_ready",
            "profile_exists": profile_exists,
            **_stats("favorites", user_id=user_id),
        },
    )


@app.get("/setup/status", response_class=HTMLResponse)
async def setup_status_fragment(request: Request):
    user_id = _current_user_id(request)
    return templates.TemplateResponse(
        request,
        "_setup_status.html",
        {
            "status": onboarding.get_onboarding_status(user_id),
        },
    )


# ---------------------------------------------------------------------------
# 视频流代理（绕过 CORS，把抖音 CDN 签名 URL 透传给浏览器）
# ---------------------------------------------------------------------------

def _extract_play_urls(raw_json_str: str) -> list[str]:
    """从 raw_json 中提取 H264 mp4 播放 URL 列表，按画质从高到低。"""
    try:
        data = json.loads(raw_json_str)
    except Exception:
        return []
    video = data.get("video", {})
    urls: list[str] = []

    def _add(url: str) -> None:
        if url and url not in urls:
            urls.append(url)

    # 1. 专属 H264 地址（最优先）
    for u in video.get("play_addr_h264", {}).get("url_list", []):
        _add(u)

    # 2. bit_rate 里的 H264 mp4，按码率降序取前两档
    h264_brs = sorted(
        [b for b in video.get("bit_rate", [])
         if not b.get("is_h265") and b.get("format") == "mp4"],
        key=lambda b: b.get("bit_rate", 0),
        reverse=True,
    )
    for br in h264_brs[:2]:
        for u in br.get("play_addr", {}).get("url_list", []):
            _add(u)

    # 3. 通用 play_addr 兜底（可能是 H265，部分浏览器不支持）
    for u in video.get("play_addr", {}).get("url_list", []):
        _add(u)

    return urls[:6]


async def _stream_video_for_kind(request: Request, content_kind: str, favorite_id: str) -> StreamingResponse:
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    conn = get_connection()
    row = conn.execute(
        f"SELECT raw_json, video_url FROM {kind.table} WHERE user_id = ? AND id = ?",
        (user_id, favorite_id),
    ).fetchone()
    if not row or not row["raw_json"]:
        raise HTTPException(404, "not found")

    play_urls = _extract_play_urls(row["raw_json"])
    if not play_urls:
        raise HTTPException(404, "no playable url in raw_json")

    proxy_headers = {
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    range_header = request.headers.get("range")
    if range_header:
        proxy_headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(10, read=120))
    resp: httpx.Response | None = None
    for url in play_urls:
        try:
            req = httpx.Request("GET", url, headers=proxy_headers)
            r = await client.send(req, stream=True)
            if r.status_code in (200, 206):
                resp = r
                break
            await r.aclose()
        except Exception as e:
            logger.debug("video proxy attempt failed for {}: {}", url[:60], e)
            continue

    if resp is None:
        await client.aclose()
        raise HTTPException(503, "视频链接已过期，请重新同步后再试")

    resp_headers: dict[str, str] = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for h in ("content-type", "content-length", "content-range"):
        if h in resp.headers:
            resp_headers[h] = resp.headers[h]

    async def _gen():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _gen(),
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp_headers.get("content-type", "video/mp4"),
    )


@app.get("/favorites/{favorite_id}/stream")
async def stream_favorite_video(request: Request, favorite_id: str):
    return await _stream_video_for_kind(request, "favorites", favorite_id)


@app.get("/likes/{favorite_id}/stream")
async def stream_like_video(request: Request, favorite_id: str):
    return await _stream_video_for_kind(request, "likes", favorite_id)


@app.post("/jobs/sync")
async def enqueue_sync_job(
    request: Request,
    kind: str = Form("favorites"),
    max_pages: int = Form(500),
):
    content = get_content_kind(kind)
    user_id = _current_user_id(request)
    job_kind = "sync_likes" if content.key == "likes" else "sync_favorites"
    job_id = jobs.enqueue_job(
        job_kind,
        user_id=user_id,
        payload={
            "content_kind": content.key,
            "max_pages": max(1, int(max_pages or 500)),
        },
    )
    return JSONResponse({"ok": True, "job_id": job_id})


@app.post("/jobs/index")
async def enqueue_index_job(
    request: Request,
    kind: str = Form("favorites"),
    force: str = Form("false"),
):
    content = get_content_kind(kind)
    user_id = _current_user_id(request)
    job_id = jobs.enqueue_job(
        "index",
        user_id=user_id,
        payload={
            "content_kind": content.key,
            "force": str(force).lower() in {"1", "true", "yes", "on"},
        },
    )
    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    user_id = _current_user_id(request)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "page": "jobs",
            "jobs": jobs.list_jobs(user_id=user_id),
            **_stats("favorites", user_id=user_id),
        },
    )


@app.get("/jobs/status", response_class=HTMLResponse)
async def jobs_status_fragment(request: Request):
    user_id = _current_user_id(request)
    return templates.TemplateResponse(
        request,
        "_jobs_table.html",
        {
            "jobs": jobs.list_jobs(user_id=user_id),
        },
    )


@app.get("/duplicates", response_class=HTMLResponse)
async def duplicates_page(request: Request):
    user_id = _current_user_id(request)
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
            "items": [_row_to_item(r, "favorites") for r in rows],
            "category_options": _category_options("favorites", user_id=user_id),
            **_stats("favorites", user_id=user_id),
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
        "track_url": f"{_kind_prefix(kind.key)}/track/open/{candidate.id}",
    }


@app.get("/memories", response_class=HTMLResponse)
async def memories_page(request: Request):
    from src.recall import selector

    user_id = _current_user_id(request)
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
            **_stats("favorites", user_id=user_id),
            "category_options": _category_options("favorites", user_id=user_id),
            "batch_action_url": _batch_action_url("favorites"),
            "batch_export_url": _batch_export_url("favorites"),
        },
    )


def _row_to_item(row, content_kind: str = "favorites") -> dict:
    """统一把 sqlite row 转成 template item 格式。"""
    kind = get_content_kind(content_kind)
    item_prefix = "/favorites" if kind.key == "favorites" else f"/{kind.key}"
    return {
        "id": row["id"],
        "title": _row_get(row, "title"),
        "author": _row_get(row, "author"),
        "author_avatar_url": cached_author_avatar_url_from_raw_json(_row_get(row, "raw_json")),
        "video_url": _row_get(row, "video_url"),
        "cover_url": _row_get(row, "cover_url"),
        "user_note": _row_get(row, "user_note"),
        "category_id": _row_get(row, "category_id"),
        "favorited_at": str(_row_get(row, "favorited_at")) if _row_get(row, "favorited_at") else None,
        "first_seen_at": str(_row_get(row, "first_seen_at")) if _row_get(row, "first_seen_at") else None,
        "video_created_at": str(_row_get(row, "video_created_at")) if _row_get(row, "video_created_at") else None,
        "last_recalled_at": str(_row_get(row, "last_recalled_at")) if _row_get(row, "last_recalled_at") else None,
        "content_kind": kind.key,
        "content_label": kind.label,
        "note_url_prefix": item_prefix,
        "track_url": f"{_kind_prefix(kind.key)}/track/open/{row['id']}",
    }


def _fetch_item(
    favorite_id: str,
    content_kind: str = "favorites",
    user_id: str = DEFAULT_USER_ID,
) -> Optional[dict]:
    kind = get_content_kind(content_kind)
    user_id = normalize_user_id(user_id)
    conn = get_connection()
    row = conn.execute(
        f"""
        SELECT id, title, author, video_url, cover_url, user_note,
               raw_json, category_id,
               CAST({kind.time_column} AS TEXT) AS favorited_at,
               CAST(first_seen_at AS TEXT) AS first_seen_at,
               CAST(video_created_at AS TEXT) AS video_created_at,
               CAST(last_recalled_at AS TEXT) AS last_recalled_at
        FROM {kind.table}
        WHERE user_id = ? AND id = ?
        """,
        (user_id, favorite_id),
    ).fetchone()
    return _row_to_item(row, kind.key) if row else None


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
    return [_row_to_item(r, kind.key) for r in rows]


def _home_page_load_url(
    content_kind: str,
    offset: int,
    page_size: int,
    category: Optional[str] = None,
    author: Optional[str] = None,
) -> str:
    kind = get_content_kind(content_kind)
    path = f"{_kind_prefix(kind.key)}/page" if kind.key != "favorites" else "/page"
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
    path = _kind_prefix(kind.key) or "/"
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
        "category_options": _category_options(kind.key, user_id=user_id),
        "batch_action_url": _batch_action_url(kind.key),
        "batch_export_url": _batch_export_url(kind.key),
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
        "category_options": _category_options(kind.key, user_id=user_id),
        "batch_action_url": _batch_action_url(kind.key),
        "batch_export_url": _batch_export_url(kind.key),
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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, category: Optional[str] = None,
                author: Optional[str] = None, p: int = 1,
                page_size: int = HOME_PAGE_SIZE):
    return await _index_for_kind(
        request,
        "favorites",
        category=category,
        author=author,
        p=p,
        page_size=page_size,
    )


@app.get("/likes", response_class=HTMLResponse)
async def likes_index(request: Request, category: Optional[str] = None,
                      author: Optional[str] = None, p: int = 1,
                      page_size: int = HOME_PAGE_SIZE):
    return await _index_for_kind(
        request,
        "likes",
        category=category,
        author=author,
        p=p,
        page_size=page_size,
    )


async def _index_for_kind(request: Request, content_kind: str,
                          category: Optional[str] = None,
                          author: Optional[str] = None,
                          p: int = 1,
                          page_size: int = HOME_PAGE_SIZE):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
        empty_msg = f"{kind.label}库还是空的，先跑 `recall {'crawl' if kind.key == 'favorites' else 'crawl-likes'}`"
    else:
        empty_msg = ""
    ctx = {
        **_stats(kind.key, user_id=user_id),
        "page": "home",
        **page_data,
        "empty_msg": empty_msg,
        "current_category": category,
        "current_category_label": cat_label,
        "current_author": author,
        "current_author_label": f"@{author}" if author else None,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/page", response_class=HTMLResponse)
async def index_page(request: Request, offset: int = 0,
                     page_size: int = HOME_PAGE_SIZE,
                     category: Optional[str] = None,
                     author: Optional[str] = None):
    return await _index_page_for_kind(
        request,
        "favorites",
        offset=offset,
        page_size=page_size,
        category=category,
        author=author,
    )


@app.get("/likes/page", response_class=HTMLResponse)
async def likes_index_page(request: Request, offset: int = 0,
                           page_size: int = HOME_PAGE_SIZE,
                           category: Optional[str] = None,
                           author: Optional[str] = None):
    return await _index_page_for_kind(
        request,
        "likes",
        offset=offset,
        page_size=page_size,
        category=category,
        author=author,
    )


async def _index_page_for_kind(request: Request, content_kind: str,
                               offset: int = 0,
                               page_size: int = HOME_PAGE_SIZE,
                               category: Optional[str] = None,
                               author: Optional[str] = None):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
            **_content_context(kind.key),
            **page_data,
        },
    )


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", category: Optional[str] = None):
    return await _search_for_kind(request, "favorites", q=q, category=category)


@app.get("/likes/search", response_class=HTMLResponse)
async def likes_search(request: Request, q: str = "", category: Optional[str] = None):
    return await _search_for_kind(request, "likes", q=q, category=category)


async def _search_for_kind(request: Request, content_kind: str, q: str = "",
                           category: Optional[str] = None):
    """HTMX 调用，返回 _grid.html 片段。"""
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
            d = _row_to_item({
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
            "category_options": _category_options(kind.key, user_id=user_id),
            "batch_action_url": _batch_action_url(kind.key),
            "batch_export_url": _batch_export_url(kind.key),
            **(page_data if not q else {}),
        },
    )


# ---------------------------------------------------------------------------
# 时间轴
# ---------------------------------------------------------------------------

@app.get("/timeline", response_class=HTMLResponse)
async def timeline(request: Request, category: Optional[str] = None):
    return await _timeline_for_kind(request, "favorites", category=category)


@app.get("/likes/timeline", response_class=HTMLResponse)
async def likes_timeline(request: Request, category: Optional[str] = None):
    return await _timeline_for_kind(request, "likes", category=category)


async def _timeline_for_kind(request: Request, content_kind: str,
                             category: Optional[str] = None):
    """
    按年-月分组：
    - 有 favorited_at 的：按真实时间分组
    - 没有的（首批全量抓取）：单独分组在最后，按 discovery_index 倒序

    支持 ?category=<id> 或 ?category=uncategorized 过滤。
    """
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
        item = _row_to_item(r, kind.key)
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
        **_stats(kind.key, user_id=user_id),
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

@app.get("/authors", response_class=HTMLResponse)
async def authors_index(request: Request):
    return await _authors_index_for_kind(request, "favorites")


@app.get("/likes/authors", response_class=HTMLResponse)
async def likes_authors_index(request: Request):
    return await _authors_index_for_kind(request, "likes")


async def _authors_index_for_kind(request: Request, content_kind: str):
    """列出所有作者 + 各自条目数。"""
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
        **_stats(kind.key, user_id=user_id),
        "page": "authors",
        "authors": authors,
    })


# ---------------------------------------------------------------------------
# 有备注（你显式标记过的）
# ---------------------------------------------------------------------------

@app.get("/notes", response_class=HTMLResponse)
async def notes_index(request: Request):
    return await _notes_index_for_kind(request, "favorites")


@app.get("/likes/notes", response_class=HTMLResponse)
async def likes_notes_index(request: Request):
    return await _notes_index_for_kind(request, "likes")


async def _notes_index_for_kind(request: Request, content_kind: str):
    """列出所有你写过备注的条目。"""
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
    items = [_row_to_item(r, kind.key) for r in rows]
    return templates.TemplateResponse(request, "notes.html", {
        **_stats(kind.key, user_id=user_id),
        "page": "notes",
        "items": items,
        "empty_msg": "还没写过备注。在卡片下方点「为啥要存它？」加备注。",
    })


# ---------------------------------------------------------------------------
# 自动分类（M5）
# ---------------------------------------------------------------------------

@app.get("/categories", response_class=HTMLResponse)
async def categories_index(request: Request):
    return await _categories_index_for_kind(request, "favorites")


@app.get("/likes/categories", response_class=HTMLResponse)
async def likes_categories_index(request: Request):
    return await _categories_index_for_kind(request, "likes")


async def _categories_index_for_kind(request: Request, content_kind: str):
    """分类总览：网格列出所有类目。"""
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    cats = cluster_mod.list_categories(account_id=user_id, content_kind=kind.key)
    uncat = cluster_mod.count_uncategorized(account_id=user_id, content_kind=kind.key)
    ctx = {
        **_stats(kind.key, user_id=user_id),
        "page": "categories",
        "categories": cats,
        "uncategorized_count": uncat,
    }
    return templates.TemplateResponse(request, "categories.html", ctx)


@app.post("/categories/merge", response_class=HTMLResponse)
async def merge_category(request: Request, source_id: int = Form(...), target_id: int = Form(...)):
    return await _merge_category_for_kind(request, "favorites", source_id=source_id, target_id=target_id)


@app.post("/likes/categories/merge", response_class=HTMLResponse)
async def likes_merge_category(request: Request, source_id: int = Form(...), target_id: int = Form(...)):
    return await _merge_category_for_kind(request, "likes", source_id=source_id, target_id=target_id)


async def _merge_category_for_kind(
    request: Request,
    content_kind: str,
    *,
    source_id: int,
    target_id: int,
):
    user_id = _current_user_id(request)
    ok = cluster_mod.merge_categories(
        target_id,
        source_id,
        account_id=user_id,
        content_kind=content_kind,
    )
    if not ok:
        return HTMLResponse("<div class='empty'>分类合并失败。</div>", status_code=400)
    return await _categories_index_for_kind(request, content_kind)


@app.get("/categories/{category_id}/name/edit", response_class=HTMLResponse)
async def category_name_edit(request: Request, category_id: int):
    return await _category_name_edit_for_kind(request, "favorites", category_id)


@app.get("/likes/categories/{category_id}/name/edit", response_class=HTMLResponse)
async def likes_category_name_edit(request: Request, category_id: int):
    return await _category_name_edit_for_kind(request, "likes", category_id)


async def _category_name_edit_for_kind(request: Request, content_kind: str, category_id: int):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    conn = get_connection()
    row = conn.execute(
        f"SELECT id, name, auto_name, item_count FROM {kind.category_table} WHERE id = ? AND account_id = ?",
        (category_id, user_id),
    ).fetchone()
    if not row:
        return HTMLResponse("<div class='empty'>找不到这个分类</div>", status_code=404)
    return templates.TemplateResponse(
        request, "_cat_name_edit.html",
        {"cat": dict(row), **_content_context(kind.key)},
    )


@app.get("/categories/{category_id}/name/view", response_class=HTMLResponse)
async def category_name_view(request: Request, category_id: int):
    return await _category_name_view_for_kind(request, "favorites", category_id)


@app.get("/likes/categories/{category_id}/name/view", response_class=HTMLResponse)
async def likes_category_name_view(request: Request, category_id: int):
    return await _category_name_view_for_kind(request, "likes", category_id)


async def _category_name_view_for_kind(request: Request, content_kind: str, category_id: int):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    conn = get_connection()
    row = conn.execute(
        f"SELECT id, name, auto_name, item_count FROM {kind.category_table} WHERE id = ? AND account_id = ?",
        (category_id, user_id),
    ).fetchone()
    if not row:
        return HTMLResponse("<div class='empty'>找不到这个分类</div>", status_code=404)
    return templates.TemplateResponse(
        request, "_cat_name_view.html",
        {"cat": dict(row), **_content_context(kind.key)},
    )


@app.patch("/categories/{category_id}/name", response_class=HTMLResponse)
async def category_name_save(request: Request, category_id: int, name: str = Form("")):
    return await _category_name_save_for_kind(request, "favorites", category_id, name)


@app.patch("/likes/categories/{category_id}/name", response_class=HTMLResponse)
async def likes_category_name_save(request: Request, category_id: int, name: str = Form("")):
    return await _category_name_save_for_kind(request, "likes", category_id, name)


async def _category_name_save_for_kind(request: Request, content_kind: str,
                                       category_id: int, name: str = Form("")):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
        {"cat": dict(row), **_content_context(kind.key)},
    )


# ---------------------------------------------------------------------------
# 备注编辑（HTMX 片段）
# ---------------------------------------------------------------------------

@app.get("/favorites/{favorite_id}/note/edit", response_class=HTMLResponse)
async def note_edit_form(request: Request, favorite_id: str):
    return await _note_edit_form_for_kind(request, "favorites", favorite_id)


@app.get("/likes/{favorite_id}/note/edit", response_class=HTMLResponse)
async def like_note_edit_form(request: Request, favorite_id: str):
    return await _note_edit_form_for_kind(request, "likes", favorite_id)


async def _note_edit_form_for_kind(request: Request, content_kind: str, favorite_id: str):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    item = _fetch_item(favorite_id, kind.key, user_id=user_id)
    if not item:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
    return templates.TemplateResponse(request, "_note_edit.html", {"item": item})


@app.get("/favorites/{favorite_id}/note/view", response_class=HTMLResponse)
async def note_view(request: Request, favorite_id: str):
    return await _note_view_for_kind(request, "favorites", favorite_id)


@app.get("/likes/{favorite_id}/note/view", response_class=HTMLResponse)
async def like_note_view(request: Request, favorite_id: str):
    return await _note_view_for_kind(request, "likes", favorite_id)


async def _note_view_for_kind(request: Request, content_kind: str, favorite_id: str):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    item = _fetch_item(favorite_id, kind.key, user_id=user_id)
    if not item:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
    return templates.TemplateResponse(request, "_note_view.html", {"item": item})


@app.patch("/favorites/{favorite_id}/note", response_class=HTMLResponse)
async def note_save(request: Request, favorite_id: str, note: str = Form("")):
    return await _note_save_for_kind(request, "favorites", favorite_id, note)


@app.patch("/likes/{favorite_id}/note", response_class=HTMLResponse)
async def like_note_save(request: Request, favorite_id: str, note: str = Form("")):
    return await _note_save_for_kind(request, "likes", favorite_id, note)


async def _note_save_for_kind(request: Request, content_kind: str,
                              favorite_id: str, note: str = ""):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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

    item = _fetch_item(favorite_id, kind.key, user_id=user_id)
    return templates.TemplateResponse(request, "_note_view.html", {"item": item})


@app.post("/favorites/{favorite_id}/category", response_class=HTMLResponse)
async def favorite_category_save(
    request: Request,
    favorite_id: str,
    category_id: str = Form(""),
    batch_select: str = Form(""),
):
    return await _category_save_for_kind(request, "favorites", favorite_id, category_id, batch_select)


@app.post("/likes/{favorite_id}/category", response_class=HTMLResponse)
async def like_category_save(
    request: Request,
    favorite_id: str,
    category_id: str = Form(""),
    batch_select: str = Form(""),
):
    return await _category_save_for_kind(request, "likes", favorite_id, category_id, batch_select)


async def _category_save_for_kind(
    request: Request,
    content_kind: str,
    favorite_id: str,
    category_id: str,
    batch_select: str = "",
):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
    item = _fetch_item(favorite_id, kind.key, user_id=user_id)
    if item is None:
        return HTMLResponse("<div class='empty'>找不到这条</div>", status_code=404)
    return templates.TemplateResponse(
        request,
        "_card.html",
        {
            "item": item,
            "category_options": _category_options(kind.key, user_id=user_id),
            "batch_action_url": _batch_action_url(kind.key),
            "batch_export_url": _batch_export_url(kind.key),
            "show_batch_select": str(batch_select).lower() in {"1", "true", "yes", "on"},
        },
    )


# ---------------------------------------------------------------------------
# 取消收藏（在抖音端真的取消）
# ---------------------------------------------------------------------------

@app.post("/favorites/batch/uncollect", response_class=HTMLResponse)
async def batch_uncollect_favorites(request: Request, ids: list[str] = Form([])):
    return await _batch_remove_for_kind(request, "favorites", ids)


@app.post("/likes/batch/unlike", response_class=HTMLResponse)
async def batch_unlike_likes(request: Request, ids: list[str] = Form([])):
    return await _batch_remove_for_kind(request, "likes", ids)


@app.post("/favorites/batch/export")
async def batch_export_favorites(request: Request, ids: list[str] = Form([])):
    return _batch_export_for_kind(request, "favorites", ids)


@app.post("/likes/batch/export")
async def batch_export_likes(request: Request, ids: list[str] = Form([])):
    return _batch_export_for_kind(request, "likes", ids)


def _batch_export_for_kind(request: Request, content_kind: str, ids: list[str]):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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


async def _batch_remove_for_kind(request: Request, content_kind: str, ids: list[str]):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
    unique_ids = [item_id for item_id in dict.fromkeys(ids or []) if item_id][:50]
    if not unique_ids:
        return HTMLResponse("<div class='empty'>没有选择条目。</div>", status_code=400)

    conn = get_connection()
    removed = 0
    now = datetime.now(timezone.utc)
    async with _uncollect_lock:
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


@app.post("/favorites/{favorite_id}/uncollect", response_class=HTMLResponse)
async def uncollect_favorite(request: Request, favorite_id: str):
    """
    后台复用本地登录态调用抖音 Web 收藏接口取消收藏。
    成功返回空内容（HTMX 把卡片 remove 掉）；失败返回错误片段。
    """
    conn = get_connection()
    user_id = _current_user_id(request)
    async with _uncollect_lock:
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


@app.post("/likes/{favorite_id}/unlike", response_class=HTMLResponse)
async def unlike_like(request: Request, favorite_id: str):
    """
    后台复用本地登录态调用抖音 Web 点赞接口取消喜欢。
    成功返回空内容（HTMX 把卡片 remove 掉）；失败返回错误片段。
    """
    conn = get_connection()
    user_id = _current_user_id(request)
    async with _uncollect_lock:
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

@app.post("/track/open/{favorite_id}")
async def track_open(request: Request, favorite_id: str):
    return await _track_open_for_kind(request, "favorites", favorite_id)


@app.post("/likes/track/open/{favorite_id}")
async def likes_track_open(request: Request, favorite_id: str):
    return await _track_open_for_kind(request, "likes", favorite_id)


async def _track_open_for_kind(request: Request, content_kind: str, favorite_id: str):
    kind = get_content_kind(content_kind)
    user_id = _current_user_id(request)
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
