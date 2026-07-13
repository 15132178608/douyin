from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def passing_pre_release_backup(output_dir: Path) -> dict:
    backup_path = output_dir / "pre-release-recall-test.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    create_release_gate_source_db(backup_path)
    from relcheck import release_gate

    return {
        "name": "pre_release_backup",
        "ok": True,
        "exit_code": 0,
        "elapsed_seconds": 0.01,
        "command": ["internal", "pre_release_backup"],
        "stdout": "ok",
        "stderr": "",
        "artifacts": {
            "backup": str(backup_path),
            "report": str(output_dir / "pre-release-backup-test.json"),
        },
        "backup": {
            "path": str(backup_path),
            "sha256": release_gate._sha256(backup_path),
            "size_bytes": 123,
            "source_counts": {"users": 1, "favorites": 1, "likes": 1},
            "backup_counts": {"users": 1, "favorites": 1, "likes": 1},
            "validation": {"ok": True},
        },
    }


def passing_manifest_rollback(manifest_path: Path) -> dict:
    return {
        "name": "manifest_rollback_dry_run",
        "ok": True,
        "exit_code": 0,
        "elapsed_seconds": 0.01,
        "command": ["internal", "manifest_rollback_dry_run"],
        "stdout": "ok",
        "stderr": "",
        "artifacts": {"manifest": str(manifest_path)},
        "rollback": {"ok": True, "mode": "dry_run", "restored": False},
    }


class FakeResponse:
    def __init__(self, text: str = "ok", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")


class FakeClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def get(self, path: str) -> FakeResponse:
        self.paths.append(path)
        return FakeResponse(f"response for {path}")


def test_web_page_benchmark_covers_required_pages_and_writes_report(tmp_path: Path) -> None:
    from relcheck import performance_benchmark

    client = FakeClient()
    report = performance_benchmark.run_page_benchmarks(client=client, repeat=2)
    output = performance_benchmark.write_benchmark_report(report, tmp_path)

    names = [page["name"] for page in report["pages"]]
    assert names == ["首页", "收藏", "喜欢", "分类", "维护", "账号页"]
    assert client.paths == ["/", "/?p=1&page_size=32", "/likes", "/categories", "/maintenance", "/auth"] * 2
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "Douyin Recall Web Benchmark" in text
    assert "账号页" in text
    assert "avg_ms" in text


def test_database_safety_audit_runs_required_operations_and_preserves_fields(tmp_path: Path) -> None:
    from relcheck import database_safety

    report = database_safety.run_database_safety_audit(tmp_path)

    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "sync",
        "logout",
        "switch_account",
        "category_import",
        "backup_restore",
    ]
    for check in report["checks"]:
        assert check["before"]["counts"]["favorites"] == check["after"]["counts"]["favorites"]
        assert check["before"]["counts"]["likes"] == check["after"]["counts"]["likes"]
        assert check["mismatches"] == []
    assert "user_note" in report["protected_fields"]["favorites"]
    assert "category_id" in report["protected_fields"]["likes"]


def test_database_safety_audit_can_rerun_in_same_work_directory(tmp_path: Path) -> None:
    from relcheck import database_safety

    first = database_safety.run_database_safety_audit(tmp_path)
    second = database_safety.run_database_safety_audit(tmp_path)

    assert first["ok"] is True
    assert second["ok"] is True
    assert [check["name"] for check in second["checks"]] == [
        "sync",
        "logout",
        "switch_account",
        "category_import",
        "backup_restore",
    ]


def test_backup_restore_drill_restores_exact_snapshot_and_reports_field_mismatches(tmp_path: Path) -> None:
    from relcheck import backup_drill

    drill = backup_drill.run_backup_restore_drill(tmp_path)

    assert drill["ok"] is True
    assert Path(drill["backup_path"]).exists()
    assert Path(drill["restored_path"]).exists()
    assert drill["mismatches"] == []
    assert drill["failure_messages"] == []
    assert "favorites" in drill["compared_tables"]
    assert drill["counts"]["before"]["favorites"] == 1
    assert drill["counts"]["before"]["likes"] == 1
    assert drill["counts"]["damaged"]["favorites"] == 1
    assert drill["counts"]["damaged"]["likes"] == 0
    assert drill["counts"]["after"] == drill["counts"]["before"]
    assert drill["damage"]["detected"] is True
    assert {
        "table": "favorites",
        "key": "fav-1",
        "field": "title",
        "before": "收藏标题",
        "after": "damaged",
    } in drill["damage"]["mismatches"]
    assert any(
        mismatch["table"] == "likes"
        and mismatch["key"] == "like-1"
        and mismatch["field"] == "<row>"
        and mismatch["after"] is None
        for mismatch in drill["damage"]["mismatches"]
    )
    mismatches = backup_drill.compare_snapshots(
        {"favorites": {"fav-1": {"title": "before"}}},
        {"favorites": {"fav-1": {"title": "after"}}},
    )
    assert mismatches == [
        {
            "table": "favorites",
            "key": "fav-1",
            "field": "title",
            "before": "before",
            "after": "after",
        }
    ]
    assert backup_drill.format_mismatches(mismatches) == [
        "favorites[fav-1].title: before -> after"
    ]


def test_query_performance_audit_compares_before_and_after_indexes() -> None:
    from relcheck import query_performance

    report = query_performance.run_query_performance_audit(
        row_count=1200,
        repeats=1,
        slow_threshold_ms=0.01,
    )

    assert report["row_count"] == 1200
    assert report["dataset"] == {"favorites_rows": 1200, "likes_rows": 1200}
    assert report["slow_threshold_ms"] == 0.01
    assert report["summary"]["query_count"] == 8
    assert report["summary"]["content_kinds"] == ["favorites", "likes"]
    assert report["summary"]["surfaces"] == ["author_page", "category", "home", "search"]
    assert report["summary"]["all_expected_indexes_used_after"] is True
    assert set(report["summary"]["expected_indexes"]) == {
        "idx_fav_active_order",
        "idx_fav_active_category_order",
        "idx_fav_active_author_order",
        "idx_like_active_order",
        "idx_like_active_category_order",
        "idx_like_active_author_order",
    }
    assert [item["name"] for item in report["queries"]] == [
        "home_list",
        "likes_home_list",
        "category_list",
        "likes_category_list",
        "author_page",
        "likes_author_page",
        "search_favorite",
        "search_like",
    ]
    for item in report["queries"]:
        assert item["content_kind"] in {"favorites", "likes"}
        assert item["surface"] in {"home", "category", "author_page", "search"}
        assert item["expected_index"]
        assert item["expected_index_used_after"] is True
        assert item["main_table_scan_after"] is False
        assert item["before_ms"] >= 0
        assert item["after_ms"] >= 0
        assert item["delta_ms"] == item["after_ms"] - item["before_ms"]
        assert "improvement_ratio" in item
        assert "slow_before" in item
        assert "slow_after" in item
        assert item["plan_after"]


