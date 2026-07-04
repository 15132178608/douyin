# Service Lifecycle Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make forgotten Douyin Recall background services easy to identify and safely stop or repair.

**Architecture:** Add a tested Python service-audit helper, surface it through `recall status`, mirror the same decision language in the Windows control script, and document the installed workflow.

**Tech Stack:** Python lifecycle helpers, Click CLI, PowerShell 5.1-compatible Windows control script, Inno Setup 6, pytest/unittest.

---

### Task 1: Python Service Audit

**Files:**
- Modify: `tests/test_server_runtime.py`
- Modify: `src/server_runtime.py`

- [ ] **Step 1: Write failing service-audit tests**

Add tests that create temporary runtime state and assert:

```python
audit = server_runtime.get_service_audit(
    runtime_dir=runtime_dir,
    configured_port=8000,
    process_checker=lambda pid: pid == 1111,
    port_owner_checker=lambda port: 1111,
)
assert audit["relation"] == "own_service_running"
assert audit["action"] == "stop"
assert audit["recorded_pid"] == 1111
assert audit["port_owner_pid"] == 1111
assert "uv run recall stop" in audit["next_step"]
```

Also cover no state plus external listener, stale state, recorded process without listener, and recorded PID/port owner mismatch.

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_server_runtime.py
```

Expected: fails because `get_service_audit` does not exist.

- [ ] **Step 3: Implement minimal audit helper**

Add `get_service_audit()` to `src/server_runtime.py`. Reuse `get_server_status()` and existing injected checkers. Do not delete files, stop processes, or mutate state in this helper.

- [ ] **Step 4: Run focused test**

Run:

```powershell
uv run python tests\test_server_runtime.py
```

Expected: server runtime tests pass.

### Task 2: CLI Status Output

**Files:**
- Modify: `tests/test_server_runtime.py`
- Modify: `src/cli.py`

- [ ] **Step 1: Write failing CLI status test**

Use `CliRunner` and monkeypatch `server_runtime.get_service_audit` so `recall status` prints:

```text
Service audit: own_service_running
Port owner PID: 1111
Next step: uv run recall stop
```

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run --with pytest python -m pytest tests\test_server_runtime.py -q
```

Expected: fails because the CLI does not print audit details yet.

- [ ] **Step 3: Implement CLI output**

Update `status_cmd()` to call `get_service_audit(configured_port=settings.web_port)` after the existing status message. Print relation, port, recorded PID when present, port owner PID when present, and next step.

- [ ] **Step 4: Run focused test**

Run:

```powershell
uv run --with pytest python -m pytest tests\test_server_runtime.py -q
```

Expected: tests pass.

### Task 3: Windows Service-Audit Guidance

**Files:**
- Modify: `tests/test_windows_packaging.py`
- Modify: `packaging/windows/control-douyin-recall.ps1`

- [ ] **Step 1: Write failing packaging tests**

Assert the control script contains `Get-ServiceAudit`, `Service audit:`, `Port owner PID:`, `Next step:`, `external listener`, `Douyin Recall Stop Service`, and `Douyin Recall Repair State`.

- [ ] **Step 2: Run red test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: fails because the PowerShell audit helper does not exist.

- [ ] **Step 3: Implement PowerShell audit helper**

Add `Get-ServiceAudit` and print its output from `Write-ControlSummary` and `Invoke-HealthCheck`. Do not add any new process-kill code; keep `Stop-DouyinRecall` delegated to `recall stop`.

- [ ] **Step 4: Run packaging test**

Run:

```powershell
uv run python tests\test_windows_packaging.py
```

Expected: packaging tests pass.

### Task 4: Version, Docs, Release

**Files:**
- Modify: `pyproject.toml`
- Modify: `packaging/windows/DouyinRecall.iss`
- Modify: `uv.lock`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/windows-troubleshooting.md`
- Modify: `docs/roadmap.md`
- Create: `docs/releases/v0.1.16.md`

- [ ] **Step 1: Bump version**

Set version to `0.1.16` in `pyproject.toml` and `DouyinRecall.iss`, then run:

```powershell
$env:UV_CACHE_DIR = 'D:\codexDownload\douyinclaude-runtime\uv-cache'
$env:UV_LINK_MODE = 'copy'
uv lock
```

- [ ] **Step 2: Document service lifecycle audit**

Document `recall status`, `Douyin Recall Stop Service`, `Douyin Recall Health Check`, and `Douyin Recall Repair State` as the recommended cleanup path for forgotten background services.

- [ ] **Step 3: Verify and release**

Run focused tests, full pytest, compileall, diff check, PowerShell parsing, ASCII check, installer build, local install smoke, tag push, GitHub Actions release verification, downloaded installer smoke, SHA256 check, and final port checks.
