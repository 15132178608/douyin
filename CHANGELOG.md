# Changelog

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