def test_query_performance_report_writes_machine_readable_json(tmp_path: Path) -> None:
    from relcheck import query_performance

    report = query_performance.run_query_performance_audit(row_count=1200, repeats=1)
    json_path = tmp_path / "query-performance-audit.json"

    query_performance.write_query_performance_json(report, json_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["row_count"] == 1200
    assert payload["summary"]["query_count"] == 8
    assert "after_ms" in payload["queries"][0]
    assert "expected_index_used_after" in payload["queries"][0]


def create_release_gate_source_db(path: Path) -> None:
    from src import db

    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        """
        INSERT INTO users (id, display_name, created_at)
        VALUES ('default', '本地默认用户', '2026-07-07 00:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO favorites (
            user_id, id, title, first_seen_at, last_seen_at, is_removed
        ) VALUES ('default', 'fav-1', '发布前收藏', '2026-07-07', '2026-07-07', 0)
        """
    )
    conn.execute(
        """
        INSERT INTO likes (
            user_id, id, title, first_seen_at, last_seen_at, is_removed
        ) VALUES ('default', 'like-1', '发布前喜欢', '2026-07-07', '2026-07-07', 0)
        """
    )
    conn.commit()
    conn.close()


def test_pre_release_backup_check_creates_validated_rollback_point_and_report(tmp_path: Path) -> None:
    from relcheck import release_gate

    source_db = tmp_path / "recall.db"
    backup_dir = tmp_path / "exports"
    output_dir = tmp_path / "release-checks"
    create_release_gate_source_db(source_db)

    check = release_gate.check_pre_release_backup(
        output_dir=output_dir,
        db_path=source_db,
        backup_dir=backup_dir,
    )

    backup = check["backup"]
    report_path = Path(check["artifacts"]["report"])
    backup_path = Path(check["artifacts"]["backup"])
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert check["ok"] is True
    assert check["name"] == "pre_release_backup"
    assert backup_path.exists()
    assert backup_path.name.startswith("pre-release-recall-")
    assert backup["path"] == str(backup_path)
    assert backup["sha256"] == release_gate._sha256(backup_path)
    assert backup["size_bytes"] > 0
    assert backup["validation"]["ok"] is True
    assert backup["source_counts"]["users"] == 1
    assert backup["source_counts"]["favorites"] == 1
    assert backup["source_counts"]["likes"] == 1
    assert backup["backup_counts"] == backup["source_counts"]
    assert report["backup"]["sha256"] == backup["sha256"]
    assert report["backup"]["backup_counts"] == backup["source_counts"]


def test_operational_script_files_expose_one_command_entrypoints() -> None:
    expected = {
        "scripts/benchmark_web_pages.py": "run_page_benchmarks",
        "scripts/database_safety_audit.py": "run_database_safety_audit",
        "scripts/backup_restore_drill.py": "run_backup_restore_drill",
        "scripts/query_performance_audit.py": "run_query_performance_audit",
        "scripts/acceptance_matrix.py": "build_acceptance_matrix",
        "scripts/validate_delivery_evidence.py": "validate_delivery_manifest_evidence",
        "scripts/preflight_summary.py": "build_preflight_summary",
        "scripts/final_release_check.py": "run_final_release_check",
        "scripts/final_release_check.ps1": "final_release_check.py",
        "scripts/release_gate.ps1": "release_gate.py",
    }
    for path, symbol in expected.items():
        text = Path(path).read_text(encoding="utf-8")
        assert symbol in text
    release_gate_script = Path("scripts/release_gate.ps1").read_text(encoding="utf-8")
    assert "UpdatePerformanceBaseline" in release_gate_script
    assert "--update-performance-baseline" in release_gate_script
    assert "KeepReleaseEvidence" in release_gate_script
    assert "--keep-release-evidence" in release_gate_script
    assert "SkipEvidenceCleanup" in release_gate_script
    assert "--skip-evidence-cleanup" in release_gate_script


def test_acceptance_matrix_covers_original_goal_and_writes_reports(tmp_path: Path) -> None:
    from relcheck import acceptance_matrix

    report = acceptance_matrix.build_acceptance_matrix()
    paths = acceptance_matrix.write_acceptance_matrix_reports(report, tmp_path)

    assert report["ok"] is True
    assert report["summary"] == {"total": 12, "covered": 12, "missing": 0}
    assert [goal["id"] for goal in report["goals"]] == [
        "regression_tests",
        "performance_benchmark",
        "database_safety_audit",
        "job_queue_stability",
        "sync_idempotency",
        "category_import_migration",
        "diagnostics_layering",
        "backup_restore_drill",
        "account_boundaries",
        "query_performance",
        "crawler_state_machine",
        "maintenance_backend_capabilities",
    ]
    for goal in report["goals"]:
        assert goal["status"] == "covered"
        assert goal["tests"]
        assert goal["verification"]
        assert goal["missing"] == []
    assert "release_gate" in report["verification_commands"]
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert payload["summary"]["covered"] == 12
    assert "回归测试补强" in markdown
    assert "维护中心后端能力补强" in markdown
    assert "acceptance_matrix" in markdown


