"""Web authentication security helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
from urllib.parse import unquote, urlsplit

from fastapi import Request, Response

from src.config import settings
from src.db import get_connection


MAX_REDIRECT_TARGET_LENGTH = 4096
MAX_REDIRECT_DECODE_ROUNDS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _redirect_probe_is_safe(probe: str) -> bool:
    if any(ord(char) < 32 or ord(char) == 127 for char in probe):
        return False
    if not probe.startswith("/") or probe.startswith("//"):
        return False
    try:
        parsed = urlsplit(probe)
    except ValueError:
        return False
    return not parsed.scheme and not parsed.netloc and "\\" not in parsed.path


def safe_local_redirect_target(value: str | None) -> str:
    """Return a local absolute path, or ``/`` for external/ambiguous targets."""
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > MAX_REDIRECT_TARGET_LENGTH:
        return "/"

    probe = candidate
    for _ in range(MAX_REDIRECT_DECODE_ROUNDS):
        if not _redirect_probe_is_safe(probe):
            return "/"
        try:
            decoded = unquote(probe, errors="strict")
        except UnicodeDecodeError:
            return "/"
        if decoded == probe:
            return candidate
        probe = decoded
    return "/"


def login_client_ip(request: Request) -> str:
    """Return the direct peer IP; forwarded headers are intentionally untrusted."""
    raw = str(request.client.host if request.client else "unknown").strip()
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw[:128] or "unknown"


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_web_security_config(*, host: str | None = None) -> None:
    """Refuse an authenticated public bind with insecure cookies.

    ``host`` is the effective server bind when a caller overrides WEB_HOST.
    """
    effective_host = settings.web_host if host is None else host
    if (
        settings.web_auth_required
        and not _is_loopback_host(effective_host)
        and not settings.session_cookie_secure
    ):
        raise RuntimeError(
            "WEB_AUTH_REQUIRED on a non-loopback host requires SESSION_COOKIE_SECURE=true "
            "and HTTPS termination."
        )


def _subject_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rate_limit_subjects(client_ip: str, invite_code: str) -> tuple[tuple[str, str], ...]:
    normalized_code = (invite_code or "").strip()
    return (
        ("ip", _subject_hash(client_ip)),
        ("ip_invite", _subject_hash(f"{client_ip}\0{normalized_code}")),
    )


def login_retry_after(client_ip: str, invite_code: str, *, now: datetime | None = None) -> int:
    current = now or _now()
    conn = get_connection()
    retry_after = 0
    for scope, subject_hash in _rate_limit_subjects(client_ip, invite_code):
        row = conn.execute(
            """
            SELECT blocked_until
            FROM login_rate_limits
            WHERE scope = ? AND subject_hash = ?
            """,
            (scope, subject_hash),
        ).fetchone()
        blocked_until = _as_datetime(row["blocked_until"]) if row else None
        if blocked_until and blocked_until > current:
            retry_after = max(retry_after, int((blocked_until - current).total_seconds()) + 1)
    return retry_after


def record_login_failure(
    client_ip: str,
    invite_code: str,
    *,
    now: datetime | None = None,
) -> int:
    current = now or _now()
    window_seconds = max(1, int(settings.login_rate_limit_window_seconds))
    max_attempts = max(1, int(settings.login_rate_limit_max_attempts))
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for scope, subject_hash in _rate_limit_subjects(client_ip, invite_code):
            row = conn.execute(
                """
                SELECT window_started_at, failed_count
                FROM login_rate_limits
                WHERE scope = ? AND subject_hash = ?
                """,
                (scope, subject_hash),
            ).fetchone()
            window_started = _as_datetime(row["window_started_at"]) if row else None
            if window_started is None or current >= window_started + timedelta(seconds=window_seconds):
                window_started = current
                failed_count = 1
            else:
                failed_count = int(row["failed_count"] or 0) + 1
            blocked_until = (
                window_started + timedelta(seconds=window_seconds)
                if failed_count >= max_attempts
                else None
            )
            conn.execute(
                """
                INSERT INTO login_rate_limits (
                    scope, subject_hash, window_started_at, failed_count,
                    blocked_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, subject_hash) DO UPDATE SET
                    window_started_at = excluded.window_started_at,
                    failed_count = excluded.failed_count,
                    blocked_until = excluded.blocked_until,
                    updated_at = excluded.updated_at
                """,
                (scope, subject_hash, window_started, failed_count, blocked_until, current),
            )
        conn.execute(
            "DELETE FROM login_rate_limits WHERE updated_at < ?",
            (current - timedelta(seconds=window_seconds * 4),),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return login_retry_after(client_ip, invite_code, now=current)


def clear_login_failures(client_ip: str, invite_code: str) -> None:
    conn = get_connection()
    for scope, subject_hash in _rate_limit_subjects(client_ip, invite_code):
        conn.execute(
            "DELETE FROM login_rate_limits WHERE scope = ? AND subject_hash = ?",
            (scope, subject_hash),
        )


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
