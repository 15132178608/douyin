# Startup Failure Diagnostics Design

## Context

Douyin Recall's Windows install flow now has two startup-related paths:

- `Douyin Recall`, which runs `start-douyin-recall.ps1`, prepares dependencies, starts the local Web service, and opens the browser.
- `Douyin Recall Prepare Runtime`, which runs the control script's `prepare` action and retries runtime preparation without starting the Web service.

The remaining usability gap is the failure message. When `uv sync`, Playwright Chromium install, uv installation, database initialization, or service startup fails, the window currently prints the raw exception and generic recovery commands. The next improvement is to say which stage failed and what the user can safely do next.

## Goals

- Track the current launcher step and print it when startup fails.
- Add Chinese recovery hints to `start-douyin-recall.ps1` for common first-launch failure stages.
- Add ASCII recovery hints to the control script's `prepare` action while preserving the existing ASCII requirement.
- Point users to `Douyin Recall Prepare Runtime` when dependency setup fails.
- Point users to `D:\codexDownload\douyinclaude-runtime`, `data\logs\start-douyin-recall.log`, and `uv run recall diagnose`.
- Keep all behavior conservative: no automatic cleanup, no cache deletion, no process killing, no browser opening from the prepare retry path.

## Non-Goals

- Do not add an installer wizard progress page.
- Do not add a graphical progress UI.
- Do not execute `prepare` automatically after a failed normal launch.
- Do not delete caches, database files, logs, browser profiles, login state, or backups.
- Do not start or stop the Web service in `Prepare-Runtime` failure handling.

## Design

In `start-douyin-recall.ps1`:

- Add `$script:CurrentStartupStep`.
- Update `Write-Step` so every startup phase records the current step.
- Add `Write-StartupFailureHint`.
- In the top-level `catch`, after the raw exception, print:
  - failed stage
  - likely cause
  - recommended next step
  - runtime cache, startup log, service logs, and diagnostics command

The hint function is intentionally simple string matching:

- `uv sync`: dependency download or Python environment setup failed; retry with `Douyin Recall Prepare Runtime`.
- `playwright install chromium`: Chromium download/setup failed; retry with `Douyin Recall Prepare Runtime`.
- `uv not found` / uv installer: network/proxy or uv install failed; check network/proxy and retry prepare.
- `init-db`: database initialization failed; check install/data directory write access and retry prepare.
- `serve`: Web service startup failed; use `recall status`, `recall stop`, Health Check, or Repair State.
- unknown: run Prepare Runtime and diagnostics.

In `control-douyin-recall.ps1`:

- Add `$script:CurrentPrepareStep`.
- Update `Invoke-PrepareStep` to record the current step.
- Add `Write-PrepareFailureHint`, using ASCII text only.
- Wrap the body of `Prepare-Runtime` so it prints:
  - failed prepare step
  - recommended retry path
  - runtime cache and logs directory
  - `uv run recall diagnose`

The control script top-level catch remains in place for non-prepare actions.

## Testing

- Windows packaging tests assert the launcher contains the current-step tracker, failure hint function, stage-specific hints, `Douyin Recall Prepare Runtime`, runtime cache, logs, and diagnostics command.
- Windows packaging tests assert the control script contains prepare-step tracking, failure hint output, runtime cache/log references, diagnostics command, and no recursive delete.
- Existing PowerShell parse and ASCII checks continue to cover the control script.
- Release smoke tests verify installed scripts and shortcuts but do not intentionally run failing network/download flows.