def write_delivery_evidence_manifest(root: Path) -> Path:
    release_dir = root / "release-checks"
    audits_dir = root / "audits"
    backup_drill_dir = audits_dir / "backup-restore-drill"
    benchmarks_dir = root / "benchmarks"
    exports_dir = root / "exports"
    for directory in (release_dir, audits_dir, backup_drill_dir, benchmarks_dir, exports_dir):
        directory.mkdir(parents=True, exist_ok=True)

    files = {
        "release_json": release_dir / "release-gate-test.json",
        "release_md": release_dir / "release-gate-test.md",
        "pre_backup_report": release_dir / "pre-release-backup-test.json",
        "pre_backup_db": exports_dir / "pre-release-recall-test.db",
        "doctor_report": release_dir / "doctor-report-test.json",
        "installed_smoke": release_dir / "installed-smoke-report.json",
        "database_safety": audits_dir / "database-safety-audit.json",
        "backup_drill": backup_drill_dir / "backup-restore-drill.json",
        "web_benchmark_json": benchmarks_dir / "web-benchmark-test.json",
        "web_benchmark_md": benchmarks_dir / "web-benchmark-test.md",
        "query_md": benchmarks_dir / "query-performance-audit.md",
        "query_json": benchmarks_dir / "query-performance-audit.json",
        "acceptance_json": release_dir / "acceptance-matrix.json",
        "acceptance_md": release_dir / "acceptance-matrix.md",
        "baseline": release_dir / "performance-baseline.json",
        "current": release_dir / "performance-current.json",
    }
    for name, path in files.items():
        if name.endswith("_db"):
            path.write_bytes(b"sqlite backup")
        else:
            path.write_text(json.dumps({"ok": True, "name": name}), encoding="utf-8")

    manifest_path = release_dir / "delivery-manifest-test.json"
    manifest = {
        "schema_version": 1,
        "ok": True,
        "release_gate": {
            "json": str(files["release_json"]),
            "markdown": str(files["release_md"]),
        },
        "installer": {"requested": False, "path": None, "sha256": None},
        "evidence": {
            "pre_release_backup": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {
                    "report": str(files["pre_backup_report"]),
                    "backup": str(files["pre_backup_db"]),
                },
                "backup": {
                    "path": str(files["pre_backup_db"]),
                    "sha256": "test-sha",
                    "source_counts": {"users": 1, "favorites": 1, "likes": 1},
                    "backup_counts": {"users": 1, "favorites": 1, "likes": 1},
                },
            },
            "pytest": {"ok": True, "exit_code": 0, "artifacts": {}},
            "doctor_json": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {"report": str(files["doctor_report"])},
            },
            "installed_smoke": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {"report": str(files["installed_smoke"])},
            },
            "database_safety_audit": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {"report": str(files["database_safety"])},
            },
            "backup_restore_drill": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {"report": str(files["backup_drill"])},
            },
            "web_benchmark": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {"report_dir": str(benchmarks_dir)},
            },
            "query_performance_audit": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {"report": str(files["query_md"])},
            },
            "acceptance_matrix": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {
                    "json": str(files["acceptance_json"]),
                    "markdown": str(files["acceptance_md"]),
                },
            },
            "performance_regression": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {
                    "web_benchmark": str(files["web_benchmark_json"]),
                    "query_performance": str(files["query_json"]),
                    "baseline": str(files["baseline"]),
                    "current": str(files["current"]),
                },
                "performance": {"regressions": []},
            },
            "manifest_rollback_dry_run": {
                "ok": True,
                "exit_code": 0,
                "artifacts": {
                    "manifest": str(manifest_path),
                    "backup": str(files["pre_backup_db"]),
                },
                "rollback": {"ok": True, "mode": "dry_run", "restored": False},
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def test_delivery_evidence_validator_checks_required_manifest_files_and_reports_missing(tmp_path: Path) -> None:
    from relcheck import delivery_evidence

    manifest_path = write_delivery_evidence_manifest(tmp_path)

    ok_report = delivery_evidence.validate_delivery_manifest_evidence(manifest_path)
    paths = delivery_evidence.write_delivery_evidence_report(ok_report, tmp_path / "release-checks")

    assert ok_report["ok"] is True
    assert ok_report["summary"] == {"total": 12, "passed": 12, "failed": 0}
    assert ok_report["missing_files"] == []
    assert ok_report["mismatches"] == []
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    assert "delivery_evidence" in paths["markdown"].read_text(encoding="utf-8")

    missing_path = tmp_path / "release-checks" / "acceptance-matrix.md"
    missing_path.unlink()

    failed_report = delivery_evidence.validate_delivery_manifest_evidence(manifest_path)

    assert failed_report["ok"] is False
    assert str(missing_path) in failed_report["missing_files"]
    assert any("acceptance_matrix.markdown" in item for item in failed_report["mismatches"])


def test_preflight_summary_aggregates_latest_reports_and_writes_chinese_markdown(tmp_path: Path) -> None:
    from relcheck import preflight_summary

    manifest_path = write_delivery_evidence_manifest(tmp_path)
    release_dir = tmp_path / "release-checks"
    benchmarks_dir = tmp_path / "benchmarks"
    audits_dir = tmp_path / "audits"
    delivery_report = {
        "ok": True,
        "summary": {"total": 12, "passed": 12, "failed": 0},
        "manifest_path": str(manifest_path),
    }
    (release_dir / "delivery-evidence-check.json").write_text(
        json.dumps(delivery_report, ensure_ascii=False),
        encoding="utf-8",
    )
    (release_dir / "acceptance-matrix.json").write_text(
        json.dumps({"ok": True, "summary": {"total": 12, "covered": 12, "missing": 0}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (release_dir / "performance-current.json").write_text(
        json.dumps(
            {
                "web_pages": {"首页": {"avg_ms": 101.5}},
                "queries": {"home_list": {"after_ms": 3.25}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (release_dir / "performance-baseline.json").write_text(
        json.dumps(
            {
                "web_pages": {"首页": {"avg_ms": 100.0}},
                "queries": {"home_list": {"after_ms": 3.0}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = preflight_summary.build_preflight_summary(
        release_checks_dir=release_dir,
        benchmarks_dir=benchmarks_dir,
        audits_dir=audits_dir,
    )
    paths = preflight_summary.write_preflight_summary(report, release_dir)

    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "release_gate",
        "delivery_evidence",
        "acceptance_matrix",
        "performance",
        "database_safety",
        "backup_restore_drill",
    ]
    assert report["missing_files"] == []
    assert report["checks"][3]["details"]["web_pages"]["首页"]["current_ms"] == 101.5
    assert report["checks"][3]["details"]["queries"]["home_list"]["baseline_ms"] == 3.0
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "发布前自检摘要" in markdown
    assert "发布门禁" in markdown
    assert "交付证据一致性" in markdown
    assert "数据库安全巡检" in markdown
    assert "备份恢复演练" in markdown

    (release_dir / "acceptance-matrix.json").unlink()
    failed = preflight_summary.build_preflight_summary(
        release_checks_dir=release_dir,
        benchmarks_dir=benchmarks_dir,
        audits_dir=audits_dir,
    )

    assert failed["ok"] is False
    assert str(release_dir / "acceptance-matrix.json") in failed["missing_files"]
    assert any(check["name"] == "acceptance_matrix" and check["ok"] is False for check in failed["checks"])


def test_final_release_check_runs_release_gate_evidence_and_preflight_in_order(tmp_path: Path) -> None:
    from relcheck import final_release_check

    calls: list[list[str]] = []

    def runner(command, cwd, env, timeout_seconds):
        calls.append(list(command))
        return final_release_check.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout="ok",
            stderr="",
        )

    output_dir = tmp_path / "release-checks"
    report = final_release_check.run_final_release_check(
        output_dir=output_dir,
        benchmarks_dir=tmp_path / "benchmarks",
        audits_dir=tmp_path / "audits",
        python_executable="python",
        runner=runner,
    )
    paths = final_release_check.write_final_release_check_report(report, output_dir)

    assert report["ok"] is True
    assert report["summary"] == {"total": 3, "passed": 3, "failed": 0, "skipped": 0}
    assert [step["name"] for step in report["steps"]] == [
        "release_gate",
        "delivery_evidence",
        "preflight_summary",
    ]
    assert [step["status"] for step in report["steps"]] == ["passed", "passed", "passed"]
    assert calls[0][:3] == ["python", "-m", "relcheck.release_gate"]
    assert "--skip-evidence-cleanup" in calls[0]
    assert "--output-dir" in calls[0]
    assert str(output_dir) in calls[0]
    assert calls[1][:2] == ["python", str(Path.cwd() / "scripts" / "validate_delivery_evidence.py")]
    assert calls[2][:2] == ["python", str(Path.cwd() / "scripts" / "preflight_summary.py")]
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "发布终检" in markdown
    assert "发布门禁" in markdown
    assert "交付证据复核" in markdown
    assert "发布前自检摘要" in markdown


def test_final_release_check_stops_after_release_gate_failure_and_marks_skipped(tmp_path: Path) -> None:
    from relcheck import final_release_check

    calls: list[list[str]] = []

    def runner(command, cwd, env, timeout_seconds):
        calls.append(list(command))
        return final_release_check.CommandResult(
            command=list(command),
            exit_code=2,
            elapsed_seconds=0.01,
            stdout="failed",
            stderr="boom",
        )

    report = final_release_check.run_final_release_check(
        output_dir=tmp_path / "release-checks",
        benchmarks_dir=tmp_path / "benchmarks",
        audits_dir=tmp_path / "audits",
        python_executable="python",
        runner=runner,
    )

    assert report["ok"] is False
    assert len(calls) == 1
    assert report["summary"] == {"total": 3, "passed": 0, "failed": 1, "skipped": 2}
    assert [step["status"] for step in report["steps"]] == ["failed", "skipped", "skipped"]
    assert report["failed_steps"] == ["release_gate"]
    assert report["steps"][1]["message"] == "前置检查失败，未执行。"


def test_release_gate_writes_doctor_report_from_large_json_stdout(tmp_path: Path) -> None:
    from relcheck import release_gate

    installer_path = tmp_path / "DouyinRecallSetup.exe"
    installer_path.write_bytes(b"test installer")

    def runner(command, cwd, env, timeout_seconds):
        stdout = f"ok: {command[-1]}"
        if command[-3:] == ["src.cli", "doctor", "--json"]:
            stdout = json.dumps(
                {
                    "ok": True,
                    "checked_at": "2026-07-07T00:00:00+00:00",
                    "checks": {
                        "database": {"ok": True, "message": "数据库正常"},
                        "large": {"ok": True, "items": ["x" * 200] * 80},
                    },
                },
                ensure_ascii=False,
            )
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout=stdout,
            stderr="",
        )

    def performance_checker(output_dir, update_baseline=False):
        current = tmp_path / "performance-current.json"
        current.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return {
            "name": "performance_regression",
            "ok": True,
            "exit_code": 0,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "performance within baseline thresholds",
            "stderr": "",
            "artifacts": {"current": str(current)},
            "performance": {"baseline_status": "compared", "regressions": []},
        }

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=performance_checker,
        manifest_rollback_checker=passing_manifest_rollback,
        include_installer_build=True,
        installer_path=installer_path,
    )
    manifest = json.loads(Path(report["reports"]["manifest_json"]).read_text(encoding="utf-8"))
    doctor_report = Path(manifest["evidence"]["doctor_json"]["artifacts"]["report"])

    assert doctor_report.exists()
    payload = json.loads(doctor_report.read_text(encoding="utf-8"))
    assert payload["checks"]["large"]["items"][0] == "x" * 200


def test_release_gate_runs_required_checks_and_writes_machine_and_markdown_reports(tmp_path: Path) -> None:
    from relcheck import release_gate

    commands: list[tuple[str, ...]] = []

    def runner(command, cwd, env, timeout_seconds):
        commands.append(tuple(command))
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout=f"ok: {command[-1]}",
            stderr="",
        )

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=lambda output_dir, update_baseline=False: {
            "name": "performance_regression",
            "ok": True,
            "exit_code": 0,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "ok",
            "stderr": "",
            "artifacts": {},
            "performance": {"regressions": []},
        },
        include_installer_build=True,
        installer_path=tmp_path / "DouyinRecallSetup.exe",
    )

    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "pre_release_backup",
        "pytest",
        "doctor_json",
        "installed_smoke",
        "database_safety_audit",
        "backup_restore_drill",
        "web_benchmark",
        "query_performance_audit",
        "acceptance_matrix",
        "performance_regression",
        "installer_build",
        "manifest_rollback_dry_run",
    ]
    assert commands[0][-3:] == ("-m", "pytest", "-q")
    assert any(command[-3:] == ("src.cli", "doctor", "--json") for command in commands)
    assert any("installed_smoke.py" in command for command in commands for command in command)
    assert any("database_safety_audit.py" in command[-1] for command in commands)
    assert any("backup_restore_drill.py" in command[-1] for command in commands)
    assert any("benchmark_web_pages.py" in command[-1] for command in commands)
    assert any("query_performance_audit.py" in command[-1] for command in commands)
    assert any("acceptance_matrix.py" in part for command in commands for part in command)
    assert report["reports"]["json"].endswith(".json")
    assert report["reports"]["markdown"].endswith(".md")
    assert Path(report["reports"]["json"]).exists()
    markdown = Path(report["reports"]["markdown"]).read_text(encoding="utf-8")
    assert "Douyin Recall Release Gate" in markdown
    assert "pytest" in markdown
    assert "installer_build" in markdown
    assert "manifest_rollback_dry_run" in markdown
    assert report["installer"]["path"].endswith("DouyinRecallSetup.exe")
    assert report["installer"]["sha256"] is None


def test_release_gate_custom_output_dir_routes_generated_evidence_to_same_root(tmp_path: Path) -> None:
    from relcheck import release_gate

    output_dir = tmp_path / "custom-release-checks"

    def runner(command, cwd, env, timeout_seconds):
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout="ok",
            stderr="",
        )

    report = release_gate.run_release_gate(
        output_dir=output_dir,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=lambda output_dir, update_baseline=False: {
            "name": "performance_regression",
            "ok": True,
            "exit_code": 0,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "ok",
            "stderr": "",
            "artifacts": {},
            "performance": {"regressions": []},
        },
        manifest_rollback_checker=passing_manifest_rollback,
    )

    installed_smoke = next(check for check in report["checks"] if check["name"] == "installed_smoke")
    installed_command = installed_smoke["command"]
    assert installed_command[installed_command.index("--app-root") + 1] == str(output_dir / "installed-smoke")
    assert installed_command[installed_command.index("--output-dir") + 1] == str(output_dir)
    assert installed_smoke["artifacts"] == {
        "report": str(output_dir / "installed-smoke-report.json"),
    }

    acceptance_matrix = next(check for check in report["checks"] if check["name"] == "acceptance_matrix")
    acceptance_command = acceptance_matrix["command"]
    assert acceptance_command[acceptance_command.index("--output-dir") + 1] == str(output_dir)
    assert acceptance_matrix["artifacts"] == {
        "json": str(output_dir / "acceptance-matrix.json"),
        "markdown": str(output_dir / "acceptance-matrix.md"),
    }

    manifest = json.loads(Path(report["reports"]["manifest_json"]).read_text(encoding="utf-8"))
    assert manifest["evidence"]["installed_smoke"]["artifacts"] == installed_smoke["artifacts"]
    assert manifest["evidence"]["acceptance_matrix"]["artifacts"] == acceptance_matrix["artifacts"]


def test_release_gate_writes_delivery_manifest_with_release_evidence(tmp_path: Path) -> None:
    from relcheck import release_gate

    installer_path = tmp_path / "DouyinRecallSetup.exe"
    installer_path.write_bytes(b"test installer")

    def runner(command, cwd, env, timeout_seconds):
        stdout = f"ok: {command[-1]}"
        if command[-3:] == ["src.cli", "doctor", "--json"]:
            stdout = json.dumps(
                {
                    "ok": True,
                    "checked_at": "2026-07-07T00:00:00+00:00",
                    "checks": {"database": {"ok": True, "message": "数据库正常"}},
                },
                ensure_ascii=False,
            )
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout=stdout,
            stderr="",
        )

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=lambda output_dir, update_baseline=False: {
            "name": "performance_regression",
            "ok": True,
            "exit_code": 0,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "performance within baseline thresholds",
            "stderr": "",
            "artifacts": {
                "web_benchmark": str(tmp_path / "web-benchmark-20260707-000000.json"),
                "query_performance": str(tmp_path / "query-performance-audit.json"),
                "baseline": str(tmp_path / "performance-baseline.json"),
                "current": str(tmp_path / "performance-current.json"),
            },
            "performance": {"baseline_status": "compared", "regressions": []},
        },
        include_installer_build=True,
        installer_path=installer_path,
    )

    manifest_json = Path(report["reports"]["manifest_json"])
    manifest_markdown = Path(report["reports"]["manifest_markdown"])
    manifest = json.loads(manifest_json.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["ok"] is True
    assert manifest["release_gate"]["json"] == report["reports"]["json"]
    assert manifest["release_gate"]["markdown"] == report["reports"]["markdown"]
    assert manifest["installer"]["path"] == str(installer_path)
    assert manifest["installer"]["sha256"] == release_gate._sha256(installer_path)
    assert Path(manifest["evidence"]["doctor_json"]["artifacts"]["report"]).exists()
    assert manifest["evidence"]["database_safety_audit"]["artifacts"]["report"].endswith(
        "database-safety-audit.json"
    )
    assert manifest["evidence"]["pre_release_backup"]["backup"]["sha256"] == release_gate._sha256(
        Path(manifest["evidence"]["pre_release_backup"]["backup"]["path"])
    )
    assert manifest["evidence"]["pre_release_backup"]["backup"]["source_counts"]["favorites"] == 1
    assert manifest["evidence"]["manifest_rollback_dry_run"]["rollback"]["restored"] is False
    assert manifest["evidence"]["backup_restore_drill"]["artifacts"]["report"].endswith(
        "backup-restore-drill.json"
    )
    assert manifest["evidence"]["web_benchmark"]["artifacts"]["report_dir"].endswith("benchmarks")
    assert manifest["evidence"]["query_performance_audit"]["artifacts"]["report"].endswith(
        "query-performance-audit.md"
    )
    assert manifest["evidence"]["performance_regression"]["artifacts"]["current"].endswith(
        "performance-current.json"
    )
    markdown = manifest_markdown.read_text(encoding="utf-8")
    assert "Douyin Recall Delivery Manifest" in markdown
    assert "database_safety_audit" in markdown
    assert "backup_restore_drill" in markdown
    assert "manifest_rollback_dry_run" in markdown
    assert str(installer_path) in markdown


def test_manifest_rollback_dry_run_check_validates_manifest_backup(tmp_path: Path) -> None:
    from relcheck import release_gate

    backup_path = tmp_path / "pre-release-recall-manifest.db"
    manifest_path = tmp_path / "delivery-manifest-check.json"
    create_release_gate_source_db(backup_path)
    sha256 = release_gate._sha256(backup_path)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ok": True,
                "evidence": {
                    "pre_release_backup": {
                        "ok": True,
                        "backup": {
                            "path": str(backup_path),
                            "sha256": sha256,
                            "source_counts": {"users": 1, "favorites": 1, "likes": 1},
                        },
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    check = release_gate.check_manifest_rollback_dry_run(manifest_path)

    assert check["ok"] is True
    assert check["name"] == "manifest_rollback_dry_run"
    assert check["artifacts"]["manifest"] == str(manifest_path)
    assert check["artifacts"]["backup"] == str(backup_path)
    assert check["rollback"]["mode"] == "dry_run"
    assert check["rollback"]["restored"] is False


def test_release_gate_fails_when_manifest_rollback_dry_run_fails(tmp_path: Path) -> None:
    from relcheck import release_gate

    def runner(command, cwd, env, timeout_seconds):
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout="ok",
            stderr="",
        )

    def failing_manifest_rollback(manifest_path: Path) -> dict:
        return {
            "name": "manifest_rollback_dry_run",
            "ok": False,
            "exit_code": 1,
            "elapsed_seconds": 0.01,
            "command": ["internal", "manifest_rollback_dry_run"],
            "stdout": "",
            "stderr": "SHA256 不匹配",
            "artifacts": {"manifest": str(manifest_path)},
            "rollback": {"ok": False, "mode": "dry_run", "restored": False},
        }

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=lambda output_dir, update_baseline=False: {
            "name": "performance_regression",
            "ok": True,
            "exit_code": 0,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "ok",
            "stderr": "",
            "artifacts": {},
            "performance": {"regressions": []},
        },
        manifest_rollback_checker=failing_manifest_rollback,
    )
    manifest = json.loads(Path(report["reports"]["manifest_json"]).read_text(encoding="utf-8"))

    assert report["ok"] is False
    assert report["checks"][-1]["name"] == "manifest_rollback_dry_run"
    assert report["checks"][-1]["ok"] is False
    assert "SHA256 不匹配" in report["checks"][-1]["stderr"]
    assert manifest["ok"] is False
    assert manifest["evidence"]["manifest_rollback_dry_run"]["ok"] is False


def test_release_gate_stops_when_pre_release_backup_fails_before_pytest(tmp_path: Path) -> None:
    from relcheck import release_gate

    calls: list[tuple[str, ...]] = []

    def runner(command, cwd, env, timeout_seconds):
        calls.append(tuple(command))
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout="ok",
            stderr="",
        )

    def failing_backup(output_dir):
        return {
            "name": "pre_release_backup",
            "ok": False,
            "exit_code": 1,
            "elapsed_seconds": 0.01,
            "command": ["internal", "pre_release_backup"],
            "stdout": "",
            "stderr": "备份校验失败",
            "artifacts": {},
            "backup": {"validation": {"ok": False}},
        }

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=failing_backup,
    )

    assert report["ok"] is False
    assert [check["name"] for check in report["checks"]] == ["pre_release_backup"]
    assert calls == []
    assert "备份校验失败" in report["checks"][0]["stderr"]


def test_release_gate_marks_failed_check_and_stops_later_gates(tmp_path: Path) -> None:
    from relcheck import release_gate

    calls: list[str] = []

    def runner(command, cwd, env, timeout_seconds):
        calls.append(command[-1])
        return release_gate.CommandResult(
            command=list(command),
            exit_code=1 if "database_safety_audit.py" in command[-1] else 0,
            elapsed_seconds=0.01,
            stdout="",
            stderr="database mismatch",
        )

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
    )

    assert report["ok"] is False
    failed = [check for check in report["checks"] if not check["ok"]]
    assert failed[0]["name"] == "database_safety_audit"
    assert "database mismatch" in failed[0]["stderr"]
    assert not any("backup_restore_drill.py" in call for call in calls)


