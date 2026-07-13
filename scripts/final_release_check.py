from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import PROJECT_ROOT
from relcheck.final_release_check import run_final_release_check, write_final_release_check_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the final Douyin Recall release verification chain.")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "release-checks"))
    parser.add_argument("--benchmarks-dir", default=str(PROJECT_ROOT / "data" / "benchmarks"))
    parser.add_argument("--audits-dir", default=str(PROJECT_ROOT / "data" / "audits"))
    installer_mode = parser.add_mutually_exclusive_group()
    installer_mode.add_argument("--build-installer", action="store_true")
    installer_mode.add_argument("--installer-path", default=None)
    parser.add_argument("--update-performance-baseline", action="store_true")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    report = run_final_release_check(
        output_dir=output_dir,
        benchmarks_dir=Path(args.benchmarks_dir),
        audits_dir=Path(args.audits_dir),
        build_installer=bool(args.build_installer),
        installer_path=Path(args.installer_path) if args.installer_path else None,
        update_performance_baseline=bool(args.update_performance_baseline),
    )
    paths = write_final_release_check_report(report, output_dir)
    print(f"Final release check report: {paths['markdown']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
