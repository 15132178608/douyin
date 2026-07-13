"""
Web template behavior tests.

Run:
    python tests/test_web_templates.py
"""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def read_template(name: str) -> str:
    return (ROOT / "src" / "web" / "templates" / name).read_text(encoding="utf-8")


def read_web_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "src" / "web").glob("**/*.py"))
    )


def test_web_app_is_application_assembly_only() -> None:
    app_path = ROOT / "src" / "web" / "app.py"
    app_source = app_path.read_text(encoding="utf-8")

    assert len(app_source.splitlines()) <= 300
    assert "@app.on_event" not in app_source
    assert "lifespan=" in app_source
    for module in (
        "auth",
        "setup",
        "browse",
        "categories",
        "item_actions",
        "jobs",
        "maintenance",
        "media",
    ):
        assert f"src.web.routes.{module}" in app_source
        assert "app.include_router" in app_source
    assert "import *" not in app_source
    assert "def __getattr__" not in app_source

    routes_dir = ROOT / "src" / "web" / "routes"
    for module in (
        "auth",
        "setup",
        "browse",
        "categories",
        "item_actions",
        "jobs",
        "maintenance",
        "media",
    ):
        source = (routes_dir / f"{module}.py").read_text(encoding="utf-8")
        assert "router.get(" in source or "router.post(" in source
        assert ")(content." not in source
    assert not (routes_dir / "content.py").exists()


def test_route_modules_are_decoupled_and_route_topology_stays_stable() -> None:
    source_paths = [
        *sorted((ROOT / "src" / "web" / "routes").glob("*.py")),
        ROOT / "src" / "web" / "runtime.py",
    ]
    for source_path in source_paths:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        package = (
            "src.web.routes" if source_path.parent.name == "routes" else "src.web"
        )
        forbidden_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                forbidden_imports.extend(
                    alias.name
                    for alias in node.names
                    if alias.name == "src.web.routes"
                    or alias.name.startswith("src.web.routes.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name(f"{'.' * node.level}{module}", package)
                if module == "src.web.routes" or module.startswith("src.web.routes."):
                    forbidden_imports.append(module)
        assert not forbidden_imports, f"{source_path}: {forbidden_imports}"

    from src.web.app import app

    assert len(app.routes) == 80
    route_module_counts = {
        module: sum(
            getattr(route.endpoint, "__module__", "") == f"src.web.routes.{module}"
            for route in app.routes
        )
        for module in ("browse", "categories", "item_actions")
    }
    assert route_module_counts == {
        "browse": 16,
        "categories": 14,
        "item_actions": 16,
    }
    split_route_topology = {
        module: {
            f"{','.join(sorted(route.methods or []))} {route.path}"
            for route in app.routes
            if getattr(route.endpoint, "__module__", "") == f"src.web.routes.{module}"
        }
        for module in route_module_counts
    }
    assert split_route_topology == {
        "browse": {
            "GET /",
            "GET /authors",
            "GET /duplicates",
            "GET /empty-status",
            "GET /likes",
            "GET /likes/authors",
            "GET /likes/empty-status",
            "GET /likes/notes",
            "GET /likes/page",
            "GET /likes/search",
            "GET /likes/timeline",
            "GET /memories",
            "GET /notes",
            "GET /page",
            "GET /search",
            "GET /timeline",
        },
        "categories": {
            "GET /categories",
            "GET /categories/{category_id}/name/edit",
            "GET /categories/{category_id}/name/view",
            "GET /likes/categories",
            "GET /likes/categories/{category_id}/name/edit",
            "GET /likes/categories/{category_id}/name/view",
            "PATCH /categories/{category_id}/name",
            "PATCH /likes/categories/{category_id}/name",
            "POST /categories/import",
            "POST /categories/merge",
            "POST /categories/organize",
            "POST /likes/categories/import",
            "POST /likes/categories/merge",
            "POST /likes/categories/organize",
        },
        "item_actions": {
            "GET /favorites/{favorite_id}/note/edit",
            "GET /favorites/{favorite_id}/note/view",
            "GET /likes/{favorite_id}/note/edit",
            "GET /likes/{favorite_id}/note/view",
            "PATCH /favorites/{favorite_id}/note",
            "PATCH /likes/{favorite_id}/note",
            "POST /favorites/batch/export",
            "POST /favorites/batch/uncollect",
            "POST /favorites/{favorite_id}/category",
            "POST /favorites/{favorite_id}/uncollect",
            "POST /likes/batch/export",
            "POST /likes/batch/unlike",
            "POST /likes/track/open/{favorite_id}",
            "POST /likes/{favorite_id}/category",
            "POST /likes/{favorite_id}/unlike",
            "POST /track/open/{favorite_id}",
        },
    }
    item_action_route_order = [
        route.path
        for route in app.routes
        if getattr(route.endpoint, "__module__", "") == "src.web.routes.item_actions"
    ]
    assert item_action_route_order.index("/favorites/batch/uncollect") < item_action_route_order.index(
        "/favorites/{favorite_id}/uncollect"
    )
    assert item_action_route_order.index("/likes/batch/unlike") < item_action_route_order.index(
        "/likes/{favorite_id}/unlike"
    )
    assert all(
        getattr(route.endpoint, "__module__", "") != "src.web.routes.content"
        for route in app.routes
    )
    stream_routes = {
        (route.path, frozenset(route.methods or ())): getattr(
            route.endpoint, "__module__", ""
        )
        for route in app.routes
        if route.path.endswith("/stream")
    }
    assert stream_routes == {
        ("/favorites/{favorite_id}/stream", frozenset({"GET"})): "src.web.routes.media",
        ("/likes/{favorite_id}/stream", frozenset({"GET"})): "src.web.routes.media",
    }

    route_line_limits = {
        "browse.py": 950,
        "categories.py": 400,
        "item_actions.py": 450,
    }
    for filename, limit in route_line_limits.items():
        source = (ROOT / "src" / "web" / "routes" / filename).read_text(encoding="utf-8")
        assert len(source.splitlines()) <= limit


def test_remaining_async_functions_in_app_have_real_awaits() -> None:
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app_source)
    async_defs = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)]

    assert len(async_defs) <= 5
    for node in async_defs:
        assert any(isinstance(child, ast.Await) for child in ast.walk(node)), node.name


