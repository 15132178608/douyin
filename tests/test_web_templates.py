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
