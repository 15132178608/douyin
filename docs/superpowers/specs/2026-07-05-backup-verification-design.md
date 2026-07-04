# Backup Verification Design

## Context

Douyin Recall can already create SQLite backups, copy the database before installer upgrades, and restore through `/maintenance` after validation and explicit confirmation. The remaining recovery gap is confidence: a user can have backup files but still not know whether the latest one is readable and restore-eligible.

## Goals

- Add a read-only backup verification command for local CLI use.
- Add a Windows Start Menu and control-menu entry for verifying the latest backup.
- Include both manual backups (`recall-backup-*.db`) and installer safety backups (`pre-install-recall-*.db`) in the latest-backup scan.
- Print clear counts for users, favorites, and likes from the validated backup.
- Keep restore behind `/maintenance`; this feature must not replace the current database.
- Do not delete, prune, rotate, or move any backup files.

## Non-Goals

- No backup deletion or retention enforcement.
- No scheduled automatic restore drill.
- No automatic restore from Start Menu.
- No cloud or external-drive sync.

## Design

Add `maintenance.list_recovery_backups(output_dir=None, limit=8)`, which scans `data\exports` for two safe backup filename families:

- `recall-backup-*.db`
- `pre-install-recall-*.db`

It returns the same `BackupInfo` shape currently used by maintenance status and sorts newest first. Existing `list_sqlite_backups()` remains focused on manual `recall-backup-*.db` so the web restore list does not unexpectedly expand.

Add `maintenance.verify_latest_backup(output_dir=None)`. It picks the first item from `list_recovery_backups(limit=1)`, calls `validate_sqlite_backup()`, and returns a report with:

- `ok`
- `backup`
- `validation`
- `errors`

If no backups exist, it returns `ok=False` with a clear error.

Add `recall verify-backup`:

- `--output`: backup directory, default `data\exports`
- `--path`: verify a specific backup file instead of latest

The command exits `0` when validation succeeds and `1` when validation fails. It prints the backup path, integrity result, required table result, and row counts.

Add Windows control action `verify-backup` and Start Menu shortcut `Douyin Recall Verify Backup`. The action calls `uv run recall verify-backup --output data\exports`, waits before exit, and does not start the web server.

## Testing

- Maintenance tests cover listing both manual and pre-install backups, latest selection, valid latest verification, invalid latest verification, and no-backup reporting.
- CLI tests cover success and failure exit codes.
- Windows packaging tests cover the new action, shortcut, release notes, troubleshooting docs, ASCII script safety, and no recursive deletion.
- Installer and release verification runs the installed `verify-backup` action after creating a backup.