def test_release_gate_stops_when_doctor_json_fails_before_installed_smoke(tmp_path: Path) -> None:
    from relcheck import release_gate

    calls: list[tuple[str, ...]] = []

    def runner(command, cwd, env, timeout_seconds):
        calls.append(tuple(command))
        return release_gate.CommandResult(
            command=list(command),
            exit_code=1 if command[-1] == "--json" else 0,
            elapsed_seconds=0.01,
            stdout="",
            stderr="doctor failed",
        )

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
    )

    assert report["ok"] is False
    assert [check["name"] for check in report["checks"]] == [
        "pre_release_backup",
        "pytest",
        "doctor_json",
    ]
    assert report["checks"][2]["stderr"] == "doctor failed"
    assert not any("installed_smoke.py" in part for command in calls for part in command)


def test_release_gate_stops_when_performance_regression_fails_before_installer(tmp_path: Path) -> None:
    from relcheck import release_gate

    def runner(command, cwd, env, timeout_seconds):
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout="ok",
            stderr="",
        )

    def performance_checker(output_dir, update_baseline=False):
        return {
            "name": "performance_regression",
            "ok": False,
            "exit_code": 1,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "",
            "stderr": "首页 regressed",
            "artifacts": {},
            "performance": {"regressions": [{"category": "web_pages", "name": "首页"}]},
        }

    report = release_gate.run_release_gate(
        output_dir=tmp_path,
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=performance_checker,
        include_installer_build=True,
    )

    assert report["ok"] is False
    assert [check["name"] for check in report["checks"]][-1] == "performance_regression"
    assert "installer_build" not in [check["name"] for check in report["checks"]]
    assert "首页 regressed" in report["checks"][-1]["stderr"]


