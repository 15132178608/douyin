# Backup Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only latest-backup verification path for CLI and Windows installs.

**Architecture:** Reuse `maintenance.validate_sqlite_backup()` and add a small recovery-backup listing helper. Expose it through a Click command and a thin Windows control-script wrapper.

**Tech Stack:** Python maintenance helpers, Click CLI, PowerShell 5.1-compatible control script, Inno Setup 6, unittest/pytest.

---

### Task 1: Maintenance Tests

**Files:**
- Modify: `tests/test_maintenance.py`

- [ ] **Step 1: Write failing tests**

Add tests that create manual and pre-install backup files in a temporary directory and assert:

```python
items = maintenance.list_recovery_backups(Path(tmp), limit=4)
assert [item.name for item in items] == [
    "pre-install-recall-20260705-100000.db",
    "recall-backup-20260704-100000.db",
]
```

Add a valid backup test:

```python
report = maintenance.verify_latest_backup(backup_dir)
assert report["ok"] is True
assert report["backup"]["name"] == "recall-backup-20260705-100000.db"
assert report["validation"]["counts"]["favorites"] == 1
```

Add a no-backup test:

```python
report = maintenance.verify_latest_backup(Path(tmp))
assert report["ok"] is False
assert "没有找到可校验的备份文件。" in report["errors"]
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_maintenance.py
```

Expected: fails because `list_recovery_backups` and `verify_latest_backup` do not exist.

### Task 2: Maintenance Implementation

**Files:**
- Modify: `src/maintenance.py`

- [ ] **Step 1: Implement helpers**

Add:

```python
RECOVERY_BACKUP_PATTERNS = ("recall-backup-*.db", "pre-install-recall-*.db")
```

Implement `list_recovery_backups()` by collecting matching files, de-duplicating paths, sorting by filename then modified time descending, and returning `BackupInfo`.

Implement `verify_latest_backup()` by picking the newest recovery backup and calling `validate_sqlite_backup()`.

- [ ] **Step 2: Run focused tests**

Run:

```powershell
uv run python tests\test_maintenance.py
```

Expected: maintenance tests pass.

### Task 3: CLI Tests And Command

**Files:**
- Modify: `tests/test_crawler_api.py`
- Modify: `src/cli.py`

- [ ] **Step 1: Write failing CLI tests**

Use `CliRunner` and `TemporaryDirectory` to assert:

```python
result = runner.invoke(cli, ["verify-backup", "--output", str(tmp)])
assert result.exit_code == 1
assert "没有找到可校验的备份文件。" in result.output
```

For a valid backup, create it with `create_test_db()` or an equivalent local helper and assert exit code `0`, `SQLite backup OK`, and `favorites: 1`.

- [ ] **Step 2: Run red CLI tests**

Run:

```powershell
uv run pytest tests/test_crawler_api.py -q
```

Expected: fails because the command does not exist.

- [ ] **Step 3: Implement command**

Add `@cli.command("verify-backup")` with `--output` and `--path`.

If `--path` is passed, call `maintenance.validate_sqlite_backup(path)` directly. Otherwise call `maintenance.verify_latest_backup(output_dir)`.

Print:

```text
SQLite backup OK: <path>
integrity: ok
required tables: ok
users: <n>
favorites: <n>
likes: <n>
```

On failure, print errors to stderr and exit `1`.

- [ ] **Step 4: Run CLI tests**

Run:

```powershell
uv run pytest tests/test_crawler_api.py -q
```

Expected: CLI tests pass.

### Task 4: Windows Packaging

**Files:**
- Modify: `packaging/windows/control-douyin-recall.ps1`
- Modify: `packaging/windows/DouyinRecall.iss`
- Modify: `tests/test_windows_packaging.py`

- [ ] **Step 1: Write failing packaging tests**

Assert `verify-backup` is in the ValidateSet, `Verify-LatestBackup` exists, the control script calls `Invoke-RecallCommand @('verify-backup', '--output', $ExportsDir)`, and Inno creates `Douyin Recall Verify Backup`.

- [ ] **Step 2: Run red packaging tests**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: fails because the control action and shortcut do not exist.

- [ ] **Step 3: Implement Windows wrappers**

Add `Verify-LatestBackup` to the control script and the menu/switch. Add the Start Menu shortcut in Inno.

- [ ] **Step 4: Run packaging tests**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass.

### Task 5: Version, Docs, Release

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/windows-troubleshooting.md`
- Modify: `docs/roadmap.md`
- Create: `docs/releases/v0.1.14.md`

- [ ] **Step 1: Bump version**

Set version to `0.1.14`, update Inno version, and run:

```powershell
uv lock
```

- [ ] **Step 2: Document verification**

Document `recall verify-backup`, `Douyin Recall Verify Backup`, and that the action is read-only.

- [ ] **Step 3: Full verification and release**

Run pytest, compileall, diff check, PowerShell parsing, build installer, local install, release tag, GitHub Actions, release download, installed verification command, and final port checks.
