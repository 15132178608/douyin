# Changelog

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
