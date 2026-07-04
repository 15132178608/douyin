"""
Update check tests.

Run:
    python tests/test_update_check.py
"""
from __future__ import annotations

from click.testing import CliRunner

from src.cli import cli


def test_update_status_detects_new_release_and_installer_asset() -> None:
    from src import update_check

    payload = {
        "tag_name": "v0.1.7",
        "html_url": "https://github.com/15132178608/douyin/releases/tag/v0.1.7",
        "assets": [
            {
                "name": "DouyinRecallSetup.exe",
                "browser_download_url": "https://example.test/DouyinRecallSetup.exe",
            }
        ],
    }

    status = update_check.get_update_status(
        local_version="0.1.6",
        fetcher=lambda _url, _timeout: payload,
    )

    assert status["local_version"] == "0.1.6"
    assert status["latest_version"] == "0.1.7"
    assert status["update_available"] is True
    assert status["release_url"].endswith("/v0.1.7")
    assert status["asset_name"] == "DouyinRecallSetup.exe"
    assert status["asset_url"] == "https://example.test/DouyinRecallSetup.exe"
    assert status["error"] is None


def test_update_status_handles_network_errors_without_crashing() -> None:
    from src import update_check

    def fail(_url: str, _timeout: float):
        raise TimeoutError("network timeout")

    status = update_check.get_update_status(local_version="0.1.6", fetcher=fail)

    assert status["local_version"] == "0.1.6"
    assert status["latest_version"] is None
    assert status["update_available"] is False
    assert "network timeout" in status["error"]


def test_cached_update_status_reuses_fetcher_within_ttl() -> None:
    from src import update_check

    calls: list[str] = []
    payload = {
        "tag_name": "v0.1.6",
        "html_url": "https://github.com/15132178608/douyin/releases/tag/v0.1.6",
        "assets": [],
    }

    def fetch(url: str, _timeout: float):
        calls.append(url)
        return payload

    update_check.clear_update_cache()
    first = update_check.get_cached_update_status(
        local_version="0.1.6",
        fetcher=fetch,
        ttl_seconds=3600,
    )
    second = update_check.get_cached_update_status(
        local_version="0.1.6",
        fetcher=fetch,
        ttl_seconds=3600,
    )

    assert first == second
    assert len(calls) == 1


def test_cli_update_command_reports_latest_release_without_installing() -> None:
    from src import update_check

    original = update_check.get_cached_update_status
    update_check.get_cached_update_status = lambda **_kwargs: {
        "local_version": "0.1.6",
        "latest_version": "0.1.7",
        "update_available": True,
        "release_url": "https://github.com/15132178608/douyin/releases/tag/v0.1.7",
        "asset_name": "DouyinRecallSetup.exe",
        "asset_url": "https://example.test/DouyinRecallSetup.exe",
        "checked_at": "2026-07-05T00:00:00+00:00",
        "error": None,
    }
    try:
        result = CliRunner().invoke(cli, ["update"])
    finally:
        update_check.get_cached_update_status = original

    assert result.exit_code == 0
    assert "当前版本: 0.1.6" in result.output
    assert "最新版本: 0.1.7" in result.output
    assert "DouyinRecallSetup.exe" in result.output
    assert "不会自动安装" in result.output


def test_cli_update_no_network_only_prints_local_version() -> None:
    result = CliRunner().invoke(cli, ["update", "--no-network"])

    assert result.exit_code == 0
    assert "当前版本:" in result.output
    assert "未联网检查最新版本" in result.output


if __name__ == "__main__":
    tests = [
        test_update_status_detects_new_release_and_installer_asset,
        test_update_status_handles_network_errors_without_crashing,
        test_cached_update_status_reuses_fetcher_within_ttl,
        test_cli_update_command_reports_latest_release_without_installing,
        test_cli_update_no_network_only_prints_local_version,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as e:
            print(f"FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
