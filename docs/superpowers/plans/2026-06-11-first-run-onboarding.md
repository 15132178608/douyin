# First-Run Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `/setup` first-run Web flow so non-technical Windows users can bind Douyin, start sync jobs, start index jobs, and reach the home page without reading CLI instructions.

**Architecture:** Add a small read-only `src/onboarding.py` status module, then wire a FastAPI `/setup` page and HTMX status fragment into the existing Web app. Reuse the current `/auth/*` and `/jobs/*` routes instead of creating new crawler or job execution paths.

**Tech Stack:** Python, FastAPI, Jinja2, HTMX, SQLite, existing `jobs` queue, existing Playwright-based Douyin auth flow.

---

## File Map

- Create `src/onboarding.py`: read-only onboarding status aggregation for the current local user.
- Create `tests/test_onboarding.py`: isolated SQLite tests for status aggregation.
- Modify `src/web/app.py`: import onboarding, add `/setup` and `/setup/status`, pass setup flags into home pages.
- Create `src/web/templates/setup.html`: single-page first-run flow.
- Create `src/web/templates/_setup_status.html`: polling status summary used by `/setup`.
- Modify `src/web/templates/index.html`: add setup entry when the current module has no items.
- Modify `src/web/templates/base.html`: add minimal shared styles for setup/empty-state controls if needed.
- Modify `tests/test_web_templates.py`: assert setup page sections and home setup entry.
- Modify `README.md`: document the first-run `/setup` flow for installer users.

## Task 1: Onboarding Status Module

**Files:**
- Create: `D:\douyinclaude\tests\test_onboarding.py`
- Create: `D:\douyinclaude\src\onboarding.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_onboarding.py` with tests that use an in-memory copy of `db.SCHEMA_SQL`, patch `src.onboarding.get_connection`, and assert:

```python
def test_empty_database_needs_setup() -> None:
    status = onboarding.get_onboarding_status("default")
    assert status["needs_setup"] is True
    assert status["has_any_items"] is False
    assert status["favorites"]["total"] == 0
    assert status["likes"]["total"] == 0
```

```python
def test_items_and_index_counts_are_content_kind_scoped() -> None:
    # Insert one favorite, one like, one favorite vec row, one like vec row.
    status = onboarding.get_onboarding_status("default")
    assert status["has_any_items"] is True
    assert status["needs_setup"] is False
    assert status["favorites"]["total"] == 1
    assert status["favorites"]["indexed"] == 1
    assert status["likes"]["total"] == 1
    assert status["likes"]["indexed"] == 1
```

```python
def test_profile_and_job_summary_are_reported() -> None:
    # Insert a user profile and pending/running/failed jobs.
    status = onboarding.get_onboarding_status("default")
    assert status["has_profile"] is True
    assert status["profile"]["nickname"] == "测试账号"
    assert status["jobs"]["pending"] == 1
    assert status["jobs"]["running"] == 1
    assert status["jobs"]["failed"] == 1
    assert status["jobs"]["needs_attention"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_onboarding.py
```

Expected: fails with `ImportError` or `AttributeError` because `src.onboarding` does not exist.

- [ ] **Step 3: Implement minimal status module**

Create `src/onboarding.py` with:

- `get_onboarding_status(user_id: str = DEFAULT_USER_ID) -> dict`
- private count helpers using `get_content_kind`
- private vector count helpers that support default legacy vector ids and scoped ids
- private job summary helper
- profile display fields from `users`

Return keys:

```python
{
    "user_id": "default",
    "needs_setup": bool,
    "has_any_items": bool,
    "has_profile": bool,
    "profile": {
        "nickname": str | None,
        "unique_id": str | None,
        "avatar_url": str | None,
        "updated_at": object | None,
    },
    "favorites": {"label": "收藏", "total": int, "indexed": int, "needs_index": bool},
    "likes": {"label": "喜欢", "total": int, "indexed": int, "needs_index": bool},
    "jobs": {"pending": int, "running": int, "failed": int, "success": int, "total": int, "needs_attention": bool},
}
```

- [ ] **Step 4: Run status tests**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_onboarding.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Commit:

```powershell
git add src/onboarding.py tests/test_onboarding.py
git commit -m "Add onboarding status summary"
```

## Task 2: Setup Page Routes and Templates

**Files:**
- Modify: `D:\douyinclaude\src\web\app.py`
- Create: `D:\douyinclaude\src\web\templates\setup.html`
- Create: `D:\douyinclaude\src\web\templates\_setup_status.html`
- Modify: `D:\douyinclaude\tests\test_web_templates.py`

- [ ] **Step 1: Write failing template tests**

Add tests to `tests/test_web_templates.py` asserting:

```python
def test_setup_page_contains_first_run_sections_and_reuses_existing_endpoints() -> None:
    setup = read_template("setup.html")
    setup_status = read_template("_setup_status.html")
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert "本地环境" in setup
    assert "绑定抖音账号" in setup
    assert "同步数据" in setup
    assert "生成搜索索引" in setup
    assert "完成" in setup
    assert 'hx-post="/auth/start"' in setup
    assert 'hx-post="/jobs/sync"' in setup
    assert 'hx-post="/jobs/index"' in setup
    assert 'hx-get="/setup/status"' in setup
    assert "favorites.total" in setup_status
    assert "likes.total" in setup_status
    assert '"/setup"' in app_source
    assert "get_onboarding_status" in app_source
```

