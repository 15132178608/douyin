# Changelog

## Unreleased

- Windows startup and `Douyin Recall Prepare Runtime` now print coarse step progress, so first-run dependency setup shows which step is running and which steps may take several minutes.

## v0.1.18

- Startup failures now print the failed stage, likely cause, and recommended next step in the Windows launcher.
- `Douyin Recall Prepare Runtime` now reports the failed preparation step before returning an error, making `uv sync`, Playwright, database, and status-check failures easier to retry.
- Failure output points users to logs, `uv run recall diagnose`, runtime cache location, Health Check, Repair State, and the safe Prepare Runtime retry entry.

See [docs/releases/v0.1.18.md](docs/releases/v0.1.18.md).

## v0.1.17

- Added `Douyin Recall Prepare Runtime` for installed Windows builds so first-launch dependency setup can be retried without starting the Web service.
- The prepare action locates or installs `uv`, runs `uv sync`, installs Playwright Chromium, initializes SQLite, and prints service status.
- Runtime downloads and caches still stay under `D:\codexDownload\douyinclaude-runtime`.

See [docs/releases/v0.1.17.md](docs/releases/v0.1.17.md).

## v0.1.16

- Added a service lifecycle audit to `recall status` so forgotten background Web services show the recorded PID, port owner PID, and safest next step.
- Added the same service-audit guidance to `Douyin Recall Control` and `Douyin Recall Health Check`.
- The audit distinguishes this project's recorded service from unrelated processes on the same port and tells users when to use Stop Service, Repair State, or inspect an external listener manually.

See [docs/releases/v0.1.16.md](docs/releases/v0.1.16.md).

## v0.1.15

- Added a `/maintenance` Douyin login recovery card that detects likely expired login state from failed sync jobs and crawl runs.
- Added `Douyin Recall Account Recovery` for installed Windows builds so users can open `/auth` directly from the Start Menu.
- The recovery path reuses the existing QR rebind flow and does not delete or reset local browser profile data.

See [docs/releases/v0.1.15.md](docs/releases/v0.1.15.md).

## v0.1.14

- Added `recall verify-backup` for read-only SQLite backup recovery checks.
- Added `Douyin Recall Verify Backup` to installed Windows builds so users can validate the latest manual or pre-install backup from the Start Menu.
- Recovery backup scanning now includes both `recall-backup-*.db` and `pre-install-recall-*.db` files under `data\exports`.

See [docs/releases/v0.1.14.md](docs/releases/v0.1.14.md).

## v0.1.13

- Added `Douyin Recall Backup Now`, `Douyin Recall Backups`, and `Douyin Recall Restore Center` Start Menu entries for installed Windows builds.
- Added a best-effort installer pre-install safety copy from `data\recall.db` to `data\exports\pre-install-recall-*.db`.
- Restore remains in `/maintenance`, where SQLite validation and explicit confirmation are required before replacing the current database.

See [docs/releases/v0.1.13.md](docs/releases/v0.1.13.md).

## v0.1.12

- Added `Douyin Recall Health Check` for local install, log, runtime cache, uv, service record, and port checks.
- Added `Douyin Recall Repair State` to clear stale `server.json` / `server.pid` records when the recorded process is no longer running.
- The repair action deletes only explicit state files one at a time and does not touch the database, logs, browser profile, or login state.

See [docs/releases/v0.1.12.md](docs/releases/v0.1.12.md).

## v0.1.11

- Added a status summary to `Douyin Recall Control` before showing the menu or detailed status command.
- The summary shows installed version, local service state, maintenance URL, logs directory, runtime cache, and the right start/stop entry.
- The summary reads local state directly from `.env`, `pyproject.toml`, and `data\runtime\server.json`, so it can explain basic state even before uv is available.

See [docs/releases/v0.1.11.md](docs/releases/v0.1.11.md).

## v0.1.10

- Added a Windows Start Menu control entry for day-to-day local operations after installation.
- Added shortcuts for status, stop service, maintenance center, diagnostics, and logs.
- The maintenance shortcut opens `/maintenance` directly and starts the local service first when needed.

See [docs/releases/v0.1.10.md](docs/releases/v0.1.10.md).

## v0.1.9

- Installer launch script now runs startup preflight checks before downloading runtime dependencies.
- Preflight checks verify the install directory, log directory, and `D:\codexDownload` runtime cache are writable.
- If uv is not installed yet, preflight checks that the uv installer URL is reachable and reports a Chinese fix hint for network, proxy, or firewall issues.

See [docs/releases/v0.1.9.md](docs/releases/v0.1.9.md).

## v0.1.8

- Installer launch script now sets `UV_LINK_MODE=copy` when using the shared runtime cache on `D:\codexDownload`.
- This suppresses uv hardlink warnings caused by installing from a D: cache into the per-user C: install directory.

See [docs/releases/v0.1.8.md](docs/releases/v0.1.8.md).

## v0.1.7

- Added a read-only GitHub Release update checker with a one-hour cache.
- Added `uv run recall update` to show the local version, latest Release, and installer link without downloading or installing anything.
- Added a version/update card to `/maintenance` so local installs can see whether a newer Windows installer is available.

See [docs/releases/v0.1.7.md](docs/releases/v0.1.7.md).

## v0.1.6

- Installer launch failures now print recovery commands for `recall status`, `recall stop`, and `recall diagnose`.
- Installer startup now records a dedicated `data\logs\start-douyin-recall.log`.
- Added Windows installer troubleshooting documentation for SmartScreen, first-run downloads, logs, maintenance, and background service cleanup.

See [docs/releases/v0.1.6.md](docs/releases/v0.1.6.md).

## v0.1.5

- GitHub Release workflow now uses `docs/releases/<tag>.md` when release notes are present.
- Installer launch script now clearly warns that the installer is unsigned and Windows SmartScreen may show a warning.
- Installer launch script now clearly explains first-run downloads for Python dependencies and Playwright browser assets.
- Runtime download/cache location remains `D:\codexDownload\douyinclaude-runtime`.

See [docs/releases/v0.1.5.md](docs/releases/v0.1.5.md).

## v0.1.4

- Added `/maintenance` for service status, sync/index status, failed jobs, backups, restore, and diagnostics.
- Added `recall status`, `recall stop`, and hardened Windows server stop behavior.
- Added `recall diagnose` with redacted logs and sensitive path exclusions.
- Added SQLite backup validation and restore safety backup flow.
- Improved installer privacy exclusions and first-run local install smoke path.

See [docs/releases/v0.1.4.md](docs/releases/v0.1.4.md).
