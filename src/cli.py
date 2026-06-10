"""
统一 CLI 入口。

用法：
    python -m src.cli --help
    python -m src.cli init-db
    python -m src.cli auth
    python -m src.cli crawl
    python -m src.cli index
    python -m src.cli digest
    python -m src.cli serve

当前命令默认走后台链路；需要排查时再显式打开调试窗口。
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
from loguru import logger

from src.config import PROJECT_ROOT, settings
from src import db as db_module


# ============================================================
# 日志初始化
# ============================================================

def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> - {message}",
    )
    try:
        logger.add(
            PROJECT_ROOT / "data" / "logs" / "recall.log",
            level="DEBUG",
            rotation="10 MB",
            retention="30 days",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("File logging disabled: {}", e)


def _console_safe(message: object, encoding: str | None = None) -> str:
    """Replace characters that the current console encoding cannot print."""
    text = str(message)
    enc = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except LookupError:
        enc = "utf-8"
    except UnicodeEncodeError:
        pass
    return text.encode(enc, errors="replace").decode(enc, errors="replace")


def _safe_echo(message: object = "", **kwargs) -> None:
    click.echo(_console_safe(message), **kwargs)


# ============================================================
# CLI
# ============================================================

@click.group()
def cli() -> None:
    """抖音收藏回忆工具 —— 把收藏夹从黑洞变成可被唤醒的资产。"""
    _setup_logging()


@cli.command("init-db")
def init_db_cmd() -> None:
    """创建数据库与所有表（幂等，重复跑安全）。"""
    db_module.init_schema()
    tables = db_module.schema_summary()
    click.echo(f"DB ready at: {settings.db_path}")
    click.echo("Tables / virtual tables:")
    for name in tables:
        click.echo(f"  - {name}")


@cli.command("create-invite")
@click.option("--code", default=None, help="可选：手动指定邀请码；不传则自动生成")
@click.option("--max-uses", default=1, show_default=True, type=int,
              help="这个邀请码最多可被领取几次")
def create_invite_cmd(code: str | None, max_uses: int) -> None:
    """[Private cloud] 创建朋友内测邀请码。"""
    from src import accounts

    db_module.init_schema()
    invite_code = accounts.create_invite(code=code, max_uses=max_uses)
    click.echo(f"邀请码：{invite_code}")
    click.echo("把这个码发给朋友，让对方在 /login 页面领取。")


@cli.command("auth")
@click.option("--timeout", default=180, show_default=True, type=int,
              help="等待手机扫码授权的最长秒数")
@click.option("--panel-timeout", default=60, show_default=True, type=int,
              help="等待抖音登录面板/二维码加载的最长秒数；VPN 慢时可调大")
@click.option("--qr-path", default=None, type=click.Path(dir_okay=False, path_type=Path),
              help="二维码截图保存路径；默认写到 data/auth/douyin-login.png")
@click.option("--visible-debug", is_flag=True, default=False,
              help="调试时展示浏览器窗口；默认后台启动，不展示抖音页面")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户的 profile")
def auth_cmd(timeout: int, panel_timeout: int, qr_path: Path | None,
             visible_debug: bool, user_id: str | None) -> None:
    """[Auth] 后台打开授权环境，保存二维码截图，等待手机扫码授权。"""
    from src.crawler.douyin import AuthQrCapture, DouyinCrawler
    from src import accounts

    profile_path = accounts.profile_path_for_user(user_id)
    screenshot_path = qr_path or (profile_path.parent / "auth" / "douyin-login.png")
    click.echo("开始扫码授权。抖音页面不会展示出来；只会保存一张二维码截图。")
    click.echo(f"二维码当前路径：{screenshot_path}")
    click.echo("每次刷新还会生成带时间戳的展示图，优先扫描最新展示图。")
    click.echo("请用抖音扫码；扫码后保持本命令运行，成功后登录态会自动保存。")
    if user_id:
        click.echo(f"用户：{user_id}  profile: {profile_path}")

    def show_qr(capture: AuthQrCapture) -> None:
        display_path = capture.display_path or capture.path
        ttl = f"{capture.ttl_seconds}s" if capture.ttl_seconds is not None else "未知"
        click.echo(f"二维码已刷新，有效期约 {ttl}：{display_path}")

    with DouyinCrawler(
        headless=False,
        api_mode=True,
        hide_window=not visible_debug,
        browser_channel="chrome",
        profile_path=profile_path,
    ) as crawler:
        result = crawler.authorize_by_qr(
            timeout_s=timeout,
            panel_timeout_s=panel_timeout,
            screenshot_path=screenshot_path,
            on_qr_capture=show_qr,
        )

    if result.screenshot_path:
        click.echo(f"最后一次二维码截图：{result.screenshot_path}")
    click.echo(result.message)
    if not result.success:
        sys.exit(1)


@cli.command("crawl")
@click.option("--headless", is_flag=True, default=False,
              help="兼容旧参数：现在默认后台运行，不需要再传 --headless")
@click.option("--visible-debug", is_flag=True, default=False,
              help="调试时展示浏览器窗口；默认后台抓取，不展示抖音页面")
@click.option("--legacy-scroll", is_flag=True, default=False,
              help="使用旧的打开收藏页+滚动监听模式。默认后台 API 翻页抓取")
@click.option("--dry-run", is_flag=True, default=False,
              help="只验证抓取链路并打印数量，不写 db、不标记 removed")
@click.option("--max-pages", default=500, show_default=True, type=int,
              help="API 模式最多翻多少页；验证链路时可配 1")
@click.option("--allow-large-removal", is_flag=True, default=False,
              help="允许一次抓取把大量本地收藏标记 removed；确认用户确实批量取消收藏时才使用")
@click.option("--max-idle", default=5, show_default=True, type=int,
              help="连续多少次滚动没新数据就停")
@click.option("--cdp", "cdp_endpoint", default=None,
              help="连接已打开的真实 Chrome（最稳，反检测过不去时用）。例：--cdp http://localhost:9222")
@click.option("--debug-xhr", is_flag=True, default=False,
              help="打印所有 aweme/collection 相关 XHR URL，排查接口路径变化")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def crawl_cmd(headless: bool, visible_debug: bool, legacy_scroll: bool, dry_run: bool,
              max_pages: int, allow_large_removal: bool, max_idle: int, cdp_endpoint: str | None,
              debug_xhr: bool, user_id: str | None) -> None:
    """[M1] 抓取抖音收藏夹，增量同步到本地 db。"""
    from datetime import datetime, timezone

    from src.crawler.douyin import DouyinCrawler
    from src.crawler.sync import SuspiciousRemovalError, apply_crawl, record_crawl_run
    from src import accounts
    from src.tenancy import normalize_user_id

    # 确保 schema 存在（init-db 没跑过也兜底）
    db_module.init_schema()

    uid = normalize_user_id(user_id)
    profile_path = accounts.profile_path_for_user(uid)

    started = datetime.now(timezone.utc)
    try:
        with DouyinCrawler(
            headless=True if headless else not visible_debug,
            max_idle_scrolls=max_idle,
            cdp_endpoint=cdp_endpoint,
            debug_xhr=debug_xhr,
            api_mode=not legacy_scroll,
            max_api_pages=max_pages,
            profile_path=profile_path,
        ) as crawler:
            favorites = crawler.crawl_collection()
    except Exception as e:
        finished = datetime.now(timezone.utc)
        if not dry_run:
            record_crawl_run(started, finished, "failed", error_message=str(e), user_id=uid)
        if settings.log_level.upper() == "DEBUG":
            logger.exception("Crawl failed: {}", e)
        else:
            logger.error("Crawl failed: {}", e)
        click.echo(f"FAILED: {e}", err=True)
        sys.exit(1)

    if not favorites:
        finished = datetime.now(timezone.utc)
        if not dry_run:
            record_crawl_run(started, finished, "partial",
                             error_message="no favorites captured", user_id=uid)
        click.echo("没拿到任何数据。可能未登录，或抖音改了接口。", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"\n[DRY RUN] 抓到 {len(favorites)} 条收藏；未写 db，未标记 removed。")
        return

    try:
        result = apply_crawl(favorites, allow_large_removal=allow_large_removal, user_id=uid)
    except SuspiciousRemovalError as e:
        finished = datetime.now(timezone.utc)
        record_crawl_run(started, finished, "failed", error_message=str(e), user_id=uid)
        click.echo(f"FAILED: {e}", err=True)
        click.echo(
            "这通常表示本次抓取不完整。确认你确实批量取消收藏后，"
            "再重新运行并加 --allow-large-removal。",
            err=True,
        )
        sys.exit(1)
    finished = datetime.now(timezone.utc)
    record_crawl_run(started, finished, "success", result, user_id=uid)

    click.echo(f"\nDone.  new={result.new_count}  updated={result.updated_count}  removed={result.removed_count}")
    click.echo(f"  Total in db: see `recall doctor` or query favorites table.")


@cli.command("crawl-likes")
@click.option("--headless", is_flag=True, default=False,
              help="兼容旧参数：现在默认后台运行，不需要再传 --headless")
@click.option("--visible-debug", is_flag=True, default=False,
              help="调试时展示浏览器窗口；默认后台抓取，不展示抖音页面")
@click.option("--dry-run", is_flag=True, default=False,
              help="只验证喜欢列表抓取链路并打印数量，不写 db、不标记 removed")
@click.option("--max-pages", default=500, show_default=True, type=int,
              help="API 模式最多翻多少页；验证链路时可配 1")
@click.option("--allow-large-removal", is_flag=True, default=False,
              help="允许一次抓取把大量本地喜欢标记 removed；确认用户确实批量取消喜欢时才使用")
@click.option("--cdp", "cdp_endpoint", default=None,
              help="连接已打开的真实 Chrome（最稳，反检测过不去时用）。例：--cdp http://localhost:9222")
@click.option("--debug-xhr", is_flag=True, default=False,
              help="打印所有 aweme/favorite/like 相关 XHR URL，排查接口路径变化")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def crawl_likes_cmd(headless: bool, visible_debug: bool, dry_run: bool,
                    max_pages: int, allow_large_removal: bool, cdp_endpoint: str | None,
                    debug_xhr: bool, user_id: str | None) -> None:
    """[Likes] 抓取抖音"我喜欢"的视频，增量同步到本地 likes 表。"""
    from datetime import datetime, timezone

    from src.crawler.douyin import DouyinCrawler
    from src.crawler.sync import (
        SuspiciousRemovalError,
        apply_like_crawl,
        record_crawl_run_for_kind,
    )
    from src import accounts
    from src.tenancy import normalize_user_id

    db_module.init_schema()

    uid = normalize_user_id(user_id)
    profile_path = accounts.profile_path_for_user(uid)

    started = datetime.now(timezone.utc)
    try:
        with DouyinCrawler(
            headless=True if headless else not visible_debug,
            cdp_endpoint=cdp_endpoint,
            debug_xhr=debug_xhr,
            api_mode=True,
            max_api_pages=max_pages,
            profile_path=profile_path,
        ) as crawler:
            likes = crawler.crawl_likes()
    except Exception as e:
        finished = datetime.now(timezone.utc)
        if not dry_run:
            record_crawl_run_for_kind("likes", started, finished, "failed",
                                      error_message=str(e), user_id=uid)
        if settings.log_level.upper() == "DEBUG":
            logger.exception("Likes crawl failed: {}", e)
        else:
            logger.error("Likes crawl failed: {}", e)
        click.echo(f"FAILED: {e}", err=True)
        sys.exit(1)

    if not likes:
        finished = datetime.now(timezone.utc)
        if not dry_run:
            record_crawl_run_for_kind("likes", started, finished, "partial",
                                      error_message="no likes captured", user_id=uid)
        click.echo("没拿到任何喜欢数据。可能未登录，或抖音改了接口。", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"\n[DRY RUN] 抓到 {len(likes)} 条喜欢；未写 db，未标记 removed。")
        return

    try:
        result = apply_like_crawl(likes, allow_large_removal=allow_large_removal, user_id=uid)
    except SuspiciousRemovalError as e:
        finished = datetime.now(timezone.utc)
        record_crawl_run_for_kind("likes", started, finished, "failed",
                                  error_message=str(e), user_id=uid)
        click.echo(f"FAILED: {e}", err=True)
        click.echo(
            "这通常表示本次抓取不完整。确认你确实批量取消喜欢后，"
            "再重新运行并加 --allow-large-removal。",
            err=True,
        )
        sys.exit(1)
    finished = datetime.now(timezone.utc)
    record_crawl_run_for_kind("likes", started, finished, "success", result, user_id=uid)

    click.echo(f"\nDone.  new={result.new_count}  updated={result.updated_count}  removed={result.removed_count}")
    click.echo("  Total in db: see `likes` table.")


@cli.command("index")
@click.option("--force", is_flag=True, default=False,
              help="重建所有索引（默认只处理新增的 favorites）")
@click.option("--batch-size", default=32, show_default=True, type=int)
@click.option("--kind", type=click.Choice(["favorites", "likes"]), default="favorites",
              show_default=True, help="要索引的内容模块")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def index_cmd(force: bool, batch_size: int, kind: str, user_id: str | None) -> None:
    """[M3] 为新条目生成 embedding 并写入 vec / FTS 索引。"""
    from src.embedding.indexer import index_all
    from src.content.kinds import get_content_kind
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    content = get_content_kind(kind)
    uid = normalize_user_id(user_id)
    click.echo(f"开始索引{content.label}（首次会下载 bge-m3 模型 ~2.3GB，请耐心等）...")
    stats = index_all(batch_size=batch_size, force=force, content_kind=content.key, user_id=uid)
    click.echo(f"\nDone. Indexed {stats['indexed']} this run.")
    click.echo(f"Total indexed in db: {stats['total_in_db']}")


@cli.command("search")
@click.argument("query", required=True)
@click.option("--top", default=10, show_default=True, type=int)
@click.option("--kind", type=click.Choice(["favorites", "likes"]), default="favorites",
              show_default=True, help="要搜索的内容模块")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def search_cmd(query: str, top: int, kind: str, user_id: str | None) -> None:
    """[M3] 命令行测搜索效果（最终用户用 serve 起 Web UI）。"""
    from src.search.hybrid import search_for_kind
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    uid = normalize_user_id(user_id)
    hits = search_for_kind(query, top_k=top, content_kind=kind, user_id=uid)
    if not hits:
        _safe_echo("没找到。可能：1) 没跑过 index；2) 词太特殊；3) 数据里没有")
        return
    _safe_echo(f"\n找到 {len(hits)} 条：\n")
    for i, h in enumerate(hits, 1):
        title = (h.title or "")[:50]
        marker = "VF" if (h.vec_rank and h.fts_rank) else ("V" if h.vec_rank else "F")
        _safe_echo(f"{i:>2}. [{marker}] @{h.author or '?'}: {title}")
        _safe_echo(f"     {h.video_url or ''}")
        _safe_echo(f"     score={h.score:.4f}  vec={h.vec_rank}  fts={h.fts_rank}")


@cli.command("digest")
@click.option("--count", default=None, type=int,
              help="主区块推几条（默认读 .env 的 DIGEST_COUNT，没配就 6）。回忆角是额外的。")
@click.option("--ignore-warmup", is_flag=True, default=False,
              help="忽略 warmup 限制——首次跑 digest 必须加这个，否则没候选")
@click.option("--dry-run", is_flag=True, default=False,
              help="只渲染 HTML 写到本地预览，不真的发邮件、不更新 last_recalled_at")
@click.option("--seed", default=None, type=int, help="固定随机种子，方便复现")
@click.option("--no-anniversary", is_flag=True, default=False,
              help="关掉「回忆角」小板块（默认是开的）")
@click.option("--kind", type=click.Choice(["favorites", "likes"]), default="favorites",
              show_default=True, help="要召回的内容模块")
@click.option("--theme", default=None,
              help="主题周报：只从标题/作者/备注里包含该主题词的候选中挑主区块")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def digest_cmd(count: int | None, ignore_warmup: bool, dry_run: bool, seed: int | None,
               no_anniversary: bool, kind: str, theme: str | None, user_id: str | None) -> None:
    """[M2] 选取本周可看的几条收藏/喜欢，加上"回忆角"，渲染并发送邮件。"""
    from src.content.kinds import get_content_kind
    from src.recall import selector, mailer
    from src.tenancy import normalize_user_id

    db_module.init_schema()

    content = get_content_kind(kind)
    n = count or settings.digest_count
    uid = normalize_user_id(user_id)

    # 先挑回忆角（小集合，固定优先），再挑主区块（排除已选）
    if no_anniversary:
        anniversaries: list = []
        milestones: list = []
    else:
        anniversaries = selector.pick_anniversary(
            limit=1, seed=seed, content_kind=content.key, user_id=uid)
        ann_ids = {c.id for c in anniversaries}
        milestones = selector.pick_milestone(
            limit=1,
            exclude_ids=ann_ids,
            seed=seed,
            content_kind=content.key,
            user_id=uid,
        )

    excluded_ids = {c.id for c in anniversaries} | {c.id for c in milestones}
    main_picks = selector.pick(
        count=n,
        ignore_warmup=ignore_warmup,
        seed=seed,
        exclude_ids=excluded_ids,
        content_kind=content.key,
        user_id=uid,
        theme=theme,
    )

    all_picks = main_picks + anniversaries + milestones
    if not all_picks:
        click.echo(
            "候选池为空。可能原因：\n"
            "  1) 首次跑 digest？加 --ignore-warmup\n"
            f"  2) 你最近所有{content.label}都被推过了（cooldown_days 内）\n"
            f"  3) db 是空的，先跑 {'crawl' if content.key == 'favorites' else 'crawl-likes'}"
        )
        sys.exit(0)

    # 统计总数（首部小标题用）
    conn = db_module.get_connection()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM {content.table} WHERE user_id = ? AND is_removed=0",
        (uid,),
    ).fetchone()["c"]

    subject, html = mailer.render_digest_html(
        main_picks,
        total_count=total,
        anniversaries=anniversaries,
        milestones=milestones,
        content_label=content.label,
    )

    if dry_run:
        from pathlib import Path
        preview_name = "digest_preview.html" if content.key == "favorites" else f"digest_preview_{content.key}.html"
        preview_path = mailer.write_preview(
            html,
            PROJECT_ROOT / "data" / preview_name,
        )
        theme_label = f"（主题：{theme}）" if theme else ""
        click.echo(f"\n[DRY RUN] 主区块 {len(main_picks)} 条{theme_label}：")
        for c in main_picks:
            click.echo(f"  - @{c.author} | {(c.title or '')[:50]}")
        if anniversaries:
            click.echo(f"\n[回忆角] 周年提醒 {len(anniversaries)} 条：")
            for c in anniversaries:
                click.echo(f"  - [{c.anniversary_years}年前发布] @{c.author} | {(c.title or '')[:50]}")
        if milestones:
            click.echo(f"\n[回忆角] 里程碑 {len(milestones)} 条：")
            for c in milestones:
                click.echo(f"  - [{c.milestone_days}天前{content.label}] @{c.author} | {(c.title or '')[:50]}")
        click.echo(f"\n预览 HTML：{preview_path}")
        click.echo("可手动打开这个本地 HTML 预览；命令不会自动打开浏览器。不发邮件，db 也不动。")
        return

    # 真发邮件
    try:
        mailer.send_email(subject, html)
    except Exception as e:
        logger.exception("Send failed: {}", e)
        click.echo(f"邮件发送失败：{e}", err=True)
        sys.exit(1)

    # 发成功 → 主区块 + 回忆角全都写 recall log + 更新 last_recalled_at
    selector.mark_recalled(
        [c.id for c in all_picks], channel="weekly_digest",
        content_kind=content.key, user_id=uid,
    )
    click.echo(
        f"\n邮件已发往 {settings.mail_to}："
        f"主 {len(main_picks)} + 周年 {len(anniversaries)} + 里程碑 {len(milestones)} = {len(all_picks)} 条。"
    )


@cli.command("export")
@click.option("--format", "export_format",
              type=click.Choice(["json", "markdown", "sqlite", "all"]),
              default="all", show_default=True,
              help="导出格式：JSON、Markdown、SQLite 备份或全部")
@click.option("--kind", type=click.Choice(["favorites", "likes", "all"]),
              default="all", show_default=True,
              help="导出收藏、喜欢或两者")
@click.option("--output", "output_dir", default=PROJECT_ROOT / "data" / "exports",
              type=click.Path(file_okay=False, path_type=Path),
              show_default=True, help="导出目录")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def export_cmd(export_format: str, kind: str, output_dir: Path, user_id: str | None) -> None:
    """导出 JSON / Markdown，或生成 SQLite 备份文件。"""
    from src import exporter
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    uid = normalize_user_id(user_id)
    formats = ["json", "markdown", "sqlite"] if export_format == "all" else [export_format]
    kinds = ["favorites", "likes"] if kind == "all" else [kind]
    results = []
    for fmt in formats:
        if fmt == "sqlite":
            results.append(("sqlite", exporter.backup_sqlite(output_dir)))
            continue
        for content_kind in kinds:
            if fmt == "json":
                results.append((f"{content_kind}/json", exporter.export_json(
                    output_dir, user_id=uid, content_kind=content_kind,
                )))
            elif fmt == "markdown":
                results.append((f"{content_kind}/markdown", exporter.export_markdown(
                    output_dir, user_id=uid, content_kind=content_kind,
                )))
    for label, result in results:
        click.echo(f"{label}: {result.count} 条 -> {result.path}")


@cli.command("tag")
@click.argument("item_ids", nargs=-1)
@click.option("--kind", type=click.Choice(["favorites", "likes"]), default="favorites",
              show_default=True, help="要打标签的内容模块")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
@click.option("--max-tags", default=5, show_default=True, type=int,
              help="每条最多生成多少个二级标签")
@click.option("--provider", type=click.Choice(["local", "ollama"]), default="local",
              show_default=True, help="标签生成方式；ollama 会调用本机 Ollama LLM")
@click.option("--model", default=None, help="Ollama 模型名，例如 qwen2.5:7b")
def tag_cmd(item_ids: tuple[str, ...], kind: str, user_id: str | None,
            max_tags: int, provider: str, model: str | None) -> None:
    """给指定条目生成二级标签，结果写入 llm_tags 字段。"""
    from src.content.kinds import get_content_kind
    from src.tagging.llm_tags import suggest_second_level_tags, write_tags
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    content = get_content_kind(kind)
    uid = normalize_user_id(user_id)
    if not item_ids:
        raise click.UsageError("至少传一个条目 id")
    conn = db_module.get_connection()
    for item_id in item_ids:
        row = conn.execute(
            f"""
            SELECT title, author, user_note, video_tags
            FROM {content.table}
            WHERE user_id = ? AND id = ?
            """,
            (uid, item_id),
        ).fetchone()
        if row is None:
            click.echo(f"{item_id}: not found", err=True)
            continue
        text = " ".join(str(row[k] or "") for k in ("title", "author", "user_note", "video_tags"))
        tags = suggest_second_level_tags(text, max_tags=max_tags, provider=provider, model=model)
        write_tags(item_id, tags, user_id=uid, content_kind=content.key)
        click.echo(f"{item_id}: {', '.join(tags) if tags else '(no tags)'}")


@cli.command("repair-favorited-at")
@click.option("--dry-run", is_flag=True, default=False,
              help="只看会改多少行，不真改")
@click.option("--threshold-seconds", default=3600, type=int, show_default=True,
              help="favorited_at 与 first_seen_at 相差小于多少秒视为「sync 错填」，重置为 NULL")
def repair_favorited_at_cmd(dry_run: bool, threshold_seconds: int) -> None:
    """
    [一次性修复] 把被 sync 错填的 favorited_at 重置回 NULL。

    背景：partial-first-crawl bug——上次抓取只拿到一部分，这次拿全；本来应该
    都视为"首抓未知时间"，结果新条目被打成 NOW()。这条命令把那些误填的清回 NULL。

    判定：favorited_at 与 first_seen_at 相差 < threshold 秒（默认 1 小时）
    """
    db_module.init_schema()
    conn = db_module.get_connection()

    rows = conn.execute(
        """
        SELECT COUNT(*) AS c FROM favorites
        WHERE favorited_at IS NOT NULL
          AND first_seen_at IS NOT NULL
          AND ABS(julianday(favorited_at) - julianday(first_seen_at)) * 86400 < ?
        """,
        (threshold_seconds,),
    ).fetchone()
    count = rows["c"]

    click.echo(f"识别到 {count} 条 favorited_at ≈ first_seen_at（差距 < {threshold_seconds}s）的记录")
    click.echo("这些大概率是 partial-first-crawl bug 的产物，本该是 NULL")

    if count == 0:
        click.echo("无需修复。")
        return

    if dry_run:
        click.echo("[DRY RUN] 不真改。去掉 --dry-run 再跑一次执行修复。")
        return

    res = conn.execute(
        """
        UPDATE favorites
        SET favorited_at = NULL
        WHERE favorited_at IS NOT NULL
          AND first_seen_at IS NOT NULL
          AND ABS(julianday(favorited_at) - julianday(first_seen_at)) * 86400 < ?
        """,
        (threshold_seconds,),
    )
    click.echo(f"已重置 {res.rowcount} 条 favorited_at 为 NULL")


@cli.command("uncollect")
@click.argument("aweme_ids", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, default=False,
              help="只验证后台 API 模式和登录态，不发送取消收藏请求、不改 db")
@click.option("--cdp", default=None,
              help="可选：连到你已打开的 Chrome 的 CDP 端点；不传则复用 recall auth 的后台 profile")
@click.option("--visible-debug", is_flag=True, default=False,
              help="调试时展示浏览器窗口；默认后台执行，不展示抖音页面")
@click.option("--page-fallback", is_flag=True, default=False,
              help="API 失败时才打开视频详情页点按钮。默认关闭，工具模式不打开视频页")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def uncollect_cmd(aweme_ids: tuple[str, ...], dry_run: bool, cdp: str | None,
                  visible_debug: bool, page_fallback: bool, user_id: str | None) -> None:
    """[Uncollect] 后台调用抖音 Web API 取消收藏。可一次传多个 aweme_id。"""
    from datetime import datetime, timezone

    from src.uncollector.douyin import uncollect_many
    from src import accounts
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    conn = db_module.get_connection()
    uid = normalize_user_id(user_id)
    profile_path = accounts.profile_path_for_user(uid)

    targets = []
    for aweme_id in aweme_ids:
        row = conn.execute(
            "SELECT id, title, video_url, is_removed FROM favorites WHERE user_id = ? AND id = ?",
            (uid, aweme_id),
        ).fetchone()
        if row is None:
            click.echo(f"目标 {aweme_id}: db 里没有这条，但仍会尝试在抖音上取消")
        else:
            title = (row["title"] or "")[:50]
            if row["is_removed"]:
                click.echo(f"目标 {aweme_id}: [WARN] 本地 db 已标记 removed: {title}")
            else:
                click.echo(f"目标 {aweme_id}: {title}")

        log_id = None
        if not dry_run:
            cur = conn.execute(
                "INSERT INTO uncollect_log (user_id, favorite_id, initiated_at, status, channel) "
                "VALUES (?, ?, ?, 'pending', 'cli')",
                (uid, aweme_id, datetime.now(timezone.utc)),
            )
            log_id = cur.lastrowid
        targets.append((aweme_id, row, log_id))

    results = uncollect_many(
        list(aweme_ids),
        cdp_endpoint=cdp,
        dry_run=dry_run,
        allow_page_fallback=page_fallback,
        headless=not visible_debug,
        hide_window=not visible_debug,
        browser_channel="chrome",
        profile_path=profile_path,
    )

    had_failure = False
    for (aweme_id, row, log_id), result in zip(targets, results):
        click.echo(f"\n[{aweme_id}] 结果: {'成功' if result.success else '失败'}")
        click.echo(f"  消息: {result.message}")
        if result.already_uncollected:
            click.echo("  备注: 抖音端本来就是未收藏状态")

        if dry_run:
            continue

        if log_id is not None:
            finished = datetime.now(timezone.utc)
            conn.execute(
                "UPDATE uncollect_log SET status = ?, finished_at = ?, error_message = ? "
                "WHERE id = ? AND user_id = ?",
                ("success" if result.success else "failed", finished,
                 None if result.success else result.message, log_id, uid),
            )
        if result.success and row is not None:
            conn.execute(
                "UPDATE favorites SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
                (datetime.now(timezone.utc), uid, aweme_id),
            )
            click.echo("  [OK] 本地 db 标记 is_removed=1")
        if not result.success:
            had_failure = True

    if dry_run:
        click.echo("\n[DRY RUN] 没真改 db、没发取消收藏请求。")
        return
    if had_failure:
        sys.exit(1)


@cli.command("unlike")
@click.argument("aweme_ids", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, default=False,
              help="只验证后台 API 模式和登录态，不发送取消喜欢请求、不改 db")
@click.option("--cdp", default=None,
              help="可选：连到你已打开的 Chrome 的 CDP 端点；不传则复用 recall auth 的后台 profile")
@click.option("--visible-debug", is_flag=True, default=False,
              help="调试时展示浏览器窗口；默认后台执行，不展示抖音页面")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def unlike_cmd(aweme_ids: tuple[str, ...], dry_run: bool, cdp: str | None,
               visible_debug: bool, user_id: str | None) -> None:
    """[Unlike] 后台调用抖音 Web API 取消喜欢。可一次传多个 aweme_id。"""
    from datetime import datetime, timezone

    from src.uncollector.douyin import unlike_many
    from src import accounts
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    conn = db_module.get_connection()
    uid = normalize_user_id(user_id)
    profile_path = accounts.profile_path_for_user(uid)

    targets = []
    for aweme_id in aweme_ids:
        row = conn.execute(
            "SELECT id, title, video_url, is_removed FROM likes WHERE user_id = ? AND id = ?",
            (uid, aweme_id),
        ).fetchone()
        if row is None:
            click.echo(f"目标 {aweme_id}: db 里没有这条喜欢，但仍会尝试在抖音上取消")
        else:
            title = (row["title"] or "")[:50]
            if row["is_removed"]:
                click.echo(f"目标 {aweme_id}: [WARN] 本地 likes 已标记 removed: {title}")
            else:
                click.echo(f"目标 {aweme_id}: {title}")

        log_id = None
        if not dry_run:
            cur = conn.execute(
                "INSERT INTO unlike_log (user_id, like_id, initiated_at, status, channel) "
                "VALUES (?, ?, ?, 'pending', 'cli')",
                (uid, aweme_id, datetime.now(timezone.utc)),
            )
            log_id = cur.lastrowid
        targets.append((aweme_id, row, log_id))

    results = unlike_many(
        list(aweme_ids),
        cdp_endpoint=cdp,
        dry_run=dry_run,
        headless=not visible_debug,
        hide_window=not visible_debug,
        browser_channel="chrome",
        profile_path=profile_path,
    )

    had_failure = False
    for (aweme_id, row, log_id), result in zip(targets, results):
        click.echo(f"\n[{aweme_id}] 结果: {'成功' if result.success else '失败'}")
        click.echo(f"  消息: {result.message}")

        if dry_run:
            continue

        if log_id is not None:
            finished = datetime.now(timezone.utc)
            conn.execute(
                "UPDATE unlike_log SET status = ?, finished_at = ?, error_message = ? "
                "WHERE id = ? AND user_id = ?",
                ("success" if result.success else "failed", finished,
                 None if result.success else result.message, log_id, uid),
            )
        if result.success and row is not None:
            conn.execute(
                "UPDATE likes SET is_removed = 1, last_seen_at = ? WHERE user_id = ? AND id = ?",
                (datetime.now(timezone.utc), uid, aweme_id),
            )
            click.echo("  [OK] 本地 likes 标记 is_removed=1")
        if not result.success:
            had_failure = True

    if dry_run:
        click.echo("\n[DRY RUN] 没真改 db、没发取消喜欢请求。")
        return
    if had_failure:
        sys.exit(1)


@cli.command("backfill-raw")
@click.option("--dry-run", is_flag=True, default=False,
              help="只打印会改多少行，不真的写")
def backfill_raw_cmd(dry_run: bool) -> None:
    """
    [一次性] 从现有 favorites.raw_json 反填 video_created_at + digg_count。

    给历史上抓的 973 条用——它们的 raw_json 是完整的，但当时 parser 还没抽
    video_created_at / digg_count 这两个字段。跑一次就够。
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    db_module.init_schema()
    conn = db_module.get_connection()

    rows = conn.execute(
        "SELECT id, raw_json, video_created_at, digg_count "
        "FROM favorites WHERE raw_json IS NOT NULL"
    ).fetchall()
    click.echo(f"扫描 {len(rows)} 条带 raw_json 的记录…")

    updated = 0
    skipped_no_raw = 0
    already_filled = 0
    for r in rows:
        try:
            item = _json.loads(r["raw_json"])
        except Exception:
            skipped_no_raw += 1
            continue

        # 抽 create_time
        new_created_at = None
        ts = item.get("create_time")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                new_created_at = _dt.fromtimestamp(int(ts), tz=_tz.utc)
            except (OSError, ValueError, OverflowError):
                new_created_at = None

        # 抽 digg_count
        new_digg = item.get("statistics", {}).get("digg_count") if isinstance(item.get("statistics"), dict) else None
        if not isinstance(new_digg, int):
            new_digg = None

        # 如果俩都已经有值，跳过
        if r["video_created_at"] is not None and r["digg_count"] is not None:
            already_filled += 1
            continue

        if dry_run:
            updated += 1
            continue

        conn.execute(
            "UPDATE favorites SET "
            "  video_created_at = COALESCE(video_created_at, ?), "
            "  digg_count       = COALESCE(digg_count, ?) "
            "WHERE id = ?",
            (new_created_at, new_digg, r["id"]),
        )
        updated += 1

    click.echo(f"\n{'[DRY RUN] ' if dry_run else ''}填充 {updated} 行")
    click.echo(f"  已有完整字段（跳过）：{already_filled}")
    click.echo(f"  raw_json 损坏（跳过）：{skipped_no_raw}")