def test_uncollect_button_has_busy_state_and_duplicate_submit_guard() -> None:
    card = read_template("_card.html")
    base = read_template("base.html")

    assert "data-disable-on-request" in card
    assert "取消中" in card
    assert "trash-working" in card
    assert "htmx:beforeRequest" in base
    assert "htmx:afterRequest" in base


def test_batch_selection_has_persistent_state_and_bulk_actions() -> None:
    grid = read_template("_grid.html")
    card = read_template("_card.html")
    base = read_template("base.html")

    assert "batch-toolbar" in grid
    assert "data-batch-content-kind" in grid
    assert "data-batch-count" in grid
    assert "data-batch-hidden-inputs" in grid
    assert "data-batch-clear" in grid
    assert "data-batch-requires-selection" in grid
    assert "批量导出" not in grid
    assert "batch_export_url" not in grid
    assert "card-select-input" in card
    assert "card-select-box" in card
    assert "data-batch-id" in card
    assert "card.is-selected" in base
    assert ".card:hover .card-select" in base
    assert ".card.is-selected .card-select" in base
    assert "pointer-events: none;" in base
    assert "douyin-batch-selection:" in base
    assert "applyBatchSelection" in base
    assert "clearBatchSelection" in base
    assert "data-batch-count" in base
    assert "data-batch-hidden-inputs" in base
    assert "path.indexOf(\"/batch/uncollect\")" in base
    assert "path.indexOf(\"/batch/unlike\")" in base


def test_empty_content_states_are_user_facing_not_cli_instructions() -> None:
    combined = "\n".join(
        [
            read_template("_grid.html"),
            read_template("_empty_state.html"),
            read_template("authors.html"),
            read_template("timeline.html"),
        ]
    )
    base = read_template("base.html")
    app_source = read_web_source()

    assert "recall crawl" not in combined
    assert "recall crawl-likes" not in combined
    assert "正在整理" in app_source
    assert "后台同步完成后会自动出现在这里" in app_source
    assert "同步{{ content_label }}" in combined
    assert 'hx-get="{{ empty_state.sync_url }}"' in combined
    assert 'hx-post="/jobs/sync"' in combined
    assert "去维护中心" not in combined
    assert "empty-panel" in base
    assert "emptySpin" in base
    assert "def empty_state_context" in app_source
    assert "def _empty_state_context" not in app_source
    assert '"/likes/empty-status"' in app_source


