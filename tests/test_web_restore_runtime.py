from __future__ import annotations

from pathlib import Path

import pytest

from src import maintenance
from src.web.routes import maintenance as maintenance_routes


def _restore_result(tmp_path: Path) -> maintenance.RestoreResult:
    safety = tmp_path / "pre-restore-recall-test.db"
    safety.write_bytes(b"safety")
    return maintenance.RestoreResult(
        backup_path=tmp_path / "source.db",
        restored_path=tmp_path / "recall.db",
        safety_backup_path=safety,
        validation={"ok": True},
    )


def test_committed_restore_init_failure_is_not_reported_as_restore_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_calls: list[str] = []

    def fail_init() -> None:
        raise RuntimeError("simulated init failure")

    monkeypatch.setattr(maintenance_routes, "init_schema", fail_init)
    monkeypatch.setattr(
        maintenance_routes.runtime,
        "start_background_workers",
        lambda: start_calls.append("started"),
    )

    message, message_kind = maintenance_routes._restart_after_committed_restore(
        _restore_result(tmp_path)
    )

    assert "已恢复数据库" in message
    assert "数据库恢复已完成，但服务重新初始化失败" in message
    assert "恢复失败" not in message
    assert message_kind == "danger"
    assert start_calls == []


def test_committed_restore_worker_start_failure_keeps_commit_boundary_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_calls: list[str] = []

    def fail_start() -> None:
        raise RuntimeError("simulated worker start failure")

    monkeypatch.setattr(
        maintenance_routes,
        "init_schema",
        lambda: init_calls.append("initialized"),
    )
    monkeypatch.setattr(
        maintenance_routes.runtime,
        "start_background_workers",
        fail_start,
    )

    message, message_kind = maintenance_routes._restart_after_committed_restore(
        _restore_result(tmp_path)
    )

    assert init_calls == ["initialized"]
    assert "已恢复数据库" in message
    assert "数据库恢复已完成，但服务重新初始化失败" in message
    assert "恢复失败" not in message
    assert message_kind == "danger"


def test_restore_route_uses_db_independent_response_after_committed_restart_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _restore_result(tmp_path)
    rendered: dict = {}

    def fake_template_response(request, template_name, context, **kwargs):
        rendered.update(
            {
                "request": request,
                "template_name": template_name,
                "context": context,
                **kwargs,
            }
        )
        return rendered

    monkeypatch.setattr(maintenance_routes, "current_user_id", lambda _request: "default")
    monkeypatch.setattr(
        maintenance_routes,
        "_resolve_restore_backup_path",
        lambda _path: tmp_path / "source.db",
    )
    monkeypatch.setattr(
        maintenance_routes.maintenance,
        "validate_sqlite_backup",
        lambda _path: {"ok": True, "name": "source.db", "errors": []},
    )
    monkeypatch.setattr(maintenance_routes, "_has_any_active_jobs", lambda: False)
    monkeypatch.setattr(maintenance_routes.runtime, "stop_background_workers", lambda: None)
    monkeypatch.setattr(
        maintenance_routes.maintenance,
        "restore_sqlite_backup",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        maintenance_routes,
        "_restart_after_committed_restore",
        lambda _result: ("已恢复数据库；服务重新初始化失败。", "danger"),
    )
    monkeypatch.setattr(
        maintenance_routes,
        "_maintenance_template_context",
        lambda *_args, **_kwargs: pytest.fail(
            "committed restart failure must not query database-backed status context"
        ),
    )
    monkeypatch.setattr(
        maintenance_routes.templates,
        "TemplateResponse",
        fake_template_response,
    )

    response = maintenance_routes.restore_backup(
        object(),
        backup_path="source.db",
        confirm_text="恢复",
    )

    assert response["template_name"] == "_restore_preview.html"
    assert response["status_code"] == 200
    assert response["context"]["restore_message_kind"] == "danger"
    assert response["context"]["restore_committed"] is True
    assert response["context"]["restore_runtime_ready"] is False
    assert "已恢复数据库" in response["context"]["restore_message"]

    rendered.clear()
    monkeypatch.setattr(
        maintenance_routes,
        "_restart_after_committed_restore",
        lambda _result: ("已恢复数据库。", "success"),
    )
    success_response = maintenance_routes.restore_backup(
        object(),
        backup_path="source.db",
        confirm_text="恢复",
    )

    assert success_response["template_name"] == "_restore_preview.html"
    assert success_response["status_code"] == 200
    assert success_response["context"]["restore_message_kind"] == "success"
    assert success_response["context"]["restore_committed"] is True
    assert success_response["context"]["restore_runtime_ready"] is True