@cli.command("categorize")
@click.option("--algo", type=click.Choice(["kmeans", "hdbscan"]), default="kmeans",
              show_default=True,
              help="聚类算法。kmeans + silhouette 自动选 K（默认，真实数据上效果更好）；hdbscan 密度自适应但容易出'大杂烩+一堆未分类'")
@click.option("--k", "force_k", type=int, default=None,
              help="仅 kmeans：强制 K。不填则 silhouette 自动选")
@click.option("--rebuild", is_flag=True, default=False,
              help="无差别重聚（默认就是重聚，留这个 flag 是为了将来增量逻辑加进来后明确意图）")
@click.option("--kind", type=click.Choice(["favorites", "likes"]), default="favorites",
              show_default=True, help="要分类的内容模块")
@click.option("--user", "user_id", default=None,
              help="指定用户 ID；不传则走 default 用户")
def categorize_cmd(algo: str, force_k: int | None, rebuild: bool, kind: str,
                   user_id: str | None) -> None:
    """[M5] 自动给收藏分类。先跑过 index 才有意义。"""
    from src.content.kinds import get_content_kind
    from src.categorize import cluster as cluster_mod
    from src.tenancy import normalize_user_id

    db_module.init_schema()
    content = get_content_kind(kind)
    uid = normalize_user_id(user_id)
    click.echo(f"开始聚类{content.label}（algo={algo}）...")
    result = cluster_mod.categorize_all(algo=algo, force_k=force_k, content_kind=content.key,
                                        account_id=uid)

    if result.skipped_reason:
        click.echo(f"\n跳过：{result.skipped_reason}")
        return

    click.echo(f"\n=== 聚类完成 ===")
    click.echo(f"  总条目：{result.total_items}")
    click.echo(f"  已归类：{result.clustered_items}")
    click.echo(f"  未分类（噪声/不像任何一类）：{result.noise_items}")
    click.echo(f"  类别数：{result.n_clusters}")
    click.echo(f"  算法：{result.algo}")
    if result.auto_k is not None:
        click.echo(f"  自动选 K：{result.auto_k}")

    click.echo("\n--- 类别一览 ---")
    cats = cluster_mod.list_categories(content_kind=content.key, account_id=uid)
    for c in cats:
        kws = ", ".join(c["keywords"][:5]) if c["keywords"] else "—"
        click.echo(f"  [#{c['id']}] {c['name']}  ({c['item_count']} 条)  关键词: {kws}")
    if result.noise_items:
        click.echo(f"  [未分类] {result.noise_items} 条（HDBSCAN 噪声/不显著归属任何簇）")