def write_release_evidence_files(root: Path, stamp: str) -> None:
    release_dir = root / "release-checks"
    benchmarks_dir = root / "benchmarks"
    diagnostics_dir = root / "diagnostics"
    release_dir.mkdir(parents=True, exist_ok=True)
    benchmarks_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    for prefix in ("release-gate", "delivery-manifest"):
        for suffix in (".json", ".md"):
            (release_dir / f"{prefix}-{stamp}{suffix}").write_text(prefix, encoding="utf-8")
    for prefix in ("pre-release-backup", "doctor-report"):
        (release_dir / f"{prefix}-{stamp}.json").write_text(prefix, encoding="utf-8")
    for suffix in (".json", ".md"):
        (release_dir / f"delivery-evidence-check-{stamp}{suffix}").write_text("delivery-evidence", encoding="utf-8")
    for suffix in (".json", ".md"):
        (release_dir / f"preflight-summary-{stamp}{suffix}").write_text("preflight", encoding="utf-8")
    for suffix in (".json", ".md"):
        (release_dir / f"final-release-check-{stamp}{suffix}").write_text("final-release-check", encoding="utf-8")
    for suffix in (".json", ".md"):
        (benchmarks_dir / f"web-benchmark-{stamp}{suffix}").write_text("benchmark", encoding="utf-8")
    (diagnostics_dir / f"douyin-recall-diagnostics-{stamp}.zip").write_bytes(b"zip")


