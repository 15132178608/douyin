from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import PROJECT_ROOT
from relcheck.delivery_evidence import (
    find_latest_delivery_manifest,
    validate_delivery_manifest_evidence,
    write_delivery_evidence_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate delivery manifest evidence files and statuses.")
    parser.add_argument("--manifest", default=None, help="delivery-manifest-*.json to validate")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "release-checks"))
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    manifest_path = Path(args.manifest) if args.manifest else find_latest_delivery_manifest(output_dir)
    if manifest_path is None:
        print(f"No delivery-manifest-*.json found in {output_dir}", file=sys.stderr)
        return 1

    report = validate_delivery_manifest_evidence(manifest_path)
    paths = write_delivery_evidence_report(report, output_dir)
    print(f"Delivery evidence report: {paths['markdown']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