def test_card_layout_keeps_note_below_and_removes_inline_category_picker() -> None:
    card = read_template("_card.html")
    base = read_template("base.html")

    assert "card-note-section" in card
    note_view = read_template("_note_view.html")
    assert "添加备注" in note_view
    assert "为啥要存它" not in note_view
    assert "card-note" in note_view
    assert "note-edit-btn" in note_view
    assert "card-category-form" not in card
    assert 'name="category_id"' not in card
    assert "right: 8px;" in base.split(".card-select", 1)[1].split("}", 1)[0]
    assert "left: 8px;" not in base.split(".card-select", 1)[1].split("}", 1)[0]
    note_add_rule = base.split(".note-add-btn", 1)[1].split("}", 1)[0]
    note_section_rule = base.split(".card-note-section", 1)[1].split("}", 1)[0]
    card_body_rule = base.split(".card-body", 1)[1].split("}", 1)[0]
    card_link_rule = base.split(".card > a", 1)[1].split("}", 1)[0]
    card_title_rule = base.split(".card-title", 1)[1].split("}", 1)[0]
    card_meta_rule = base.split(".card-meta", 1)[1].split("}", 1)[0]
    assert "position: absolute" not in note_add_rule
    assert "top:" not in note_add_rule
    assert "opacity: 0" not in note_add_rule
    assert "min-height:" in note_section_rule
    assert "margin-top: auto" in note_section_rule
    assert "display: flex" in card_body_rule
    assert "flex: 0 0 auto" in card_link_rule
    assert "flex: 0 0 104px" in card_body_rule
    assert "height: 104px" in card_body_rule
    assert "min-height: 2.8em" in card_title_rule
    assert "white-space: nowrap" in card_meta_rule


def test_uncollect_routes_enqueue_jobs_without_import_time_worker_pool() -> None:
    app_source = read_web_source()

    assert "_uncollect_lock" in app_source
    assert "start_background_workers" in app_source
    assert "with _uncollect_lock" in app_source
    assert "row[\"is_removed\"]" in app_source
    assert "jobs.enqueue_job(" in app_source
    assert '"content_kind": "favorites"' in app_source
    assert "_uncollect_worker" not in app_source
    assert "shutdown_uncollect_workers" not in app_source


def test_web_startup_does_not_warm_uncollect_bridge_or_lock_profile() -> None:
    app_source = read_web_source()
    startup_block = app_source.split("def start_background_workers", 1)[1].split("def stop_background_workers", 1)[0]

    assert "_uncollect_worker.warmup" not in startup_block
    assert "uncollect-bridge-warmup" not in startup_block
    assert "jobs.run_forever" in startup_block


def test_web_startup_recovers_interrupted_jobs_before_worker_loop() -> None:
    app_source = read_web_source()
    startup_block = app_source.split("def start_background_workers", 1)[1].split("def stop_background_workers", 1)[0]

    assert "jobs.recover_stale_running_jobs(stale_after_seconds=0)" in startup_block
    assert startup_block.index("jobs.recover_stale_running_jobs(stale_after_seconds=0)") < startup_block.index("jobs.run_forever")


def test_topbar_does_not_show_or_poll_uncollect_bridge_status() -> None:
    app_source = read_web_source()
    base = read_template("base.html")

    assert "/status/uncollect-bridge" not in app_source
    assert "_bridge_status.html" not in base
    assert "uncollect-bridge-status" not in base
    assert "清理准备中" not in base
    assert "清理已就绪" not in base
    assert 'content_kind == "favorites"' not in base


def test_navigation_separates_modules_from_views() -> None:
    base = read_template("base.html")

    assert "module-switch" in base
    assert "view-nav" in base
    assert "module-link" in base
    assert "view-link" in base
    assert base.index("module-switch") < base.index("view-nav")
    view_nav = base.split('class="view-nav"', 1)[1].split("</div>", 1)[0]
    assert "{{ jobs_url }}" not in view_nav
    assert ">任务</a>" not in view_nav
    assert 'href="/maintenance"' not in view_nav
    assert ">维护</a>" not in view_nav