def test_release_evidence_retention_dry_run_reports_candidates_without_deleting(tmp_path: Path) -> None:
    from relcheck import release_gate

    stamps = ["20260707-010000", "20260707-020000", "20260707-030000"]
    for stamp in stamps:
        write_release_evidence_files(tmp_path, stamp)

    report = release_gate.describe_release_evidence_retention(
        release_checks_dir=tmp_path / "release-checks",
        benchmarks_dir=tmp_path / "benchmarks",
        diagnostics_dir=tmp_path / "diagnostics",
        keep_latest=1,
    )

    assert report["ok"] is True
    assert report["mode"] == "dry_run"
    assert report["delete_method"] == "one_file_at_a_time"
    candidate_names = {item["name"] for item in report["delete_candidates"]}
    assert "release-gate-20260707-010000.json" in candidate_names
    assert "delivery-manifest-20260707-020000.md" in candidate_names
    assert "delivery-evidence-check-20260707-010000.json" in candidate_names
    assert "preflight-summary-20260707-010000.json" in candidate_names
    assert "final-release-check-20260707-010000.json" in candidate_names
    assert "web-benchmark-20260707-010000.json" in candidate_names
    assert "douyin-recall-diagnostics-20260707-020000.zip" in candidate_names
    assert (tmp_path / "release-checks" / "release-gate-20260707-010000.json").exists()
    assert (tmp_path / "release-checks" / "delivery-evidence-check-20260707-010000.json").exists()
    assert (tmp_path / "release-checks" / "preflight-summary-20260707-010000.json").exists()
    assert (tmp_path / "release-checks" / "final-release-check-20260707-010000.json").exists()
    assert (tmp_path / "benchmarks" / "web-benchmark-20260707-010000.json").exists()
    assert (tmp_path / "diagnostics" / "douyin-recall-diagnostics-20260707-010000.zip").exists()


def test_release_evidence_retention_apply_deletes_old_reports_without_touching_databases(tmp_path: Path) -> None:
    from relcheck import release_gate

    stamps = ["20260707-010000", "20260707-020000", "20260707-030000"]
    for stamp in stamps:
        write_release_evidence_files(tmp_path, stamp)
    release_db = tmp_path / "release-checks" / "pre-release-recall-20260707-010000.db"
    baseline = tmp_path / "release-checks" / "performance-baseline.json"
    current = tmp_path / "release-checks" / "performance-current.json"
    benchmark_db = tmp_path / "benchmarks" / "query-cache.db"
    export_backup = tmp_path / "exports" / "pre-release-recall-20260707-010000.db"
    release_db.write_bytes(b"do not delete")
    baseline.write_text("baseline", encoding="utf-8")
    current.write_text("current", encoding="utf-8")
    benchmark_db.write_bytes(b"do not delete")
    export_backup.parent.mkdir()
    export_backup.write_bytes(b"backup")

    report = release_gate.enforce_release_evidence_retention(
        release_checks_dir=tmp_path / "release-checks",
        benchmarks_dir=tmp_path / "benchmarks",
        diagnostics_dir=tmp_path / "diagnostics",
        keep_latest=2,
    )

    assert report["ok"] is True
    assert report["mode"] == "apply"
    deleted_names = {item["name"] for item in report["deleted"]}
    assert "release-gate-20260707-010000.json" in deleted_names
    assert "delivery-manifest-20260707-010000.md" in deleted_names
    assert "delivery-evidence-check-20260707-010000.md" in deleted_names
    assert "preflight-summary-20260707-010000.md" in deleted_names
    assert "final-release-check-20260707-010000.md" in deleted_names
    assert "pre-release-backup-20260707-010000.json" in deleted_names
    assert "doctor-report-20260707-010000.json" in deleted_names
    assert "web-benchmark-20260707-010000.md" in deleted_names
    assert "douyin-recall-diagnostics-20260707-010000.zip" in deleted_names
    assert not (tmp_path / "release-checks" / "release-gate-20260707-010000.json").exists()
    assert (tmp_path / "release-checks" / "release-gate-20260707-020000.json").exists()
    assert (tmp_path / "release-checks" / "release-gate-20260707-030000.json").exists()
    assert release_db.exists()
    assert baseline.exists()
    assert current.exists()
    assert benchmark_db.exists()
    assert export_backup.exists()
    protected_names = {item["name"] for item in report["protected"]}
    assert release_db.name in protected_names
    assert benchmark_db.name in protected_names