- [ ] **Step 2: Run template tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_web_templates.py
```

Expected: fails because `setup.html` and app route do not exist.

- [ ] **Step 3: Add routes**

Modify `src/web/app.py`:

- Import `src.onboarding`.
- Add `@app.get("/setup")`.
- Add `@app.get("/setup/status")`.
- Pass `onboarding_status` into template contexts.

Route behavior:

```python
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    user_id = _current_user_id(request)
    session = _douyin_auth_sessions.get(user_id, {})
    status = onboarding.get_onboarding_status(user_id)
    return templates.TemplateResponse(request, "setup.html", {...})
```

- [ ] **Step 4: Add templates**

Create `setup.html`:

- Extends `base.html`.
- Uses five sections:
  - 本地环境
  - 绑定抖音账号
  - 同步数据
  - 生成搜索索引
  - 完成
- Includes existing `_auth_status.html`.
- Polls `/setup/status` every 5 seconds.
- Uses existing job forms:
  - favorites sync
  - likes sync
  - favorites index
  - likes index

Create `_setup_status.html`:

- Shows favorites total/indexed.
- Shows likes total/indexed.
- Shows pending/running/failed job counts.
- Shows next-step text derived from `status`.

- [ ] **Step 5: Run template tests**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_web_templates.py
```

Expected: all template tests pass.

- [ ] **Step 6: Commit**

Commit:

```powershell
git add src/web/app.py src/web/templates/setup.html src/web/templates/_setup_status.html tests/test_web_templates.py
git commit -m "Add first-run setup page"
```

## Task 3: Home Empty-State Setup Entry

**Files:**
- Modify: `D:\douyinclaude\src\web\app.py`
- Modify: `D:\douyinclaude\src\web\templates\index.html`
- Modify: `D:\douyinclaude\tests\test_web_templates.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_web_templates.py`:

```python
def test_home_empty_state_links_to_setup() -> None:
    index = read_template("index.html")
    app_source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert 'href="/setup"' in index
    assert "开始设置" in index
    assert "onboarding_status" in app_source
```

- [ ] **Step 2: Run template tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_web_templates.py
```

Expected: fails until the home template and context are updated.

- [ ] **Step 3: Update home context and empty state**

Modify `_index_for_kind` in `src/web/app.py` so it includes:

```python
"onboarding_status": onboarding.get_onboarding_status(user_id),
```

Modify `index.html` so when there are no items for the current module it shows a concise empty state with a `/setup` link.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_web_templates.py
```

Expected: all template tests pass.

- [ ] **Step 5: Commit**

Commit:

```powershell
git add src/web/app.py src/web/templates/index.html tests/test_web_templates.py
git commit -m "Link empty home state to setup"
```

## Task 4: Documentation and Verification

**Files:**
- Modify: `D:\douyinclaude\README.md`
- Modify: `D:\douyinclaude\docs\roadmap.md`

- [ ] **Step 1: Update docs**

Update README Windows installer section:

- Mention first launch opens `/setup`.
- Mention first sync and index can take time.
- Mention data remains local under `data/`.

Update roadmap “Windows 安装体验 / 首次使用流程”:

- Mark first-run setup as present or in progress, depending on implementation state.

- [ ] **Step 2: Run full verification**

Run:

```powershell
.\.venv\Scripts\python.exe tests\test_onboarding.py
.\.venv\Scripts\python.exe tests\test_web_templates.py
.\.venv\Scripts\python.exe tests\test_jobs.py
.\.venv\Scripts\python.exe tests\test_parser.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_windows_packaging.py -v
.\.venv\Scripts\python.exe -m compileall -q src tests
```

Expected: all commands exit 0.

- [ ] **Step 3: Sensitive-file scan**

Run:

```powershell
$tracked = git ls-files
$badPaths = $tracked | Where-Object { $_ -match '(^|/)\.env$' -or $_ -match '(^|/)\.env\.(local|production|development|test)$' -or $_ -match '^data/' -or $_ -match '(^|/)\.venv/' -or $_ -match '(^|/)\.claude/' -or $_ -match '(^|/)AGENTS\.md$' -or $_ -match '(^|/).+\.db$' }
if ($badPaths) { $badPaths | ForEach-Object { "tracked-private-path=$_" }; exit 1 }
$patterns = @('sk-[A-Za-z0-9_-]{20,}', 'ghp_[A-Za-z0-9_]{20,}', 'github_pat_[A-Za-z0-9_]{20,}', 'AKIA[0-9A-Z]{16}', 'AIza[0-9A-Za-z_-]{20,}', 'xox[baprs]-[0-9A-Za-z-]{20,}')
foreach ($pattern in $patterns) {
  $matches = Select-String -Path $tracked -Pattern $pattern -ErrorAction SilentlyContinue
  if ($matches) { $matches | ForEach-Object { "sensitive-pattern-hit=$($_.Path):$($_.LineNumber):$($_.Pattern)" }; exit 1 }
}
"No private paths or configured token patterns found."
```

Expected: no private paths or token patterns found.

- [ ] **Step 4: Commit docs**

Commit:

```powershell
git add README.md docs/roadmap.md
git commit -m "Document first-run setup flow"
```

- [ ] **Step 5: Push**

Run:

```powershell
git status -sb
git push origin main
```

Expected: branch pushed and working tree clean.

## Self-Review

- Spec coverage: The plan covers status aggregation, `/setup`, HTMX progress, home entry, docs, and verification.
- Placeholder scan: No unresolved placeholder text is intentionally left for implementers.
- Scope control: Desktop shell, signing, auto-update, SMTP setup, and cloud/multi-user work remain out of scope.
