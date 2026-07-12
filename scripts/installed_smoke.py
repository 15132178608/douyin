from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.config import PROJECT_ROOT
from relcheck.installed_smoke import run_installed_smoke_test, write_installed_smoke_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run installed-layout smoke checks.")
    parser.add_argument("--app-root", default=str(PROJECT_ROOT / "data" / "release-checks" / "installed-smoke"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "release-checks"))
    parser.add_argument("--port", default=18765, type=int)
    args = parser.parse_args()

    report = run_installed_smoke_test(Path(args.app_root), port=args.port)
    path = write_installed_smoke_report(report, Path(args.output_dir))
    print(json.dumps({"ok": report["ok"], "report": str(path)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