def test_auth_page_is_focused_account_management_without_job_controls() -> None:
    auth = read_template("auth.html") + read_template("_profile_summary.html") + read_template("_auth_status.html")
    app_source = read_web_source()
    auth_page_block = app_source.split('def auth_page', 1)[1].split('@router.post("/auth/start"', 1)[0]

    assert "账号管理" in auth
    assert "account-actions-shell" in auth
    assert "account-action-card" in auth
    assert "添加账号" in auth
    assert "切换账号" in auth
    assert "退出登录" in auth
    assert 'action="/auth/add"' in auth
    assert 'action="/auth/switch"' in auth
    assert "authPollTimer" in auth
    assert "AUTH_STATUS_POLL_MS = 350" in auth
    assert "setInterval(refreshAuthStatus, AUTH_STATUS_POLL_MS)" in auth
    assert "setInterval(refreshAuthStatus, 1000)" not in auth
    assert "https://unpkg.com/htmx.org" not in read_template("auth.html")
    assert "settings-shell" not in auth
    assert "settings-sidebar" not in auth
    assert "settings-nav-link" not in auth
    assert "本机数据" not in auth
    assert "同步内容" not in auth
    assert "登录资料" not in auth
    assert "刷新账号资料" not in auth
    assert 'hx-post="/auth/profile/refresh"' not in auth
    assert "account-panel" not in auth
    assert "同步操作" not in auth
    assert "同步收藏" not in auth
    assert "同步喜欢" not in auth
    assert "索引收藏" not in auth
    assert "索引喜欢" not in auth
    assert "后台队列" not in auth
    assert 'href="/jobs"' not in auth
    assert "job_summary" not in auth
    assert 'action="/auth/logout"' in auth
    assert "data-auth-start" in auth
    assert '"/auth/add"' in app_source
    assert '"/auth/switch"' in app_source
    assert '"/auth/logout"' in app_source
    assert "job_summary" not in auth_page_block


def test_maintenance_center_exposes_backup_and_full_maintenance_actions() -> None:
    jobs_template = read_template("jobs.html")
    maintenance_status = read_template("_maintenance_status.html")
    restore_preview = read_template("_restore_preview.html")
    app_source = read_web_source()
    base = read_template("base.html")

    assert "维护中心" in jobs_template
    assert 'hx-post="/maintenance/run"' in jobs_template
    assert 'hx-post="/maintenance/backup"' in jobs_template
    assert 'hx-post="/maintenance/diagnostics"' in jobs_template
    assert 'hx-get="/maintenance/status"' in jobs_template
    assert "最近同步" in maintenance_status
    assert "服务状态" in maintenance_status
    assert "版本更新" in maintenance_status
    assert "maintenance_status.server" in maintenance_status
    assert "maintenance_status.update" in maintenance_status
    assert "DouyinRecallSetup.exe" in maintenance_status
    assert "uv run python -m src.cli update" not in maintenance_status
    assert "控制入口" in maintenance_status
    assert "最近备份" in maintenance_status
    assert "抖音登录" in maintenance_status
    assert "登录态可能过期" in maintenance_status
    assert 'href="/auth"' in maintenance_status
    assert "douyin_login_expired" in maintenance_status
    assert "maintenance_status.backups.items" not in maintenance_status
    assert 'maintenance_status.backups["items"]' in maintenance_status
    assert 'hx-post="/maintenance/restore/validate"' in maintenance_status
    assert 'hx-post="/maintenance/restore"' in restore_preview
    assert 'name="confirm_text"' in restore_preview
    assert "输入“恢复”" in restore_preview
    assert "attention_codes" in maintenance_status
    assert '"/maintenance"' in app_source
    assert "enqueue_full_maintenance" in app_source
    assert "restore_sqlite_backup" in app_source
    assert "create_diagnostic_bundle" in app_source
    assert "backup_sqlite" in app_source
    assert 'href="/maintenance"' not in base


def test_category_page_starts_background_organize_without_cli_instructions() -> None:
    categories = read_template("categories.html")
    app_source = read_web_source()

    assert "recall index" not in categories
    assert "recall categorize" not in categories
    assert "开始整理分类" in categories
    assert 'hx-post="{{ kind_prefix }}/categories/organize"' in categories
    assert '"/categories/organize"' in app_source
    assert '"/likes/categories/organize"' in app_source
    assert '_enqueue_category_organize_jobs' in app_source
    assert '"categorize"' in app_source


