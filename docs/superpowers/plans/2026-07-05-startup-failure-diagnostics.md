# Startup Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Windows first-launch and runtime-preparation failures explain the failed stage and the safest next step.

**Architecture:** Add step tracking and failure-hint functions to the Windows launcher and control script. Keep launcher hints in Chinese and keep control-script hints ASCII to preserve PowerShell 5.1 packaging tests.

**Tech Stack:** PowerShell 5.1-compatible scripts, Inno Setup 6, unittest packaging tests.

---

### Task 1: Launcher Failure Hints

**Files:**
- Modify: `tests/test_windows_packaging.py`
- Modify: `packaging/windows/start-douyin-recall.ps1`

- [ ] **Step 1: Write failing launcher test**

Add assertions to `test_launcher_prints_recovery_steps_when_startup_fails`:

```python
self.assertIn("$script:CurrentStartupStep", launcher)
self.assertIn("function Write-StartupFailureHint", launcher)
self.assertIn("失败阶段", launcher)
self.assertIn("可能原因", launcher)
self.assertIn("建议下一步", launcher)
self.assertIn("Douyin Recall Prepare Runtime", launcher)
self.assertIn("uv sync", launcher)
self.assertIn("playwright install chromium", launcher)
self.assertIn("recall init-db", launcher)
self.assertIn("recall serve", launcher)
self.assertIn("uv run recall diagnose", launcher)
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: fails because launcher failure hints do not exist.

- [ ] **Step 3: Implement launcher hints**

Add `$script:CurrentStartupStep`, update `Write-Step`, add `Write-StartupFailureHint`, and call it from the top-level catch before `Write-Troubleshooting`.

- [ ] **Step 4: Run focused test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass or fail only on control-script hint assertions from Task 2.

### Task 2: Prepare Runtime Failure Hints

**Files:**
- Modify: `tests/test_windows_packaging.py`
- Modify: `packaging/windows/control-douyin-recall.ps1`

- [ ] **Step 1: Write failing control test**

Add assertions to `test_control_script_exposes_retryable_runtime_preparation`:

```python
self.assertIn("$script:CurrentPrepareStep", control)
self.assertIn("function Write-PrepareFailureHint", control)
self.assertIn("Prepare failed at step:", control)
self.assertIn("Likely cause:", control)
self.assertIn("Recommended next step:", control)
self.assertIn("Retry entry: Douyin Recall Prepare Runtime", control)
self.assertIn("uv run recall diagnose", control)
self.assertIn("Runtime cache:", control)
self.assertIn("Logs:", control)
self.assertNotIn("Remove-Item -Recurse", control)
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: fails because control-script failure hints do not exist.

- [ ] **Step 3: Implement prepare hints**

Add `$script:CurrentPrepareStep`, set it in `Invoke-PrepareStep`, add `Write-PrepareFailureHint`, and wrap `Prepare-Runtime` with a local try/catch that prints the hint and rethrows.

- [ ] **Step 4: Run focused test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass.

### Task 3: Version and Docs

**Files:**
- Modify: `pyproject.toml`
- Modify: `packaging/windows/DouyinRecall.iss`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/windows-troubleshooting.md`
- Modify: `docs/roadmap.md`
- Create: `docs/releases/v0.1.18.md`

- [ ] **Step 1: Bump version**

Set version to `0.1.18` in `pyproject.toml` and `DouyinRecall.iss`, then run:

```powershell
$env:UV_CACHE_DIR = 'D:\codexDownload\douyinclaude-runtime\uv-cache'
$env:UV_LINK_MODE = 'copy'
uv lock
```

- [ ] **Step 2: Document failure diagnostics**

Document that startup and prepare failures now print failed stage, likely cause, next step, runtime cache, logs, and diagnostics command.

- [ ] **Step 3: Verify and release**

Run focused tests, full pytest, compileall, diff check, PowerShell parsing, ASCII check, installer build, local install smoke, tag push, GitHub Actions release verification, downloaded installer smoke, SHA256 check, and final port checks.