def test_release_gate_runs_evidence_retention_when_enabled(tmp_path: Path) -> None:
    from relcheck import release_gate

    write_release_evidence_files(tmp_path, "20260707-010000")
    write_release_evidence_files(tmp_path, "20260707-020000")

    def runner(command, cwd, env, timeout_seconds):
        return release_gate.CommandResult(
            command=list(command),
            exit_code=0,
            elapsed_seconds=0.01,
            stdout="ok",
            stderr="",
        )

    report = release_gate.run_release_gate(
        output_dir=tmp_path / "release-checks",
        runner=runner,
        pre_release_backup_checker=passing_pre_release_backup,
        performance_checker=lambda output_dir, update_baseline=False: {
            "name": "performance_regression",
            "ok": True,
            "exit_code": 0,
            "elapsed_seconds": 0.01,
            "command": ["internal", "performance_regression"],
            "stdout": "ok",
            "stderr": "",
            "artifacts": {},
            "performance": {"regressions": []},
        },
        manifest_rollback_checker=passing_manifest_rollback,
        cleanup_release_evidence=True,
        release_evidence_keep=1,
        benchmarks_dir=tmp_path / "benchmarks",
        diagnostics_dir=tmp_path / "diagnostics",
    )

    assert report["ok"] is True
    assert "release_evidence_retention" in report
    assert report["release_evidence_retention"]["ok"] is True
    assert not (tmp_path / "release-checks" / "release-gate-20260707-010000.json").exists()
    assert not (tmp_path / "benchmarks" / "web-benchmark-20260707-010000.json").exists()
    assert not (tmp_path / "diagnostics" / "douyin-recall-diagnostics-20260707-010000.zip").exists()
    assert (tmp_path / "benchmarks" / "web-benchmark-20260707-020000.json").exists()
    assert (tmp_path / "diagnostics" / "douyin-recall-diagnostics-20260707-020000.zip").exists()
    assert Path(report["reports"]["json"]).exists()
    assert Path(report["reports"]["manifest_json"]).exists()


def test_performance_regression_check_creates_baseline_and_fails_large_slowdown(tmp_path: Path) -> None:
    from relcheck import release_gate

    benchmarks = tmp_path / "benchmarks"
    output_dir = tmp_path / "release-checks"
    benchmarks.mkdir()

    def write_reports(home_ms: float, query_ms: float) -> None:
        web = {
            "generated_at": "2026-07-07T00:00:00+00:00",
            "repeat": 3,
            "pages": [
                {"name": "首页", "path": "/", "status_code": 200, "runs": 3, "avg_ms": home_ms},
                {"name": "维护", "path": "/maintenance", "status_code": 200, "runs": 3, "avg_ms": 20.0},
            ],
        }
        (benchmarks / "web-benchmark-20260707-000000.json").write_text(
            json.dumps(web, ensure_ascii=False),
            encoding="utf-8",
        )
        query = {
            "row_count": 10000,
            "repeats": 3,
            "queries": [
                {"name": "home_list", "after_ms": query_ms},
                {"name": "search_like", "after_ms": 1.0},
            ],
        }
        (benchmarks / "query-performance-audit.json").write_text(
            json.dumps(query, ensure_ascii=False),
            encoding="utf-8",
        )

    write_reports(home_ms=100.0, query_ms=5.0)
    first = release_gate.check_performance_regression(output_dir, benchmarks_dir=benchmarks)
    assert first["ok"] is True
    assert first["performance"]["baseline_status"] == "created"
    assert (output_dir / "performance-baseline.json").exists()

    write_reports(home_ms=130.0, query_ms=5.4)
    mild = release_gate.check_performance_regression(output_dir, benchmarks_dir=benchmarks)
    assert mild["ok"] is True
    assert mild["performance"]["regressions"] == []

    write_reports(home_ms=220.0, query_ms=20.0)
    failed = release_gate.check_performance_regression(output_dir, benchmarks_dir=benchmarks)
    assert failed["ok"] is False
    names = {(item["category"], item["name"]) for item in failed["performance"]["regressions"]}
    assert ("web_pages", "首页") in names
    assert ("queries", "home_list") in names
    assert "首页" in failed["stderr"]


