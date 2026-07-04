# Windows Backup Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Windows installs easier to back up and safer to upgrade by adding backup-oriented Start Menu controls and installer pre-install safety backup.

**Architecture:** Reuse the existing SQLite export and maintenance restore flow. Add only thin Windows control-script wrappers and an Inno Setup pre-install copy step, keeping restore confirmation in the web maintenance center.

**Tech Stack:** PowerShell 5.1-compatible scripts, Inno Setup 6, Python `unittest`/`pytest`, existing `recall export --format sqlite` CLI.

---

### Task 1: Packaging Tests

**Files:**
- Modify: `tests/test_windows_packaging.py`

- [ ] **Step 1: Write failing tests**

Add assertions that `control-douyin-recall.ps1` exposes `backup`, `backups`, and `restore`, includes functions named `Create-SqliteBackup`, `Open-BackupsDirectory`, and `Open-RestoreCenter`, uses `Invoke-RecallCommand @('export', '--format', 'sqlite', '--output', $ExportsDir)`, and avoids recursive delete.

Add assertions that `DouyinRecall.iss` installs shortcuts named `Douyin Recall Backup Now`, `Douyin Recall Backups`, and `Douyin Recall Restore Center`, and contains a pre-install safety backup procedure with `pre-install-recall-`.

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: tests fail because actions, shortcuts, and pre-install backup code do not exist yet.

### Task 2: Windows Control Script

**Files:**
- Modify: `packaging/windows/control-douyin-recall.ps1`

- [ ] **Step 1: Implement actions**

Add `$ExportsDir = Join-Path $AppRoot "data\exports"`.

Add functions:

```powershell
function Create-SqliteBackup {
    Initialize-RuntimeEnvironment
    Write-Header "Create SQLite backup"
    New-Item -ItemType Directory -Path $ExportsDir -Force | Out-Null
    Invoke-RecallCommand @('export', '--format', 'sqlite', '--output', $ExportsDir)
    Write-Host "Backups directory: $ExportsDir"
}

function Open-BackupsDirectory {
    New-Item -ItemType Directory -Path $ExportsDir -Force | Out-Null
    Write-Header "Open backups directory"
    Write-Host $ExportsDir
    Start-Process $ExportsDir
}

function Open-RestoreCenter {
    $port = Get-WebPort
    $url = "http://127.0.0.1:$port/maintenance"
    Write-Header "Open restore center"
    if (Test-WebAvailable -Url $url) {
        Start-Process $url
        return
    }
    & $StartScript -OpenPath "/maintenance"
}
```

Extend `ValidateSet`, the menu, and the switch with `backup`, `backups`, and `restore`.

- [ ] **Step 2: Run focused tests**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: control-script assertions pass; Inno assertions still fail until Task 3.

### Task 3: Inno Installer

**Files:**
- Modify: `packaging/windows/DouyinRecall.iss`

- [ ] **Step 1: Add shortcuts**

Add Start Menu shortcuts for:

- `Douyin Recall Backup Now` with `-Action ""backup""`
- `Douyin Recall Backups` with `-Action ""backups""`
- `Douyin Recall Restore Center` with `-Action ""restore""`

- [ ] **Step 2: Add pre-install backup code**

Add a `[Code]` section that:

- checks `{app}\data\recall.db`
- creates `{app}\data\exports` when needed
- copies the database to `{app}\data\exports\pre-install-recall-<timestamp>.db`
- skips safely when the database does not exist
- does not delete anything

- [ ] **Step 3: Run focused tests**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass.

### Task 4: Version And Docs

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/windows-troubleshooting.md`
- Modify: `docs/roadmap.md`
- Create: `docs/releases/v0.1.13.md`

- [ ] **Step 1: Bump version**

Set project and installer version to `0.1.13`, then run:

```powershell
uv lock
```

- [ ] **Step 2: Document backup/recovery controls**

Document the new Start Menu entries, pre-install safety backup path, and restore-center confirmation flow.

- [ ] **Step 3: Run tests**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: release-note and troubleshooting assertions pass.

### Task 5: Verification And Release

**Files:**
- Build artifact: `packaging/windows/out/DouyinRecallSetup.exe`
- Release artifact download target: `D:\codexDownload\douyin-release-v0.1.13`

- [ ] **Step 1: Run full verification**

Run:

```powershell
uv run --with pytest python -m pytest -q
uv run python -m compileall src tests
git diff --check
```

Expected: tests pass, compile succeeds, diff check is clean.

- [ ] **Step 2: Build and install locally**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build-installer.ps1
```

Install silently and verify installed shortcuts and health/backup commands.

- [ ] **Step 3: Commit, tag, push, and verify GitHub Release**

Commit, tag `v0.1.13`, push `main` and tag, wait for Actions, download the release installer to `D:\codexDownload\douyin-release-v0.1.13`, install it silently, and verify the installed controls.
