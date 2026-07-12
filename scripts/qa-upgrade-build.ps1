param(
    [Parameter(Mandatory = $true)]
    [string]$OldInstallerPath,
    [string]$NewInstallerPath = "D:\douyinclaude\packaging\windows\out\DouyinRecallSetup.exe",
    [string]$QaRoot = "D:\codexDownload\douyin-release-v0.1.21\upgrade-qa",
    [string]$PythonPath = "",
    [switch]$SkipUninstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$UninstallRegistryPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D520E24-23C6-4C2E-8C2D-7AF8A935E32F}_is1"
$DownloadRoot = [System.IO.Path]::GetFullPath("D:\codexDownload")

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-InstallerVersion {
    param(
        [string]$Path,
        [string]$ExpectedVersion
    )
    $version = (Get-Item -LiteralPath $Path).VersionInfo.ProductVersion
    if (-not $version -or -not $version.StartsWith($ExpectedVersion, [System.StringComparison]::Ordinal)) {
        throw "Expected installer version $ExpectedVersion, got '$version': $Path"
    }
}

function Invoke-PythonScript {
    param(
        [string]$ScriptPath,
        [string[]]$Arguments
    )
    & $PythonPath $ScriptPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python QA helper failed with exit code $LASTEXITCODE`: $ScriptPath"
    }
}

function Invoke-IsolatedInstaller {
    param(
        [string]$InstallerPath,
        [string]$AppRoot,
        [string]$Label
    )
    Write-Step "$Label silent install to isolated directory"
    $installArgs = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/NOICONS",
        "/DIR=$AppRoot"
    )
    $process = Start-Process `
        -FilePath $InstallerPath `
        -ArgumentList $installArgs `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "$Label installer failed with exit code $($process.ExitCode)"
    }
}

function Save-InnoRegistration {
    if (-not (Test-Path -LiteralPath $UninstallRegistryPath)) {
        return [pscustomobject]@{
            Exists = $false
            Values = @()
        }
    }

    Get-ItemProperty -LiteralPath $UninstallRegistryPath | Out-Null
    $item = Get-Item -LiteralPath $UninstallRegistryPath
    $values = @()
    foreach ($name in $item.GetValueNames()) {
        $values += [pscustomobject]@{
            Name = $name
            Value = $item.GetValue($name)
            Kind = $item.GetValueKind($name).ToString()
        }
    }

    return [pscustomobject]@{
        Exists = $true
        Values = $values
    }
}

function Convert-RegistryKindToPropertyType {
    param([string]$Kind)
    switch ($Kind) {
        "String" { return "String" }
        "ExpandString" { return "ExpandString" }
        "Binary" { return "Binary" }
        "DWord" { return "DWord" }
        "MultiString" { return "MultiString" }
        "QWord" { return "QWord" }
        default { return "String" }
    }
}

