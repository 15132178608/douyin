# Windows Backup Recovery Design

## Context

Douyin Recall already has SQLite backup, validation, restore, and diagnostics in the Python maintenance layer and `/maintenance`. Windows installs now also have Start Menu controls for status, stop, diagnostics, logs, health checks, and stale-state repair. The next recovery gap is discoverability and upgrade protection: a personal install should make a current database backup easy to create and should create a safety backup before an installer overwrites application files.

## Goals

- Add a Start Menu entry and control-menu action for creating a SQLite backup without opening the web UI.
- Add a Start Menu entry and control-menu action for opening the backup directory.
- Add a Start Menu entry that opens the maintenance restore workflow rather than performing a blind restore.
- Run a best-effort pre-install backup from the Inno Setup installer before files are upgraded.
- Keep all runtime/cache behavior under `D:\codexDownload\douyinclaude-runtime`.
- Keep repair and cleanup operations explicit; do not add bulk delete behavior.

## Non-Goals

- No one-click restore from Start Menu. Restore stays behind `/maintenance` validation and confirmation.
- No backup pruning or retention cleanup in this release.
- No migration of browser profile, `.env`, or login-state storage.

## Design

The Windows control script gains three low-risk actions:

- `backup`: initializes the runtime environment and runs `uv run recall export --format sqlite --output data\exports`.
- `backups`: creates and opens `data\exports`.
- `restore`: opens `/maintenance`, where the existing restore validation and confirmation UI already lives.

The Inno installer gains a `[Code]` pre-install step. If an existing install contains `data\recall.db`, the step copies it to `data\exports\pre-install-recall-<timestamp>.db` before files are installed. This backup is best-effort: failure is surfaced with a message, but the user can choose whether to continue. Silent installs should not block on prompts.

The release documentation and troubleshooting docs explain where backups live, which entries to use, and that restore still requires maintenance-center confirmation.

## Testing

- Packaging tests assert the new actions, functions, Start Menu shortcuts, and Inno pre-install backup code.
- Packaging tests assert the control script stays ASCII and does not use recursive delete.
- Release notes and troubleshooting tests assert backup and restore entries are documented.
- Existing maintenance tests continue covering SQLite validation and restore safety backup behavior.
- Installer verification must include local silent install and running the installed backup action against a temporary or empty install database state.
