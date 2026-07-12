from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from relcheck.database_safety import run_database_safety_audit


def main() -> int:
    output_dir = PROJECT_ROOT / "data" / "audits"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = run_database_safety_audit(output_dir / "database-safety-work")
    report_path = output_dir / "database-safety-audit.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Database safety audit: {report_path}")
    if not report["ok"]:
        for check in report["checks"]:
            for mismatch in check["mismatches"]:
                print(
                    f"{check['name']}: {mismatch['table']} "
                    f"{mismatch['key']} {mismatch['field']} mismatch"
                )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