def test_installed_smoke_test_uses_isolated_data_and_download_roots(tmp_path: Path) -> None:
    from relcheck import installed_smoke

    app_root = tmp_path / "installed-app"
    report = installed_smoke.run_installed_smoke_test(app_root, port=18766)

    assert report["ok"] is True
    assert report["app_root"] == str(app_root)
    assert report["data_dir"] == str(app_root / "data")
    assert report["runtime_download_root"] == str(app_root / "runtime-downloads")
    assert report["checks"]["env_file"]["ok"] is True
    assert report["checks"]["status_command"]["ok"] is True
    assert report["checks"]["maintenance_endpoint"]["ok"] is True
    assert report["checks"]["backup_directory"]["ok"] is True
    assert report["checks"]["download_paths"]["ok"] is True
    assert report["checks"]["service_lifecycle"]["ok"] is True
    assert report["checks"]["service_lifecycle"]["details"]["after_stop_state"] == "stopped"
    auth_fragments = report["checks"]["auth_setup_fragments"]
    assert auth_fragments["ok"] is True
    assert auth_fragments["details"]["states"] == [
        "qr_ready",
        "scan_pending",
        "confirmed",
        "failed",
    ]
    assert auth_fragments["details"]["setup_unchanged_status_codes"] == [204, 204, 204]
    assert auth_fragments["details"]["sensitive_tokens_found"] == []
    assert auth_fragments["details"]["endpoints"] == [
        "/auth/status",
        "/setup/auth-status",
        "/setup/scan-state",
    ]
    queue_stability = report["checks"]["job_queue_stability"]
    assert queue_stability["ok"] is True
    assert queue_stability["details"]["duplicate_suppressed"] is True
    assert queue_stability["details"]["duplicate_job_count"] == 1
    assert queue_stability["details"]["retry_status"] == "pending"
    assert queue_stability["details"]["retry_attempts"] == 1
    assert queue_stability["details"]["retry_next_run_at"] is not None
    assert queue_stability["details"]["recovered_stale_running"] == 1
    assert queue_stability["details"]["stale_status_after_maintenance"] == "pending"
    assert queue_stability["details"]["maintenance_running_count"] == 0
    assert queue_stability["details"]["maintenance_retrying_count"] == 2
    assert queue_stability["details"]["terminal_failed_count"] == 1
    assert queue_stability["details"]["page_backend_inconsistent"] is False
    assert queue_stability["details"]["failed_section_retrying_count"] == 2
    sync_idempotency = report["checks"]["sync_idempotency"]
    assert sync_idempotency["ok"] is True
    assert sync_idempotency["details"]["content_kinds"] == ["favorites", "likes"]
    for content_kind in ("favorites", "likes"):
        details = sync_idempotency["details"][content_kind]
        assert details["row_count_after_repeated_sync"] == 4
        assert details["duplicate_rows_for_stable_item"] == 1
        assert details["first_sync"]["new_count"] == 1
        assert details["first_sync"]["updated_count"] == 2
        assert details["first_sync"]["removed_count"] == 1
        assert details["second_sync"]["new_count"] == 0
        assert details["second_sync"]["removed_count"] == 0
        assert details["note_preserved"] is True
        assert details["category_preserved"] is True
        assert details["action_time_preserved"] is True
        assert details["video_created_at_preserved"] is True
        assert details["missing_item_marked_removed"] is True
        assert details["returning_item_reactivated"] is True
    category_import = report["checks"]["category_import_migration"]
    assert category_import["ok"] is True
    assert category_import["details"]["discovered_sources"] == 1
    assert category_import["details"]["candidate_match_count"] == 1
    assert category_import["details"]["candidate_source_item_count"] == 2
    assert category_import["details"]["import_result"]["reason"] == "imported"
    assert category_import["details"]["import_result"]["category_count"] == 1
    assert category_import["details"]["import_result"]["assigned_item_count"] == 1
    assert category_import["details"]["imported_category_name"] == "旧库分类"
    assert category_import["details"]["matched_item_category"] == "旧库分类"
    assert category_import["details"]["unmatched_item_imported"] is False
    assert category_import["details"]["existing_guard"]["reason"] == "current_has_categories"
    assert category_import["details"]["existing_guard"]["category_count_after"] == 1
    assert category_import["details"]["existing_guard"]["existing_category_preserved"] is True
    assert category_import["details"]["existing_guard"]["existing_item_unassigned"] is True
    account_boundaries = report["checks"]["account_boundaries"]
    assert account_boundaries["ok"] is True
    assert account_boundaries["details"]["add_account_created_new_user"] is True
    assert account_boundaries["details"]["add_account_started_qr_for_new_user"] is True
    assert account_boundaries["details"]["switch_account_changed_current_session"] is True
    assert account_boundaries["details"]["other_session_unchanged_after_switch"] is True
    assert account_boundaries["details"]["switch_rejects_unbound_account"] is True
    assert account_boundaries["details"]["logout_cleared_current_profile_only"] is True
    assert account_boundaries["details"]["logout_preserved_local_content"] is True
    assert account_boundaries["details"]["rebind_updated_current_user_only"] is True
    assert account_boundaries["details"]["multi_account_data_isolated"] is True
    maintenance_status = report["checks"]["maintenance_status"]
    assert maintenance_status["ok"] is True
    assert maintenance_status["details"]["schema_version"] == 1
    assert maintenance_status["details"]["section_keys"] == [
        "actions",
        "backup",
        "failed_tasks",
        "index",
        "login",
        "service",
    ]
    assert maintenance_status["details"]["capabilities_schema_version"] == 1
    assert maintenance_status["details"]["capability_keys"] == [
        "backup_status",
        "failed_tasks",
        "index_status",
        "login_status",
        "service_status",
        "suggested_actions",
    ]
    rollback_check = report["checks"]["rollback_check"]
    assert rollback_check["ok"] is True
    assert rollback_check["details"]["no_manifest"]["ok"] is True
    assert "delivery-manifest" in rollback_check["details"]["no_manifest"]["message"]
    assert rollback_check["details"]["dry_run"]["ok"] is True
    assert rollback_check["details"]["dry_run"]["restored"] is False
    assert "--apply" not in rollback_check["details"]["command"]
    assert rollback_check["details"]["control_script_exists"] is True
    assert rollback_check["details"]["control_script_has_apply"] is False
    assert (app_root / ".env").exists()
    assert not (Path.cwd() / "data" / "installed-smoke-marker.txt").exists()


def test_installed_smoke_rollback_check_handles_relative_app_root(tmp_path: Path, monkeypatch) -> None:
    from relcheck import installed_smoke

    monkeypatch.chdir(tmp_path)

    report = installed_smoke.run_installed_smoke_test(Path("installed-app"), port=18767)

    assert report["ok"] is True
    rollback_check = report["checks"]["rollback_check"]
    assert rollback_check["ok"] is True
    assert rollback_check["details"]["dry_run"]["ok"] is True
    assert rollback_check["details"]["dry_run"]["restored"] is False
    assert not rollback_check["details"]["dry_run"]["errors"]


def test_installed_smoke_category_import_migration_can_rerun_same_app_root(tmp_path: Path) -> None:
    from relcheck import installed_smoke

    app_root = tmp_path / "installed-app"

    first = installed_smoke.run_installed_smoke_test(app_root, port=18768)
    second = installed_smoke.run_installed_smoke_test(app_root, port=18769)

    for report in (first, second):
        category_import = report["checks"]["category_import_migration"]
        assert report["ok"] is True
        assert category_import["ok"] is True
        assert category_import["details"]["discovered_sources"] == 1
        assert category_import["details"]["import_result"]["reason"] == "imported"
        assert category_import["details"]["existing_guard"]["reason"] == "current_has_categories"


def test_release_checklist_documents_script_reports_and_manual_gates() -> None:
    checklist = Path("docs/release-checklist.md")

    text = checklist.read_text(encoding="utf-8")

    assert "scripts\\release_gate.ps1" in text
    assert "data\\release-checks" in text
    assert "DouyinRecallSetup.exe" in text
    assert "升级前备份" in text
    assert "首次启动" in text
    assert "诊断包" in text
    assert "delivery-manifest" in text
    assert "pre_release_backup" in text
    assert "pre-release-recall" in text
    assert "manifest_rollback_dry_run" in text
    assert "rollback-from-manifest" in text
    assert "rollback-check" in text
    assert "Rollback Check" in text
    assert "证据保留" in text
    assert "--keep-release-evidence" in text
    assert "--skip-evidence-cleanup" in text
