# Changelog

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
