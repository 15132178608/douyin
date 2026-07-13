from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import PROJECT_ROOT
from relcheck.preflight_summary import build_preflight_summary, write_preflight_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a read-only release preflight summary.")
    parser.add_argument("--release-checks-dir", default=str(PROJECT_ROOT / "data" / "release-checks"))
    parser.add_argument("--benchmarks-dir", default=str(PROJECT_ROOT / "data" / "benchmarks"))
    parser.add_argument("--audits-dir", default=str(PROJECT_ROOT / "data" / "audits"))
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)

    release_checks_dir = Path(args.release_checks_dir)
    report = build_preflight_summary(
        release_checks_dir=release_checks_dir,
        benchmarks_dir=Path(args.benchmarks_dir),
        audits_dir=Path(args.audits_dir),
    )
    paths = write_preflight_summary(report, Path(args.output_dir) if args.output_dir else release_checks_dir)
    print(f"Preflight summary report: {paths['markdown']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
