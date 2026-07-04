# Runtime Preparation Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a retryable Windows "prepare runtime only" path for first-launch dependency setup without starting the Web service.

**Architecture:** Extend the existing Windows control script with a `prepare` action that reuses the same runtime cache conventions as the launcher, installs/locates uv, runs dependency setup commands, and exits before any Web server startup.

**Tech Stack:** PowerShell 5.1-compatible control script, Inno Setup 6, unittest packaging tests, release docs.

---

### Task 1: Windows Prepare Runtime Action

**Files:**
- Modify: `tests/test_windows_packaging.py`
- Modify: `packaging/windows/control-douyin-recall.ps1`
- Modify: `packaging/windows/DouyinRecall.iss`

- [ ] **Step 1: Write failing packaging test**

Add assertions that:

```python
self.assertIn('"prepare"', control)
self.assertIn("function Find-OrInstall-Uv", control)
self.assertIn("function Invoke-PrepareStep", control)
self.assertIn("function Prepare-Runtime", control)
self.assertIn("$UvDownloadDir", control)
self.assertIn("https://astral.sh/uv/install.ps1", control)
self.assertIn("Invoke-WebRequest -Uri $UvInstallScriptUrl -OutFile $installer", control)
self.assertIn("uv sync", control)
self.assertIn("playwright install chromium", control)
self.assertIn("recall init-db", control)
self.assertIn("recall status", control)
self.assertIn("does not start the local web service", control)
self.assertIn('"prepare" { Prepare-Runtime; Wait-BeforeExit }', control)
self.assertNotIn('"prepare" { Start-DouyinRecall', control)
self.assertIn("Douyin Recall Prepare Runtime", script)
self.assertIn('-Action ""prepare""', script)
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: fails because `prepare` does not exist.

- [ ] **Step 3: Implement control action**

Add `prepare` to the control script `ValidateSet`, helpers, menu item, and switch. Add the Inno Start Menu shortcut. Do not call `Start-Process`, `recall serve`, `/maintenance`, or browser open paths from `Prepare-Runtime`.

- [ ] **Step 4: Run focused test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass.

### Task 2: Version and Documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `packaging/windows/DouyinRecall.iss`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/windows-troubleshooting.md`
- Modify: `docs/roadmap.md`
- Create: `docs/releases/v0.1.17.md`

- [ ] **Step 1: Bump version**

Set version to `0.1.17` in `pyproject.toml` and `DouyinRecall.iss`, then run:

```powershell
$env:UV_CACHE_DIR = 'D:\codexDownload\douyinclaude-runtime\uv-cache'
$env:UV_LINK_MODE = 'copy'
uv lock
```

- [ ] **Step 2: Document retry path**

Document `Douyin Recall Prepare Runtime` as the safe retry entry for first-launch downloads and dependency setup. State that it does not start the local Web service or open a browser.

- [ ] **Step 3: Verify and release**

Run focused tests, full pytest, compileall, diff check, PowerShell parsing, ASCII check, installer build, local install smoke, tag push, GitHub Actions release verification, downloaded installer smoke, SHA256 check, and final port checks.