function Restore-InnoRegistration {
    param([object]$Snapshot)

    if ($null -eq $Snapshot) {
        return
    }

    if (-not $Snapshot.Exists) {
        if (Test-Path -LiteralPath $UninstallRegistryPath) {
            Remove-Item -LiteralPath $UninstallRegistryPath -Force
        }
        return
    }

    $parent = Split-Path -Parent $UninstallRegistryPath
    $leaf = Split-Path -Leaf $UninstallRegistryPath
    if (-not (Test-Path -LiteralPath $UninstallRegistryPath)) {
        New-Item -Path $parent -Name $leaf -Force | Out-Null
    }

    $savedNames = @{}
    foreach ($entry in $Snapshot.Values) {
        $savedNames[$entry.Name] = $true
    }

    $currentItem = Get-Item -LiteralPath $UninstallRegistryPath
    foreach ($name in $currentItem.GetValueNames()) {
        if (-not $savedNames.ContainsKey($name)) {
            Remove-ItemProperty -LiteralPath $UninstallRegistryPath -Name $name -ErrorAction SilentlyContinue
        }
    }

    foreach ($entry in $Snapshot.Values) {
        $propertyType = Convert-RegistryKindToPropertyType -Kind $entry.Kind
        New-ItemProperty `
            -LiteralPath $UninstallRegistryPath `
            -Name $entry.Name `
            -Value $entry.Value `
            -PropertyType $propertyType `
            -Force | Out-Null
    }
}

if (-not (Test-Path -LiteralPath $DownloadRoot)) {
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
}
$QaRoot = [System.IO.Path]::GetFullPath($QaRoot)
$qaRootIsManaged = $QaRoot.Equals($DownloadRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $QaRoot.StartsWith($DownloadRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
Assert-True -Condition $qaRootIsManaged -Message "QaRoot must stay under D:\codexDownload: $QaRoot"

if (-not $PythonPath) {
    $PythonPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"
}
foreach ($requiredFile in @($OldInstallerPath, $NewInstallerPath, $PythonPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file not found: $requiredFile"
    }
}
$OldInstallerPath = (Resolve-Path -LiteralPath $OldInstallerPath).Path
$NewInstallerPath = (Resolve-Path -LiteralPath $NewInstallerPath).Path
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
Assert-InstallerVersion -Path $OldInstallerPath -ExpectedVersion "0.1.20"
Assert-InstallerVersion -Path $NewInstallerPath -ExpectedVersion "0.1.21"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runRoot = Join-Path $QaRoot "upgrade-$stamp-$PID"
$appRoot = Join-Path $runRoot "DouyinRecall"
$dataRoot = Join-Path $appRoot "data"
$dbPath = Join-Path $dataRoot "recall.db"
$reportPath = Join-Path $runRoot "qa-upgrade-result.json"
$seedPath = Join-Path $runRoot "seed-v0.1.20-db.py"
$verifyPath = Join-Path $runRoot "verify-v0.1.21-upgrade.py"
$verifyUninstallPath = Join-Path $runRoot "verify-uninstall-data.py"
$tempRoot = Join-Path $runRoot "temp"
$hfCacheRoot = Join-Path $runRoot "hf-cache"
$originalTemp = $env:TEMP
$originalTmp = $env:TMP
$originalHfHome = $env:HF_HOME
$originalSentenceTransformersHome = $env:SENTENCE_TRANSFORMERS_HOME
$originalRegistration = Save-InnoRegistration

New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
New-Item -ItemType Directory -Path $hfCacheRoot -Force | Out-Null
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:HF_HOME = $hfCacheRoot
$env:SENTENCE_TRANSFORMERS_HOME = Join-Path $hfCacheRoot "sentence-transformers"

$seedCode = @'
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import sqlite_vec


db_path = Path(sys.argv[1])
db_path.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(db_path)
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
conn.executescript(
    """
    PRAGMA foreign_keys = ON;
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL
    );
    CREATE TABLE favorites (
        user_id TEXT NOT NULL DEFAULT 'default',
        id TEXT NOT NULL,
        title TEXT,
        description TEXT,
        author TEXT,
        author_id TEXT,
        video_url TEXT,
        cover_url TEXT,
        duration_ms INTEGER,
        favorited_at TIMESTAMP,
        first_seen_at TIMESTAMP NOT NULL,
        last_seen_at TIMESTAMP NOT NULL,
        last_recalled_at TIMESTAMP,
        user_note TEXT,
        raw_json TEXT,
        is_removed INTEGER NOT NULL DEFAULT 0,
        discovery_index INTEGER,
        video_tags TEXT,
        llm_tags TEXT,
        video_created_at TIMESTAMP,
        digg_count INTEGER,
        PRIMARY KEY (user_id, id)
    );
    CREATE TABLE likes (
        user_id TEXT NOT NULL DEFAULT 'default',
        id TEXT NOT NULL,
        title TEXT,
        description TEXT,
        author TEXT,
        author_id TEXT,
        video_url TEXT,
        cover_url TEXT,
        duration_ms INTEGER,
        liked_at TIMESTAMP,
        first_seen_at TIMESTAMP NOT NULL,
        last_seen_at TIMESTAMP NOT NULL,
        last_recalled_at TIMESTAMP,
        user_note TEXT,
        raw_json TEXT,
        is_removed INTEGER NOT NULL DEFAULT 0,
        discovery_index INTEGER,
        video_tags TEXT,
        llm_tags TEXT,
        video_created_at TIMESTAMP,
        digg_count INTEGER,
        PRIMARY KEY (user_id, id)
    );
    CREATE VIRTUAL TABLE favorites_vec USING vec0(
        id TEXT PRIMARY KEY,
        embedding FLOAT[1024]
    );
    CREATE VIRTUAL TABLE likes_vec USING vec0(
        id TEXT PRIMARY KEY,
        embedding FLOAT[1024]
    );
    CREATE VIRTUAL TABLE favorites_fts USING fts5(
        id UNINDEXED,
        title,
        description,
        author,
        user_note,
        tokenize = 'unicode61'
    );
    CREATE VIRTUAL TABLE likes_fts USING fts5(
        id UNINDEXED,
        title,
        description,
        author,
        user_note,
        tokenize = 'unicode61'
    );
    """
)
now = "2026-07-12T00:00:00+00:00"
for user_id in ("default", "alice"):
    conn.execute(
        "INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
        (user_id, f"Upgrade QA {user_id}", now),
    )
    favorite_id = f"{user_id}-favorite"
    like_id = f"{user_id}-like"
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, description, author, video_url,
            favorited_at, first_seen_at, last_seen_at, raw_json,
            is_removed, discovery_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 0, 1)
        """,
        (
            user_id,
            favorite_id,
            f"upgrade recovery favorite {user_id}",
            "legacy schema favorite survives installer upgrade",
            "Upgrade QA",
            f"https://example.test/{favorite_id}",
            now,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO likes (
            user_id, id, title, description, author, video_url,
            liked_at, first_seen_at, last_seen_at, raw_json,
            is_removed, discovery_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 0, 1)
        """,
        (
            user_id,
            like_id,
            f"upgrade recovery like {user_id}",
            "legacy schema like survives installer upgrade",
            "Upgrade QA",
            f"https://example.test/{like_id}",
            now,
            now,
            now,
        ),
    )
    embedding = sqlite_vec.serialize_float32([1.0] * 1024)
    conn.execute(
        "INSERT INTO favorites_vec (id, embedding) VALUES (?, ?)",
        (favorite_id, embedding),
    )
    conn.execute(
        "INSERT INTO likes_vec (id, embedding) VALUES (?, ?)",
        (like_id, embedding),
    )
    conn.execute(
        "INSERT INTO favorites_fts (id, title, description, author, user_note) VALUES (?, ?, '', '', '')",
        (favorite_id, f"upgrade recovery favorite {user_id}"),
    )
    conn.execute(
        "INSERT INTO likes_fts (id, title, description, author, user_note) VALUES (?, ?, '', '', '')",
        (like_id, f"upgrade recovery like {user_id}"),
    )
conn.commit()
assert {row[1] for row in conn.execute("PRAGMA table_info(favorites_vec)")} == {"id", "embedding"}
assert "user_id" not in {row[1] for row in conn.execute("PRAGMA table_info(favorites_fts)")}
assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 2
assert conn.execute("SELECT COUNT(*) FROM likes").fetchone()[0] == 2
assert conn.execute("SELECT COUNT(*) FROM favorites_vec").fetchone()[0] == 2
assert conn.execute("SELECT COUNT(*) FROM likes_fts").fetchone()[0] == 2
conn.close()
'@

$verifyCode = @'
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import sqlite_vec


app_root = Path(sys.argv[1]).resolve()
db_path = Path(sys.argv[2]).resolve()
backup_path = Path(sys.argv[3]).resolve()
report_path = Path(sys.argv[4]).resolve()


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


backup = sqlite3.connect(backup_path)
try:
    backup.enable_load_extension(True)
    sqlite_vec.load(backup)
    backup.enable_load_extension(False)
    assert columns(backup, "favorites_vec") == {"id", "embedding"}
    assert "user_id" not in columns(backup, "favorites_fts")
    assert backup.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 2
    assert backup.execute("SELECT COUNT(*) FROM likes").fetchone()[0] == 2
    assert backup.execute("SELECT COUNT(*) FROM favorites_vec").fetchone()[0] == 2
    assert backup.execute("SELECT COUNT(*) FROM likes_fts").fetchone()[0] == 2
finally:
    backup.close()

os.environ["DB_PATH"] = str(db_path)
os.environ["WEB_AUTH_REQUIRED"] = "true"
sys.path.insert(0, str(app_root))

from src import db, jobs  # noqa: E402
from src.embedding import indexer  # noqa: E402
from src.search import hybrid  # noqa: E402
from src.web import runtime  # noqa: E402


class FakeEncoder:
    def encode(self, texts, batch_size=32):
        return np.ones((len(texts), 1024), dtype=np.float32)


fake_encoder = FakeEncoder()
indexer.get_encoder = lambda: fake_encoder
hybrid.get_encoder = lambda: fake_encoder

db.init_schema()
pending_before_worker = {
    (item["user_id"], item["content_kind"], item["reason"])
    for item in db.list_pending_search_reindexes()
}
expected_pending = {
    (user_id, content_kind, "search_index_schema_rebuilt")
    for user_id in ("default", "alice")
    for content_kind in ("favorites", "likes")
}
assert pending_before_worker == expected_pending, (pending_before_worker, expected_pending)

runtime.start_background_workers()
try:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not db.list_pending_search_reindexes():
            break
        time.sleep(0.1)
    pending_after_worker = db.list_pending_search_reindexes()
    if pending_after_worker:
        job_state = [
            dict(row)
            for row in db.get_connection().execute(
                "SELECT id, user_id, kind, status, attempts, error_message FROM job_queue ORDER BY id"
            )
        ]
        raise AssertionError(
            f"background worker did not finish durable reindex markers: {pending_after_worker}; jobs={job_state}"
        )
finally:
    runtime.stop_background_workers()

conn = db.get_connection()
assert "user_id" in columns(conn, "favorites_vec")
assert "user_id" in columns(conn, "favorites_fts")
assert "user_id" in columns(conn, "likes_vec")
assert "user_id" in columns(conn, "likes_fts")
assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 2
assert conn.execute("SELECT COUNT(*) FROM likes").fetchone()[0] == 2

counts = {}
hits = {}
for user_id in ("default", "alice"):
    counts[user_id] = {}
    hits[user_id] = {}
    for content_kind, expected_id in (
        ("favorites", f"{user_id}-favorite"),
        ("likes", f"{user_id}-like"),
    ):
        item_counts = db.search_index_counts(user_id, content_kind)
        assert item_counts == {"active": 1, "vector": 1, "fts": 1}, item_counts
        counts[user_id][content_kind] = item_counts
        result_ids = [
            hit.id
            for hit in hybrid.search_for_kind(
                "upgrade recovery",
                user_id=user_id,
                content_kind=content_kind,
            )
        ]
        assert expected_id in result_ids, (user_id, content_kind, result_ids)
        hits[user_id][content_kind] = result_ids

successful_jobs = conn.execute(
    "SELECT COUNT(*) FROM job_queue WHERE kind = 'index' AND status = 'success'"
).fetchone()[0]
assert successful_jobs == 4, successful_jobs
report = {
    "status": "passed",
    "old_version": "0.1.20",
    "new_version": "0.1.21",
    "app_root": str(app_root),
    "database": str(db_path),
    "preinstall_backup": str(backup_path),
    "content_counts": {"favorites": 2, "likes": 2},
    "pending_before_worker": sorted([list(item) for item in pending_before_worker]),
    "pending_after_worker": [],
    "successful_index_jobs": successful_jobs,
    "index_counts": counts,
    "search_hits": hits,
    "fake_encoder": True,
    "uninstall_verified": False,
}
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
db.close_connection()
'@

$verifyUninstallCode = @'
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


db_path = Path(sys.argv[1])
assert db_path.is_file(), f"uninstaller removed user database: {db_path}"
conn = sqlite3.connect(db_path)
try:
    assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM likes").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM favorites_fts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM likes_fts").fetchone()[0] == 2
finally:
    conn.close()
'@

try {
    Set-Content -LiteralPath $seedPath -Value $seedCode -Encoding UTF8
    Set-Content -LiteralPath $verifyPath -Value $verifyCode -Encoding UTF8
    Set-Content -LiteralPath $verifyUninstallPath -Value $verifyUninstallCode -Encoding UTF8

    Invoke-IsolatedInstaller `
        -InstallerPath $OldInstallerPath `
        -AppRoot $appRoot `
        -Label "v0.1.20"
    Assert-True `
        -Condition (Test-Path -LiteralPath (Join-Path $appRoot "src\db.py") -PathType Leaf) `
        -Message "v0.1.20 install is missing src\db.py: $appRoot"

    Write-Step "Seed a populated v0.1.20 search schema"
    Invoke-PythonScript -ScriptPath $seedPath -Arguments @($dbPath)

    Invoke-IsolatedInstaller `
        -InstallerPath $NewInstallerPath `
        -AppRoot $appRoot `
        -Label "v0.1.21 in-place upgrade"
    $installedProject = Get-Content -Raw -LiteralPath (Join-Path $appRoot "pyproject.toml")
    Assert-True `
        -Condition $installedProject.Contains('version = "0.1.21"') `
        -Message "In-place upgrade did not install v0.1.21 source."

    Write-Step "Verify installer-created pre-upgrade database backup"
    $preinstallBackups = @(
        Get-ChildItem `
            -LiteralPath (Join-Path $dataRoot "exports") `
            -Filter "pre-install-recall-*.db" `
            -File
    )
    Assert-True `
        -Condition ($preinstallBackups.Count -eq 1) `
        -Message "Expected exactly one pre-install backup, found $($preinstallBackups.Count)."
    $preinstallBackup = $preinstallBackups[0].FullName

    Write-Step "Run migrated app startup and wait for automatic background reindex"
    Invoke-PythonScript `
        -ScriptPath $verifyPath `
        -Arguments @($appRoot, $dbPath, $preinstallBackup, $reportPath)

    if (-not $SkipUninstall) {
        Write-Step "Run isolated uninstaller and verify user data is retained"
        $uninstallers = @(
            Get-ChildItem -LiteralPath $appRoot -Filter "unins*.exe" -File |
                Sort-Object LastWriteTime -Descending
        )
        Assert-True `
            -Condition ($uninstallers.Count -gt 0) `
            -Message "No Inno uninstaller was found under $appRoot"
        $uninstallArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        $uninstall = Start-Process `
            -FilePath $uninstallers[0].FullName `
            -ArgumentList $uninstallArgs `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
        if ($uninstall.ExitCode -ne 0) {
            throw "Isolated uninstaller failed with exit code $($uninstall.ExitCode)"
        }
        Invoke-PythonScript -ScriptPath $verifyUninstallPath -Arguments @($dbPath)
        Assert-True `
            -Condition (-not (Test-Path -LiteralPath (Join-Path $appRoot "src\db.py") -PathType Leaf)) `
            -Message "Uninstaller left installed application source in place."
        $report = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
        $report.uninstall_verified = $true
        $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    }

    Write-Step "Upgrade QA passed"
    Write-Host "QA root: $runRoot"
    Write-Host "Report: $reportPath"
}
finally {
    try {
        Restore-InnoRegistration -Snapshot $originalRegistration
    }
    finally {
        $env:TEMP = $originalTemp
        $env:TMP = $originalTmp
        $env:HF_HOME = $originalHfHome
        $env:SENTENCE_TRANSFORMERS_HOME = $originalSentenceTransformersHome
    }
}
