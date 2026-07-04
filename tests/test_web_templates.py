"""
Web template behavior tests.

Run:
    python tests/test_web_templates.py
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def read_template(name: str) -> str:
    return (ROOT / "src" / "web" / "templates" / name).read_text(encoding="utf-8")


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


def test_card_layout_keeps_note_below_and_removes_inline_category_picker() -> None:
    card = read_template("_card.html")
    base = read_template("base.html")

    assert "card-note-section" in card
    assert "为啥要存它" in read_template("_note_view.html")
    assert "card-category-form" not in card
    assert 'name="category_id"' not in card
    assert "right: 8px;" in base.split(".card-select", 1)[1].split("}", 1)[0]
    assert "left: 8px;" not in base.split(".card-select", 1)[1].split("}", 1)[0]
    note_add_rule = base.split(".note-add-btn", 1)[1].split("}", 1)[0]
    assert "position: absolute" not in note_add_rule
    assert "top:" not in note_add_rule
    assert "opacity: 0" not in note_add_rule


def test_uncollect_route_serializes_profile_access_and_skips_removed_items() -> None:
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")

    assert "_uncollect_lock" in app_source
    assert "_uncollect_worker" in app_source
    assert "_start_background_workers" in app_source
    assert "async with _uncollect_lock" in app_source
    assert "row[\"is_removed\"]" in app_source
    assert "jobs.enqueue_job(" in app_source
    assert '"content_kind": "favorites"' in app_source
    assert "_shutdown_uncollect_worker" in app_source


def test_web_startup_does_not_warm_uncollect_bridge_or_lock_profile() -> None:
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    startup_block = app_source.split("def _start_background_workers", 1)[1].split("@app.on_event(\"shutdown\")", 1)[0]

    assert "_uncollect_worker.warmup" not in startup_block
    assert "uncollect-bridge-warmup" not in startup_block
    assert "jobs.run_forever" in startup_block


def test_topbar_does_not_show_or_poll_uncollect_bridge_status() -> None:
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
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


def test_auth_page_links_to_background_queue_with_conditional_status() -> None:
    auth = read_template("auth.html")
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")

    assert "后台队列" in auth
    assert 'href="/jobs"' in auth
    assert "job_summary.failed" in auth
    assert "job_summary.running" in auth
    assert "queue-alert" in auth
    assert "_job_queue_summary" in app_source
    assert '"job_summary": _job_queue_summary(user_id)' in app_source


def test_maintenance_center_exposes_backup_and_full_maintenance_actions() -> None:
    jobs_template = read_template("jobs.html")
    maintenance_status = read_template("_maintenance_status.html")
    restore_preview = read_template("_restore_preview.html")
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
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
    assert "uv run recall update" in maintenance_status
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
    assert 'href="/maintenance"' in base


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
    setup = read_template("setup.html") + read_template("_auth_status.html")
    setup_status = read_template("_setup_status.html")
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")

    assert "本地环境" in setup
    assert "绑定抖音账号" in setup
    assert "同步数据" in setup
    assert "生成搜索索引" in setup
    assert "完成" in setup
    assert 'hx-post="/auth/start"' in setup
    assert 'hx-post="/jobs/sync"' in setup
    assert 'hx-post="/jobs/index"' in setup
    assert 'hx-get="/setup/status"' in setup
    assert "favorites.total" in setup_status
    assert "likes.total" in setup_status
    assert '"/setup"' in app_source
    assert "get_onboarding_status" in app_source


def test_home_empty_state_links_to_setup() -> None:
    index = read_template("index.html")
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")

    assert 'href="/setup"' in index
    assert "开始设置" in index
    assert "onboarding_status" in app_source


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
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
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
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    authors = read_template("authors.html")
    base = read_template("base.html")

    assert "cached_author_avatar_url_from_raw_json" in app_source
    assert "avatar_url" in app_source
    assert "author-avatar" in authors
    assert "<img" in authors
    assert "author-avatar-fallback" in authors
    assert ".author-avatar" in base


def test_favorite_cards_render_author_avatar() -> None:
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    hybrid_source = (ROOT / "src" / "search" / "hybrid.py").read_text(encoding="utf-8")
    card = read_template("_card.html")
    base = read_template("base.html")

    assert '"author_avatar_url"' in app_source
    assert "cached_author_avatar_url_from_raw_json(_row_get(row, \"raw_json\"))" in app_source
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


def test_auth_page_has_clear_profile_refresh_and_bound_fallback_state() -> None:
    auth = read_template("auth.html") + read_template("_profile_summary.html") + read_template("_auth_status.html")

    assert 'hx-post="/auth/profile/refresh"' in auth
    assert 'hx-target="#profile-summary"' in auth
    assert "刷新账号资料" in auth
    assert ".profile-avatar[hidden] { display: none; }" in auth
    assert "已绑定抖音账号" in auth
    assert "昵称和头像还没读取到" in auth
    assert "登录态已过期" in auth
    assert "本地登录资料" in auth
    assert "可直接同步" not in auth
    assert "back-home" in auth
    assert "本地默认用户" not in auth
    assert "未读取昵称" not in auth


if __name__ == "__main__":
    tests = [
        test_uncollect_button_has_busy_state_and_duplicate_submit_guard,
        test_batch_selection_has_persistent_state_and_bulk_actions,
        test_card_layout_keeps_note_below_and_removes_inline_category_picker,
        test_uncollect_route_serializes_profile_access_and_skips_removed_items,
        test_web_startup_does_not_warm_uncollect_bridge_or_lock_profile,
        test_topbar_does_not_show_or_poll_uncollect_bridge_status,
        test_navigation_separates_modules_from_views,
        test_auth_page_links_to_background_queue_with_conditional_status,
        test_maintenance_center_exposes_backup_and_full_maintenance_actions,
        test_cli_exposes_server_lifecycle_commands_and_serve_guard,
        test_cli_exposes_diagnostic_bundle_command,
        test_setup_page_contains_first_run_sections_and_reuses_existing_endpoints,
        test_home_empty_state_links_to_setup,
        test_desktop_pagination_blocks_mobile_load_more_trigger,
        test_desktop_pagination_has_number_links_and_direct_edges,
        test_desktop_pagination_page_size_follows_grid_columns,
        test_card_meta_does_not_claim_platform_recall_history,
        test_category_page_refreshes_after_back_forward_cache_uncollect,
        test_author_page_renders_avatar_through_cache_proxy,
        test_favorite_cards_render_author_avatar,
        test_topbar_renders_account_chip_with_douyin_profile_fallback,
        test_video_modal_is_single_player_without_playlist_panel,
        test_auth_page_has_clear_profile_refresh_and_bound_fallback_state,
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