@cli.command("serve")
@click.option("--host", default=None,
              help="监听地址；默认读 .env 的 WEB_HOST（未配置则 127.0.0.1）。对外开放用 0.0.0.0")
@click.option("--port", default=None, type=int,
              help="端口；默认读 .env 的 WEB_PORT（未配置则 8000）")
def serve_cmd(host: str | None, port: int | None) -> None:
    """[M3] 启动 Web UI（搜索 + 时间轴）。"""
    import uvicorn

    db_module.init_schema()
    h = host or settings.web_host
    p = port or settings.web_port
    click.echo(f"\n启动 Web UI: http://{h}:{p}")
    click.echo("  · 浏览器打开上面这个地址")
    click.echo("  · 第一次搜索时会加载 bge-m3 模型（~20 秒）")
    click.echo("  · Ctrl+C 停止\n")
    uvicorn.run("src.web.app:app", host=h, port=p, log_level="info")


@cli.command("doctor")
def doctor_cmd() -> None:
    """检查环境是否就绪：依赖、配置、db、Playwright profile。"""
    click.echo("=== Environment Doctor ===")
    click.echo(f"Project root:       {PROJECT_ROOT}")
    click.echo(f"DB path:            {settings.db_path}")
    click.echo(f"  exists:           {settings.db_path.exists()}")
    click.echo(f"Playwright profile: {settings.playwright_profile_path}")
    click.echo(f"  exists:           {settings.playwright_profile_path.exists()}")
    click.echo(f"SMTP host:          {settings.smtp_host or '(not set — M2 will fail)'}")
    click.echo(f"Mail to:            {settings.mail_to or '(not set — M2 will fail)'}")

    # 检查关键依赖
    deps_status = {}
    for mod in ("sqlite_vec", "playwright", "sentence_transformers", "jieba",
                "fastapi", "loguru", "pydantic_settings"):
        try:
            __import__(mod)
            deps_status[mod] = "OK"
        except ImportError as e:
            deps_status[mod] = f"MISSING ({e})"
    click.echo("Dependencies:")
    for mod, status in deps_status.items():
        click.echo(f"  - {mod}: {status}")


if __name__ == "__main__":
    cli()
