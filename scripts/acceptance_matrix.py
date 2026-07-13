from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relcheck.acceptance_matrix import build_acceptance_matrix, write_acceptance_matrix_reports
from src.config import PROJECT_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the Douyin Recall acceptance coverage matrix.")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "release-checks"))
    args = parser.parse_args(argv)

    report = build_acceptance_matrix()
    paths = write_acceptance_matrix_reports(report, Path(args.output_dir))
    print(f"Acceptance matrix report: {paths['markdown']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