def test_cli_exposes_server_lifecycle_commands_and_serve_guard() -> None:
    cli_source = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")

    assert 'from src import server_runtime' in cli_source
    assert '@cli.command("status")' in cli_source
    assert '@cli.command("stop")' in cli_source
    assert "should_start_server" in cli_source
    assert "write_server_state" in cli_source
    assert "clear_server_state" in cli_source
    assert "stop_recorded_server" in cli_source


def test_cli_exposes_diagnostic_bundle_command() -> None:
    cli_source = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")

    assert 'from src import diagnostics' in cli_source
    assert '@cli.command("diagnose")' in cli_source
    assert "create_diagnostic_bundle" in cli_source


def test_cli_exposes_update_check_command() -> None:
    cli_source = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")

    assert 'from src import update_check' in cli_source
    assert '@cli.command("update")' in cli_source
    assert "get_cached_update_status" in cli_source
    assert "--no-network" in cli_source


def test_setup_page_contains_first_run_sections_and_reuses_existing_endpoints() -> None:
    setup = (
        read_template("setup.html")
        + read_template("_setup_auth_status.html")
        + read_template("_setup_scan_state.html")
        + read_template("_setup_success_motion.html")
    )
    app_source = read_web_source()

    assert "扫码登录抖音" in setup
    assert "打开抖音 App 扫一扫" in setup
    assert "正在生成二维码" in setup
    assert "手机已扫码，等待你在抖音 App 确认登录" in setup
    assert "手机取消了登录" in setup
    assert "setupAuthPollTimer" in setup
    assert "SETUP_AUTH_POLL_MS = 350" in setup
    assert "setupAuthStatusKey" in setup
    assert "if (!force && nextKey === currentKey) { return; }" in setup
    assert 'hx-trigger="every 250ms"' not in setup
    assert 'hx-trigger="every 500ms"' not in setup
    assert 'hx-trigger="every 1s"' not in setup
    assert "auth_status in ('starting', 'qr_ready')" not in setup
    assert "auth_status in ('starting', 'qr_ready', 'scan_pending', 'confirmed')" in setup
    assert 'hx-target="#setup-auth-status-area"' not in setup
    assert "启动浏览器" not in setup
    assert "后台队列" not in setup
    assert "返回首页" not in setup
    assert "自动整理进度" not in setup
    assert 'action="/setup/auth-start"' in setup
    assert '"/setup/auth-status"' in setup
    assert 'hx-get="/setup/scan-state?qr={{ qr_version }}"' not in setup
    assert 'hx-post="/jobs/sync"' not in setup
    assert 'hx-post="/jobs/index"' not in setup
    assert 'hx-get="/setup/status"' not in setup
    assert "ensure_douyin_auth_started(user_id)" in app_source
    assert "job_service.enqueue_first_run_jobs(user_id)" in app_source
    assert '"/setup/auth-start"' in app_source
    assert '"/setup/auth-status"' in app_source
    assert '"/setup/scan-state"' in app_source
    assert '"/setup"' in app_source
    assert "get_onboarding_status" in app_source


def test_setup_scan_waiting_status_does_not_repaint_while_polling() -> None:
    setup = (
        read_template("setup.html")
        + read_template("_setup_auth_status.html")
        + read_template("_setup_scan_state.html")
        + read_template("_setup_success_motion.html")
    )
    app_source = read_web_source()
    scan_endpoint = app_source.split("def setup_scan_state_fragment", 1)[1].split(
        "\n\n@app.", 1
    )[0]

    assert 'id="setup-scan-state"' in setup
    assert 'id="setup-scan-poller"' not in setup
    assert "data-setup-auth-state" in setup
    assert "data-setup-qr-version" in setup
    assert 'hx-target="#setup-scan-state"' not in setup
    assert 'hx-target="#setup-auth-status-area"' not in setup
    assert "Response(status_code=204)" in scan_endpoint
    assert 'context["auth_status"] == "qr_ready"' in scan_endpoint
    assert 'context["auth_status"] == "scan_pending"' in scan_endpoint
    assert 'context["auth_status"] == "confirmed"' in scan_endpoint
    assert 'state == "confirmed"' in scan_endpoint
    assert '"_setup_auth_status.html"' in scan_endpoint


