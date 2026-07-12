from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from relcheck.performance_benchmark import run_page_benchmarks, write_benchmark_report


def main() -> int:
    report = run_page_benchmarks()
    path = write_benchmark_report(report, PROJECT_ROOT / "data" / "benchmarks")
    print(f"Benchmark report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
