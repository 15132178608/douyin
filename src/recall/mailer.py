"""
邮件渲染 + SMTP 发送。

- 模板：Jinja2，输出 HTML（不写纯文本 fallback，简单点）
- 发送：smtplib.SMTP_SSL，默认 163 的 465 端口
- 出错都用 loguru 记录，调用方 catch 后写 crawl_runs 类似的失败日志
"""
from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from src.config import PROJECT_ROOT, settings
from src.recall.selector import Candidate


# Jinja2 环境
_TEMPLATE_DIR = PROJECT_ROOT / "src" / "recall" / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_digest_html(
    items: Iterable[Candidate],
    total_count: int,
    anniversaries: Iterable[Candidate] | None = None,
    milestones: Iterable[Candidate] | None = None,
    content_label: str = "收藏",
) -> tuple[str, str]:
    """
    渲染 HTML 邮件。返回 (subject, html_body)。

    items：主选取的 N 条
    anniversaries：视频 N 年前这周发布的（可选，0~limit 条）
    milestones：你 30/90/180/365/730 天前收藏的（可选，0~limit 条）
    """
    items = list(items)
    anniversaries = list(anniversaries or [])
    milestones = list(milestones or [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 中性 subject——不再用"被遗忘"那种煽情词
    subject = f"📌 本周从你 {total_count} 条{content_label}里翻了 {len(items)} 条 · {today}"

    template = _jinja.get_template("digest.html.j2")
    html = template.render(
        subject=subject,
        items=items,
        anniversaries=anniversaries,
        milestones=milestones,
        total_count=total_count,
        content_label=content_label,
        generated_at=today,
    )
    return subject, html


def send_email(subject: str, html_body: str, to: str | None = None) -> None:
    """通过 SMTP_SSL 发邮件。失败抛异常。"""
    to = to or settings.mail_to
    sender = settings.mail_from or settings.smtp_user

    # 配置完整性检查
    missing = []
    if not settings.smtp_host: missing.append("SMTP_HOST")
    if not settings.smtp_user: missing.append("SMTP_USER")
    if not settings.smtp_password: missing.append("SMTP_PASSWORD")
    if not sender: missing.append("MAIL_FROM (或 SMTP_USER)")
    if not to: missing.append("MAIL_TO")
    if missing:
        raise RuntimeError(f"邮件配置不完整，请在 .env 中填写：{', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("此邮件为 HTML 格式，请用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")

    logger.info("Sending email via {}:{} to {}", settings.smtp_host, settings.smtp_port, to)

    # 163/QQ/Outlook 都建议用 SSL（465）；Gmail 用 STARTTLS（587）
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)

    logger.info("Email sent.")


def write_preview(html_body: str, out_path: Path) -> Path:
    """把渲染后的 HTML 写到文件，方便浏览器预览。用在 --dry-run 模式。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_body, encoding="utf-8")
    logger.info("Preview HTML written to {}", out_path)
    return out_path