def test_setup_auth_shows_confirmed_state_before_background_sync_finishes() -> None:
    setup = (
        read_template("_setup_auth_status.html")
        + read_template("_setup_scan_state.html")
        + read_template("_setup_success_motion.html")
    )
    base = read_template("base.html")
    app_source = read_web_source()
    auth_worker = app_source.split("def _run_douyin_auth", 1)[1].split(
        "def ensure_douyin_auth_started", 1
    )[0]

    assert "auth_status == 'scan_pending'" in setup
    assert "auth_status in ('confirmed', 'success')" in setup
    assert "setup-success-motion" in setup
    assert "setup-success-ring" in setup
    assert "setup-sync-dots" in setup
    assert "data-auth-success" in setup
    assert "@keyframes setupSuccessIn" in base
    assert "@keyframes setupSuccessRipple" in base
    assert "@keyframes setupSyncDot" in base
    assert "prefers-reduced-motion: reduce" in base
    assert 'hx-get="/setup/scan-state?state=confirmed"' not in setup
    assert 'status": "confirmed"' in auth_worker
    assert 'status": "scan_pending"' in auth_worker
    assert "def on_scan_pending" in auth_worker
    assert "def on_login_confirmed" in auth_worker
    assert "on_scan_pending=on_scan_pending" in auth_worker
    assert "on_login_confirmed=on_login_confirmed" in auth_worker
    assert '"scan_pending", "confirmed", "success", "failed"' in app_source
    assert auth_worker.index("on_scan_pending=on_scan_pending") < auth_worker.index("on_login_confirmed=on_login_confirmed")
    assert auth_worker.index("on_login_confirmed=on_login_confirmed") < auth_worker.index("crawler.get_self_profile()")


def test_setup_auth_enqueues_sync_before_profile_refresh() -> None:
    app_source = read_web_source()
    auth_worker = app_source.split("def _run_douyin_auth", 1)[1].split(
        "def ensure_douyin_auth_started", 1
    )[0]

    assert "job_service.enqueue_first_run_jobs(user_id)" in auth_worker
    assert "crawler.get_self_profile()" in auth_worker
    assert auth_worker.index("job_service.enqueue_first_run_jobs(user_id)") < auth_worker.index("crawler.get_self_profile()")


def test_setup_page_is_scan_first_not_a_step_by_step_wizard() -> None:
    setup = (
        read_template("setup.html")
        + read_template("_setup_auth_status.html")
        + read_template("_setup_scan_state.html")
        + read_template("_setup_success_motion.html")
    )
    base = read_template("base.html")

    assert "setup-scan-shell" in setup
    assert "setup-qr-panel" in setup
    assert "setup-card" in setup
    assert "setup-scan-card" in setup
    assert "data-auth-success" in setup
    assert "setup-main-grid" not in setup
    assert "setup-primary-copy" not in setup
    assert "setup-progress-area" not in setup
    assert "setup-status-grid" not in setup
    assert "setup-privacy-notes" not in setup
    assert "setup-wizard" not in setup
    assert "setup-stepper" not in setup
    assert "setup-progress-fill" not in setup
    assert "data-setup-wizard" not in setup
    assert "data-setup-panel" not in setup
    assert "data-setup-prev" not in setup
    assert "data-setup-next" not in setup
    assert "上一步" not in setup
    assert "下一步" not in setup
    assert "setupWizardShowStep" not in setup
    assert "setup-grid" not in setup
    assert "{% if page != 'setup' %}" in base
    assert "setup-only-body" in base
    assert ".setup-scan-shell" in base
    assert ".setup-qr-panel" in base
    assert ".setup-card" in base
    assert ".setup-scan-card" in base
    assert ".setup-stepper" not in base
    assert ".setup-wizard-footer" not in base


def test_mobile_setup_layout_prevents_horizontal_overflow() -> None:
    base = read_template("base.html")

    assert "@media (max-width: 760px)" in base
    assert ".topbar-main {" in base
    assert "grid-template-columns: minmax(0, 1fr) auto" in base
    assert ".view-nav {" in base
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in base
    assert ".container {" in base
    assert "max-width: 100%" in base
    assert "overflow-x: hidden" in base
    assert ".setup-scan-shell {" in base
    assert ".setup-qr-panel {" in base
    assert "overflow-wrap: anywhere" in base


def test_home_redirects_first_run_to_scan_setup_instead_of_showing_empty_shell() -> None:
    index = read_template("index.html")
    app_source = read_web_source()

    assert 'href="/setup"' not in index
    assert "开始设置" not in index
    assert "content_state.should_show_setup_before_home" in app_source
    assert 'RedirectResponse("/setup", status_code=303)' in app_source
    assert "should_auto_start_setup_auth(status)" in app_source


