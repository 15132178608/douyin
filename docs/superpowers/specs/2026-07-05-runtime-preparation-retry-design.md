# Runtime Preparation Retry Design

## Context

Douyin Recall's Windows launcher already performs startup preflight checks, prepares the runtime cache under `D:\codexDownload\douyinclaude-runtime`, installs or locates `uv`, runs `uv sync`, installs Playwright Chromium, initializes SQLite, then starts the local Web service.

This is functional, but first launch still has a usability gap: if dependency download or Playwright setup fails, the retry path is to launch the app again. That can feel risky because the same entry eventually starts a background Web process and opens a browser. The user needs a direct, repeatable "prepare runtime only" path that makes downloads retryable without starting the app.

## Goals

- Add an installed Windows action that prepares runtime dependencies without starting the Web service.
- Make the action safe to rerun after partial first-launch failures.
- Keep downloads and caches under `D:\codexDownload\douyinclaude-runtime`.
- Install `uv` into the current user flow when it is missing, using the existing `D:\codexDownload\douyinclaude-runtime\uv` download directory.
- Run these steps in order: locate/install uv, `uv sync`, `uv run playwright install chromium`, `uv run recall init-db`, `uv run recall status`.
- Print clear progress and retry guidance, including the logs directory and runtime cache.

## Non-Goals

- Do not start `recall serve`.
- Do not open a browser or `/maintenance`.
- Do not add an installer wizard progress page.
- Do not download files outside `D:\codexDownload\douyinclaude-runtime`.
- Do not delete runtime caches, logs, database files, browser profiles, login state, or backups.

## Design

Add a new action to `packaging/windows/control-douyin-recall.ps1`:

```powershell
-Action prepare
```

The action is exposed in:

- `ValidateSet`
- `Douyin Recall Control` menu
- Inno Setup Start Menu shortcut: `Douyin Recall Prepare Runtime`

The PowerShell implementation adds:

- `$UvDownloadDir = Join-Path $DownloadRoot "uv"`
- `$UvInstallScriptUrl = "https://astral.sh/uv/install.ps1"`
- `Find-OrInstall-Uv`, which mirrors the launcher behavior:
  - respect `$env:UV_EXE`
  - use `uv.exe` on PATH
  - use `$env:USERPROFILE\.local\bin\uv.exe`
  - otherwise download `install-uv.ps1` into `$UvDownloadDir` and run it
- `Invoke-PrepareStep`, a small wrapper that prints step names and fails with the command exit code.
- `Prepare-Runtime`, which runs the retryable dependency preparation sequence and explicitly states that it does not start the local Web service.

`Start-DouyinRecall` and the existing launcher remain unchanged. The normal app shortcut still launches the Web UI. The new action is for preparation/retry only.

## Testing

- Windows packaging tests cover the new `prepare` action, Start Menu shortcut, uv install download path, retry messaging, and the fact that the action does not call `recall serve`.
- Existing launcher tests continue covering the normal startup path.
- Release smoke tests install the generated setup, verify the new shortcut and installed control script, run a parse/ASCII check, and avoid executing `prepare` because it may download large dependencies.
- Final verification confirms ports `8000` and `8017` are not listening after smoke tests.
