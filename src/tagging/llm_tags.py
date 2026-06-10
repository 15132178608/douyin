"""Second-level tag suggestion and storage.

This module provides a local deterministic fallback today and keeps the storage
shape ready for an external LLM provider later.
"""
from __future__ import annotations

import json
import re

import httpx

from src.content.kinds import get_content_kind
from src.db import get_connection
from src.tenancy import DEFAULT_USER_ID, normalize_user_id


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9_+-]{2,}")
_STOPWORDS = {
    "这个", "那个", "视频", "收藏", "喜欢", "作者", "一个", "一些",
    "the", "and", "for", "with",
}


def _clean_tags(values: list[object], max_tags: int) -> list[str]:
    tags: list[str] = []
    for value in values:
        token = str(value).strip()
        if not token:
            continue
        token = token.lower() if token.isascii() else token
        if token in _STOPWORDS or token in tags:
            continue
        tags.append(token)
        if len(tags) >= max(1, int(max_tags or 1)):
            break
    return tags


def _suggest_local(text: str, max_tags: int) -> list[str]:
    """Return short, stable tags from text as a local fallback tagger."""
    values: list[str] = []
    for match in _TOKEN_RE.findall(text or ""):
        token = match.strip().lower() if match.isascii() else match.strip()
        if not token:
            continue
        values.append(token)
    return _clean_tags(values, max_tags)


def _suggest_ollama(
    text: str,
    *,
    max_tags: int,
    model: str,
    endpoint: str,
) -> list[str]:
    prompt = (
        "给下面这条抖音收藏生成二级标签。"
        f"只返回 JSON，格式为 {{\"tags\":[...]}}，最多 {max_tags} 个，标签要短。\n\n"
        f"{text}"
    )
    resp = httpx.post(
        f"{endpoint.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=60.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    raw = payload.get("response", "")
    try:
        parsed = json.loads(raw)
        values = parsed.get("tags", [])
    except (TypeError, ValueError, AttributeError):
        values = re.split(r"[,，\s]+", str(raw))
    return _clean_tags(list(values), max_tags)


def suggest_second_level_tags(
    text: str,
    *,
    max_tags: int = 5,
    provider: str = "local",
    model: str | None = None,
    endpoint: str = "http://127.0.0.1:11434",
) -> list[str]:
    """Suggest short second-level tags using local fallback or an Ollama LLM."""
    if provider == "ollama":
        return _suggest_ollama(
            text,
            max_tags=max_tags,
            model=model or "qwen2.5:7b",
            endpoint=endpoint,
        )
    return _suggest_local(text, max_tags)


def write_tags(
    item_id: str,
    tags: list[str],
    *,
    user_id: str = DEFAULT_USER_ID,
    content_kind: str = "favorites",
) -> bool:
    kind = get_content_kind(content_kind)
    uid = normalize_user_id(user_id)
    clean = [t.strip() for t in tags if t and t.strip()]
    conn = get_connection()
    res = conn.execute(
        f"UPDATE {kind.table} SET llm_tags = ? WHERE user_id = ? AND id = ?",
        (json.dumps(clean, ensure_ascii=False), uid, item_id),
    )
    return res.rowcount > 0
