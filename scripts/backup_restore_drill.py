from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from relcheck.backup_drill import run_backup_restore_drill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the backup and restore drill.")
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "data" / "audits" / "backup-restore-drill"),
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    report = run_backup_restore_drill(output_dir)
    report_path = output_dir / "backup-restore-drill.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Backup restore drill: {report_path}")
    if not report["ok"]:
        for message in report["failure_messages"]:
            print(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
