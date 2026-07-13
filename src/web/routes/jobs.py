"""Background job routes."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src import jobs
from src.content.kinds import get_content_kind
from src.web import content_state, job_service
from src.web.helpers import current_user_id, templates


router = APIRouter()


@router.post("/jobs/sync")
def enqueue_sync_job(
    request: Request,
    kind: str = Form("favorites"),
    max_pages: int = Form(500),
):
    content_kind = get_content_kind(kind)
    user_id = current_user_id(request)
    job_kind = "sync_likes" if content_kind.key == "likes" else "sync_favorites"
    job_id = jobs.enqueue_job(
        job_kind,
        user_id=user_id,
        payload={
            "content_kind": content_kind.key,
            "max_pages": max(1, int(max_pages or 500)),
        },
    )
    if request.headers.get("HX-Request"):
        return RedirectResponse(
            content_state.empty_status_url(content_kind.key),
            status_code=303,
        )
    return JSONResponse({"ok": True, "job_id": job_id})


@router.post("/jobs/index")
def enqueue_index_job(
    request: Request,
    kind: str = Form("favorites"),
    force: str = Form("false"),
):
    content_kind = get_content_kind(kind)
    job_id = jobs.enqueue_job(
        "index",
        user_id=current_user_id(request),
        payload={
            "content_kind": content_kind.key,
            "force": str(force).lower() in {"1", "true", "yes", "on"},
        },
    )
    return JSONResponse({"ok": True, "job_id": job_id})


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return RedirectResponse("/maintenance", status_code=303)


@router.get("/jobs/status", response_class=HTMLResponse)
def jobs_status_fragment(request: Request):
    return templates.TemplateResponse(
        request,
        "_jobs_table.html",
        {"jobs": job_service.jobs_for_template(current_user_id(request))},
    )