def test_local_browser_runtime_does_not_require_user_installed_chrome() -> None:
    source_paths = [
        ROOT / "src" / "web" / "app.py",
        ROOT / "src" / "jobs.py",
        ROOT / "src" / "cli.py",
        ROOT / "src" / "uncollector" / "douyin.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)

    assert 'browser_channel="chrome"' not in combined
    assert 'browser_channel: Optional[str] = "chrome"' not in combined


def test_setup_qr_auth_does_not_show_internal_browser_window() -> None:
    app_source = read_web_source()
    auth_block = app_source.split("def _run_douyin_auth", 1)[1].split(
        "def ensure_douyin_auth_started", 1
    )[0]

    assert "headless=False" not in auth_block
    assert "headless=True" in auth_block
    assert "hide_window=True" in auth_block


def test_local_first_run_prewarms_qr_auth_on_web_startup() -> None:
    app_source = read_web_source()
    startup_block = app_source.split("def start_background_workers", 1)[1].split(
        "def stop_background_workers", 1
    )[0]

    assert "_maybe_prewarm_first_run_auth()" in startup_block
    assert "def _maybe_prewarm_first_run_auth" in app_source
    assert "settings.web_auth_required" in app_source
    assert "douyin_auth.should_auto_start_setup_auth(status)" in app_source
    assert "douyin_auth.ensure_douyin_auth_started(user_id)" in app_source


def test_desktop_pagination_blocks_mobile_load_more_trigger() -> None:
    base = read_template("base.html")
    load_more = read_template("_load_more.html")
    pagination = read_template("_pagination.html")

    assert "load-more-sentinel" in load_more
    assert "data-load-more-url" in load_more
    assert 'hx-trigger="revealed"' not in load_more
    assert "pagination-desktop" in pagination
    assert "matchMedia" in base
    assert "pointer: fine" in base
    assert "pointer: coarse" in base
    assert "load-more-sentinel" in base
    assert "enableMobileLoadMore" in base
    assert "htmx.process" in base
    assert "event.preventDefault()" in base


def test_desktop_pagination_has_number_links_and_direct_edges() -> None:
    pagination = read_template("_pagination.html")

    assert "first_page_url" in pagination
    assert "last_page_url" in pagination
    assert "pagination-pages" in pagination
    assert "pagination-number" in pagination
    assert "pagination-ellipsis" in pagination
    assert "aria-current" in pagination
    assert "首页" in pagination
    assert "末页" in pagination


def test_desktop_pagination_page_size_follows_grid_columns() -> None:
    base = read_template("base.html")

    assert "syncDesktopPageSize" in base
    assert "desktopPageSizeForGrid" in base
    assert "gridTemplateColumns" in base
    assert "DESKTOP_PAGE_ROWS" in base
    assert "page_size" in base


def test_card_meta_does_not_claim_platform_recall_history() -> None:
    app_source = read_web_source()
    card = read_template("_card.html")

    assert "从未想起" not in card
    assert "最近想起" not in card
    assert "喜欢于" in card
    assert "收藏于" in card
    assert "发布于" in card
    assert "发现于" not in card
    assert "时间未知" not in card
    assert "video_created_at" in app_source


def test_category_page_refreshes_after_back_forward_cache_uncollect() -> None:
    base = read_template("base.html")
    card = read_template("_card.html")

    assert "douyin-uncollect-changed" in base
    assert "htmx:afterSwap" in base
    assert "event.persisted" in base
    assert "\"/categories\", \"/likes/categories\"" in base
    assert "/likes/{{ item.id }}/unlike" in card
    assert "path.indexOf(\"/likes/\")" in base
    assert "path.indexOf(\"/unlike\")" in base


def test_author_page_renders_avatar_through_cache_proxy() -> None:
    app_source = read_web_source()
    authors = read_template("authors.html")
    base = read_template("base.html")

    assert "cached_author_avatar_url_from_raw_json" in app_source
    assert "avatar_url" in app_source
    assert "author-avatar" in authors
    assert "<img" in authors
    assert "author-avatar-fallback" in authors
    assert ".author-avatar" in base


def test_favorite_cards_render_author_avatar() -> None:
    app_source = read_web_source()
    hybrid_source = (ROOT / "src" / "search" / "hybrid.py").read_text(encoding="utf-8")
    card = read_template("_card.html")
    base = read_template("base.html")

    assert '"author_avatar_url"' in app_source
    assert "cached_author_avatar_url_from_raw_json(row_get(row, \"raw_json\"))" in app_source
    assert "raw_json" in hybrid_source
    assert "raw_json: Optional[str]" in hybrid_source
    assert '"raw_json": h.raw_json' in app_source
    assert "item.author_avatar_url" in card
    assert "card-author-avatar" in card
    assert ".card-author-avatar" in base
    assert "[hidden] { display: none !important; }" in base


def test_topbar_renders_account_chip_with_douyin_profile_fallback() -> None:
    base = read_template("base.html")

    assert "account-chip" in base
    assert "request.state.current_user.douyin_nickname" in base
    assert "request.state.current_user.douyin_avatar_url" in base
    assert "request.state.current_user.display_name" in base
    assert "已绑定" in base
    assert "未读取昵称" not in base
    assert "账号" not in base.split("account-chip", 1)[1].split("</a>", 1)[0]


def test_video_modal_is_single_player_without_playlist_panel() -> None:
    base = read_template("base.html")

    assert "vm-stage" in base
    assert "vm-details" in base
    assert "vm-playlist" not in base
    assert "播放列表" not in base


def test_auth_page_has_switch_account_logout_and_bound_fallback_state() -> None:
    auth = read_template("auth.html") + read_template("_profile_summary.html") + read_template("_auth_status.html")

    assert 'hx-post="/auth/start"' not in auth
    assert 'action="/auth/add"' in auth
    assert 'action="/auth/switch"' in auth
    assert 'action="/auth/logout"' in auth
    assert "添加账号" in auth
    assert "切换账号" in auth
    assert "退出登录" in auth
    assert "刷新账号资料" not in auth
    assert ".profile-avatar[hidden] { display: none; }" in auth
    assert "已绑定抖音账号" in auth
    assert "昵称和头像还没读取到" in auth
    assert "登录态已过期" in auth
    assert "已有登录态" in auth
    assert "可直接同步" not in auth
    assert "back-home" in auth
    assert "本地默认用户" not in auth
    assert "未读取昵称" not in auth


if __name__ == "__main__":
    tests = [
        test_uncollect_button_has_busy_state_and_duplicate_submit_guard,
        test_batch_selection_has_persistent_state_and_bulk_actions,
        test_card_layout_keeps_note_below_and_removes_inline_category_picker,
        test_uncollect_routes_enqueue_jobs_without_import_time_worker_pool,
        test_web_startup_does_not_warm_uncollect_bridge_or_lock_profile,
        test_topbar_does_not_show_or_poll_uncollect_bridge_status,
        test_navigation_separates_modules_from_views,
        test_auth_page_is_focused_account_management_without_job_controls,
        test_maintenance_center_exposes_backup_and_full_maintenance_actions,
        test_cli_exposes_server_lifecycle_commands_and_serve_guard,
        test_cli_exposes_diagnostic_bundle_command,
        test_setup_page_contains_first_run_sections_and_reuses_existing_endpoints,
        test_setup_page_is_scan_first_not_a_step_by_step_wizard,
        test_mobile_setup_layout_prevents_horizontal_overflow,
        test_home_redirects_first_run_to_scan_setup_instead_of_showing_empty_shell,
        test_local_browser_runtime_does_not_require_user_installed_chrome,
        test_setup_qr_auth_does_not_show_internal_browser_window,
        test_local_first_run_prewarms_qr_auth_on_web_startup,
        test_desktop_pagination_blocks_mobile_load_more_trigger,
        test_desktop_pagination_has_number_links_and_direct_edges,
        test_desktop_pagination_page_size_follows_grid_columns,
        test_card_meta_does_not_claim_platform_recall_history,
        test_category_page_refreshes_after_back_forward_cache_uncollect,
        test_author_page_renders_avatar_through_cache_proxy,
        test_favorite_cards_render_author_avatar,
        test_topbar_renders_account_chip_with_douyin_profile_fallback,
        test_video_modal_is_single_player_without_playlist_panel,
        test_auth_page_has_switch_account_logout_and_bound_fallback_state,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
