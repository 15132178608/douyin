# Login Recovery Design

## Context

Douyin Recall already has a QR login flow at `/auth`, a first-run setup flow at `/setup`, and sync jobs that surface failed crawl attempts in `/maintenance`. The remaining long-term-use gap is recovery after Douyin cookies expire: the user can see failed jobs, but the maintenance center does not clearly say that the likely fix is to rebind the Douyin account.

## Goals

- Detect likely Douyin login expiration from recent failed sync jobs and crawl runs.
- Show a clear recovery card in `/maintenance` with a link to `/auth`.
- Add a Windows Start Menu entry that opens account recovery directly.
- Reuse the existing QR login flow and profile refresh behavior.
- Keep this read-only until the user explicitly starts binding from `/auth`.

## Non-Goals

- Do not delete or reset Playwright profile directories.
- Do not automatically start QR login from `/maintenance` or the Windows shortcut.
- Do not build a new login flow separate from `/auth`.
- Do not expose the local web UI publicly.

## Design

Add a read-only auth recovery summary to `maintenance.get_maintenance_status()`.

The summary inspects:

- latest failed `crawl_runs` and `like_crawl_runs`
- recent failed `job_queue` items for sync and cleanup work
- saved Douyin profile fields from the current user row
- whether the local Playwright profile directory exists

If an error message contains common auth markers such as `用户未登录`, `登录态失效`, `请登录`, `login required`, or `not login`, the summary sets:

```python
auth = {
    "status": "expired",
    "needs_rebind": True,
    "recovery_url": "/auth",
    "latest_error": {...},
    ...
}
```

If no auth failure is visible, the status is `bound` when local login/profile evidence exists, otherwise `missing`.

The maintenance template adds a compact `抖音登录` card. When `needs_rebind` is true, it shows `登录态可能过期`, a short error snippet, and a `重新绑定抖音账号` link to `/auth`.

The Windows control script adds an `auth` action and a `Douyin Recall Account Recovery` shortcut. This action opens `/auth` if the local web service is reachable, otherwise it starts the normal launcher with `-OpenPath "/auth"`.

## Testing

- Maintenance tests cover auth-expired detection from crawl-run and job errors.
- Template tests cover the new maintenance card and `/auth` recovery link.
- Windows packaging tests cover the `auth` control action and Start Menu shortcut.
- Release verification installs the packaged build and checks that the account recovery shortcut and control action exist.
