"""Maintenance, diagnostics, backup, and restore routes."""
from __future__ import annotations

import copy
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from src import db as db_module
from src import diagnostics, jobs, maintenance
from src.db import init_schema
from src.web.helpers import current_user_id, templates
from src.web import runtime
from src.web.routes import auth, content
from src.web.routes.jobs import jobs_for_template


router = APIRouter()


def public_operation_error_message(prefix: str, message: str | None = None) -> str:
    return f"{(prefix or '操作失败').strip()}，请打开诊断包或日志查看详情。"


def _public_backup_item(item: dict | None) -> dict | None:
    if not item:
        return None
    public = dict(item)
    name = public.get("name") or Path(str(public.get("path") or "")).name
    if name:
        public["name"] = name
        public["path"] = name
    else:
        public.pop("path", None)
    return public


def _public_backup_items(items: list[dict] | None) -> list[dict]:
    return [public for item in (items or []) if (public := _public_backup_item(item)) is not None]


def _public_local_path_token(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "本机路径"
    return Path(text).name or "本机路径"


def _public_diagnostic_value_for_template(value, *, key: str = ""):
    lowered_key = key.lower()
    if isinstance(value, dict):
        return {
            item_key: _public_diagnostic_value_for_template(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_public_diagnostic_value_for_template(item, key=key) for item in value]
    if not isinstance(value, str) or not value.strip():
        return value
    if any(token in lowered_key for token in ("path", "dir", "root", "file")):
        return _public_local_path_token(value)
    if "command" in lowered_key:
        return "本机命令"
    if auth.looks_sensitive_for_public_page(value):
        prefix = "任务失败" if "error" in lowered_key else "维护状态异常"
        return public_operation_error_message(prefix, value)
    return value


def public_maintenance_status_for_template(user_id: str) -> dict:
    status = copy.deepcopy(maintenance.get_maintenance_status(user_id, include_update=True))
    backups = status.get("backups")
    if isinstance(backups, dict):
        backups["output_dir"] = "本机备份目录"
        backups["latest"] = _public_backup_item(backups.get("latest"))
        backups["items"] = _public_backup_items(backups.get("items"))
        retention = backups.get("retention")
        if isinstance(retention, dict):
            for key in ("kept", "delete_candidates", "protected"):
                retention[key] = _public_backup_items(retention.get(key))
    auth_status = status.get("auth")
    if isinstance(auth_status, dict):
        latest_error = auth_status.get("latest_error")
        if isinstance(latest_error, dict):
            raw_message = latest_error.get("message")
            latest_error["message"] = (
                "登录态可能过期，请重新绑定抖音账号。"
                if auth.looks_sensitive_for_public_page(raw_message)
                else auth.public_douyin_auth_message(raw_message)
            )
        errors = auth_status.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict):
                    raw_message = error.get("message")
                    error["message"] = (
                        "登录态可能过期，请重新绑定抖音账号。"
                        if auth.looks_sensitive_for_public_page(raw_message)
                        else auth.public_douyin_auth_message(raw_message)
                    )
    update = status.get("update")
    if isinstance(update, dict) and update.get("error"):
        update["error"] = "检查更新失败，请稍后再试；详情见日志或诊断包。"
    status["sections"] = _public_diagnostic_value_for_template(status.get("sections"))
    status["capabilities"] = _public_diagnostic_value_for_template(status.get("capabilities"))
    return status


def _resolve_restore_backup_path(backup_path: str) -> Path:
    path = Path((backup_path or "").strip())
    if not path.is_absolute() and len(path.parts) == 1:
        return maintenance.DEFAULT_BACKUP_DIR / path.name
    return path


def _public_restore_report_for_template(report: dict) -> dict:
    public = dict(report or {})
    public["path"] = public.get("name") or Path(str(public.get("path") or "")).name
    return public


def _maintenance_template_context(
    request: Request,
    user_id: str,
    *,
    message: str = "",
    message_kind: str = "info",
) -> dict:
    return {
        "page": "maintenance",
        "jobs": jobs_for_template(user_id),
        "maintenance_status": public_maintenance_status_for_template(user_id),
        "message": message,
        "message_kind": message_kind,
        **content._stats("favorites", user_id=user_id),
    }


@router.get("/maintenance", response_class=HTMLResponse)
def maintenance_page(request: Request):
    user_id = current_user_id(request)
    return templates.TemplateResponse(
        request, "jobs.html", _maintenance_template_context(request, user_id)
    )


@router.get("/maintenance/status", response_class=HTMLResponse)
def maintenance_status_fragment(request: Request):
    user_id = current_user_id(request)
    return templates.TemplateResponse(
        request,
        "_maintenance_status.html",
        _maintenance_template_context(request, user_id),
    )


@router.post("/maintenance/run", response_class=HTMLResponse)
def enqueue_standard_maintenance(request: Request, max_pages: int = Form(500)):
    user_id = current_user_id(request)
    job_ids = maintenance.enqueue_full_maintenance(user_id, max_pages=max_pages)
    return templates.TemplateResponse(
        request,
        "_maintenance_status.html",
        _maintenance_template_context(
            request,
            user_id,
            message=f"已加入标准维护队列：{len(job_ids)} 个任务。",
            message_kind="success",
        ),
    )


@router.post("/maintenance/backup", response_class=HTMLResponse)
def backup_sqlite_now(request: Request):
    user_id = current_user_id(request)
    try:
        result = maintenance.create_sqlite_backup()
        message = f"已生成 SQLite 备份：{result.path.name}"
        message_kind = "success"
    except Exception as exc:
        logger.exception("Manual SQLite backup failed: {}", exc)
        message = public_operation_error_message("备份失败", str(exc))
        message_kind = "danger"
    return templates.TemplateResponse(
        request,
        "_maintenance_status.html",
        _maintenance_template_context(request, user_id, message=message, message_kind=message_kind),
    )


@router.post("/maintenance/diagnostics", response_class=HTMLResponse)
def create_diagnostics_now(request: Request):
    user_id = current_user_id(request)
    try:
        result = diagnostics.create_diagnostic_bundle(diagnostics.DEFAULT_OUTPUT_DIR)
        message = f"诊断包已生成：{result.path.name}"
        message_kind = "success"
    except Exception as exc:
        logger.exception("Diagnostic bundle failed: {}", exc)
        message = public_operation_error_message("诊断包生成失败", str(exc))
        message_kind = "danger"
    return templates.TemplateResponse(
        request,
        "_maintenance_status.html",
        _maintenance_template_context(request, user_id, message=message, message_kind=message_kind),
    )


@router.post("/maintenance/restore/validate", response_class=HTMLResponse)
def validate_restore_backup(request: Request, backup_path: str = Form("")):
    report = maintenance.validate_sqlite_backup(_resolve_restore_backup_path(backup_path))
    return templates.TemplateResponse(
        request,
        "_restore_preview.html",
        {
            "restore_report": _public_restore_report_for_template(report),
            "restore_message": "",
            "restore_message_kind": "info",
        },
    )


@router.post("/maintenance/restore", response_class=HTMLResponse)
def restore_backup(
    request: Request,
    backup_path: str = Form(""),
    confirm_text: str = Form(""),
):
    user_id = current_user_id(request)
    resolved_backup_path = _resolve_restore_backup_path(backup_path)
    report = maintenance.validate_sqlite_backup(resolved_backup_path)
    if (confirm_text or "").strip() != "恢复":
        return templates.TemplateResponse(
            request,
            "_restore_preview.html",
            {
                "restore_report": _public_restore_report_for_template(report),
                "restore_message": "没有输入确认文字，未执行恢复。",
                "restore_message_kind": "danger",
            },
            status_code=400,
        )
    active_jobs = [
        job
        for job in jobs.list_jobs(user_id=user_id, limit=200)
        if job.get("status") in {"pending", "running"}
    ]
    if active_jobs:
        return templates.TemplateResponse(
            request,
            "_restore_preview.html",
            {
                "restore_report": _public_restore_report_for_template(report),
                "restore_message": "后台队列还有等待或运行中的任务，请等任务结束后再恢复。",
                "restore_message_kind": "danger",
            },
            status_code=409,
        )
    try:
        runtime.stop_background_workers()
        result = maintenance.restore_sqlite_backup(
            resolved_backup_path,
            close_connection=db_module.close_connection,
        )
        init_schema()
        runtime.start_background_workers()
        message = f"已恢复数据库。恢复前安全备份：{result.safety_backup_path.name}"
        message_kind = "success"
    except Exception as exc:
        logger.exception("SQLite restore failed: {}", exc)
        try:
            init_schema()
            runtime.start_background_workers()
        except Exception:
            logger.exception("Could not restart database connection after failed restore")
        message = public_operation_error_message("恢复失败", str(exc))
        message_kind = "danger"
    return templates.TemplateResponse(
        request,
        "_maintenance_status.html",
        _maintenance_template_context(request, user_id, message=message, message_kind=message_kind),
    )
