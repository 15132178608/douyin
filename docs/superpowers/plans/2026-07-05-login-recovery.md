# Login Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Douyin login expiration visible and recoverable from maintenance and installed Windows builds.

**Architecture:** Add a read-only auth recovery summary to maintenance status by inspecting recent crawl/job errors and local profile evidence. Render that summary in the maintenance template and add a Windows shortcut that opens the existing `/auth` QR rebind flow.

**Tech Stack:** Python maintenance helpers, FastAPI/Jinja templates, PowerShell 5.1-compatible Windows control script, Inno Setup 6, unittest/pytest.

---

### Task 1: Maintenance Auth Status

**Files:**
- Modify: `tests/test_maintenance.py`
- Modify: `src/maintenance.py`

- [ ] **Step 1: Write failing tests**

Add a test that inserts a failed likes crawl run and failed sync job with `用户未登录`, then asserts:

```python
status = maintenance.get_maintenance_status("default")
assert status["auth"]["needs_rebind"] is True
assert status["auth"]["status"] == "expired"
assert status["auth"]["recovery_url"] == "/auth"
assert "douyin_login_expired" in status["attention_codes"]
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_maintenance.py
```

Expected: fails because `auth` is not in the maintenance status.

- [ ] **Step 3: Implement status summary**

Add auth markers and a helper that returns `status`, `needs_rebind`, `recovery_url`, profile evidence, and recent auth-like errors.

- [ ] **Step 4: Run focused test**

Run:

```powershell
uv run python tests\test_maintenance.py
```

Expected: maintenance tests pass.

### Task 2: Maintenance UI

**Files:**
- Modify: `tests/test_web_templates.py`
- Modify: `src/web/templates/_maintenance_status.html`
- Modify: `src/web/templates/jobs.html`

- [ ] **Step 1: Write failing template tests**

Assert the maintenance template contains:

```python
assert "抖音登录" in maintenance_status
assert "登录态可能过期" in maintenance_status
assert 'href="/auth"' in maintenance_status
assert "douyin_login_expired" in maintenance_status
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_web_templates.py
```

Expected: fails because the card does not exist.

- [ ] **Step 3: Implement template card**

Add a maintenance card that uses `maintenance_status.auth.needs_rebind`, links to `/auth`, and keeps normal states compact.

- [ ] **Step 4: Run template tests**

Run:

```powershell
uv run python tests\test_web_templates.py
```

Expected: template tests pass.

### Task 3: Windows Account Recovery Entry

**Files:**
- Modify: `tests/test_windows_packaging.py`
- Modify: `packaging/windows/control-douyin-recall.ps1`
- Modify: `packaging/windows/DouyinRecall.iss`

- [ ] **Step 1: Write failing packaging tests**

Assert the control script includes `auth` in `ValidateSet`, defines `Open-AccountRecovery`, starts `-OpenPath "/auth"`, and Inno creates `Douyin Recall Account Recovery`.

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: fails because the Windows shortcut/action does not exist.

- [ ] **Step 3: Implement Windows action**

Add `auth` action, menu item, and Start Menu shortcut.

- [ ] **Step 4: Run packaging tests**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass.

### Task 4: Version, Docs, Release

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/windows-troubleshooting.md`
- Modify: `docs/roadmap.md`
- Create: `docs/releases/v0.1.15.md`

- [ ] **Step 1: Bump version**

Set version to `0.1.15`, update Inno version, and run:

```powershell
uv lock
```

- [ ] **Step 2: Document account recovery**

Document `Douyin Recall Account Recovery`, `/auth`, and the maintenance login-expired card.

- [ ] **Step 3: Verify and release**

Run full pytest, compileall, diff check, PowerShell parsing, installer build, local install smoke, tag push, GitHub Actions, release download, installed shortcut verification, and port checks.
