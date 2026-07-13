from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from relcheck.query_performance import (
    run_query_performance_audit,
    write_query_performance_json,
    write_query_performance_report,
)


def main() -> int:
    output_dir = PROJECT_ROOT / "data" / "benchmarks"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = run_query_performance_audit()
    report_path = output_dir / "query-performance-audit.md"
    json_path = output_dir / "query-performance-audit.json"
    write_query_performance_report(report, report_path)
    write_query_performance_json(report, json_path)
    print(f"Query performance audit: {report_path}")
    print(f"Query performance JSON: {json_path}")
    for item in report["queries"]:
        print(f"{item['name']}: before={item['before_ms']:.2f}ms after={item['after_ms']:.2f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
