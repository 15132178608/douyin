"""Web response-time benchmark helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import time
from typing import Any


DEFAULT_PAGES = [
    {"name": "首页", "path": "/"},
    {"name": "收藏", "path": "/?p=1&page_size=32"},
    {"name": "喜欢", "path": "/likes"},
    {"name": "分类", "path": "/categories"},
    {"name": "维护", "path": "/maintenance"},
    {"name": "账号页", "path": "/auth"},
]


def _default_client():
    from fastapi.testclient import TestClient
    from src.db import init_schema
    from src.web import app as web_app

    # TestClient used without a context manager does not run FastAPI lifespan.
    # Explicit initialization keeps the release benchmark on the same upgraded
    # schema that the installed launcher prepares before serving requests.
    init_schema()
    return TestClient(web_app.app)


def run_page_benchmarks(
    *,
    client: Any | None = None,
    pages: list[dict[str, str]] | None = None,
    repeat: int = 3,
    disable_network_update: bool = True,
) -> dict:
    """Measure configured web pages and return a JSON-serializable report."""
    active_client = client or _default_client()
    page_specs = pages or DEFAULT_PAGES
    safe_repeat = max(1, int(repeat or 1))
    measurements: dict[str, list[dict[str, float | int]]] = {
        page["name"]: [] for page in page_specs
    }

    original_update_getter = None
    if disable_network_update:
        from src import update_check

        original_update_getter = update_check.get_cached_update_status
        update_check.get_cached_update_status = lambda *args, **kwargs: {
            "local_version": "benchmark",
            "latest_version": "benchmark",
            "update_available": False,
            "release_url": None,
            "asset_name": "DouyinRecallSetup.exe",
            "asset_url": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }

    try:
        for _ in range(safe_repeat):
            for page in page_specs:
                started = time.perf_counter()
                response = active_client.get(page["path"])
                elapsed_ms = (time.perf_counter() - started) * 1000
                content = getattr(response, "content", b"")
                if isinstance(content, str):
                    body_bytes = len(content.encode("utf-8"))
                else:
                    body_bytes = len(content or b"")
                measurements[page["name"]].append(
                    {
                        "elapsed_ms": elapsed_ms,
                        "status_code": int(getattr(response, "status_code", 0)),
                        "body_bytes": body_bytes,
                    }
                )
    finally:
        if original_update_getter is not None:
            from src import update_check

            update_check.get_cached_update_status = original_update_getter

    results: list[dict] = []
    for page in page_specs:
        samples = measurements[page["name"]]
        elapsed = [float(sample["elapsed_ms"]) for sample in samples]
        results.append(
            {
                "name": page["name"],
                "path": page["path"],
                "status_code": int(samples[-1]["status_code"]),
                "runs": len(samples),
                "avg_ms": sum(elapsed) / len(elapsed),
                "min_ms": min(elapsed),
                "max_ms": max(elapsed),
                "body_bytes": int(samples[-1]["body_bytes"]),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repeat": safe_repeat,
        "pages": results,
    }


def write_benchmark_report(report: dict, output_dir: Path | str) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    markdown_path = output_path / f"web-benchmark-{stamp}.md"
    json_path = output_path / f"web-benchmark-{stamp}.json"

    lines = [
        "# Douyin Recall Web Benchmark",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- repeat: `{report['repeat']}`",
        "",
        "| page | path | status | runs | avg_ms | min_ms | max_ms | body_bytes |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for page in report["pages"]:
        lines.append(
            "| {name} | `{path}` | {status_code} | {runs} | {avg_ms:.2f} | "
            "{min_ms:.2f} | {max_ms:.2f} | {body_bytes} |".format(**page)
        )
    lines.extend(
        [
            "",
            "Use the JSON file with the same timestamp as a machine-readable baseline for later comparisons.",
        ]
    )

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return markdown_path
