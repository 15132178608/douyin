from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from relcheck.performance_benchmark import run_page_benchmarks, write_benchmark_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the release Web pages.")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "benchmarks"))
    args = parser.parse_args(argv)

    report = run_page_benchmarks()
    path = write_benchmark_report(report, Path(args.output_dir))
    print(f"Benchmark report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
