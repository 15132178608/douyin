"""
Author avatar helper tests.

Run:
    python tests/test_author_avatars.py
"""
from __future__ import annotations

import json

from src.web.authors import author_avatar_url_from_raw_json, cached_author_avatar_url_from_raw_json


def test_author_avatar_url_prefers_thumb_url() -> None:
    raw_json = json.dumps(
        {
            "author": {
                "avatar_thumb": {
                    "url_list": ["https://example.com/thumb.jpeg"],
                },
                "avatar_medium": {
                    "url_list": ["https://example.com/medium.jpeg"],
                },
            },
        }
    )

    assert author_avatar_url_from_raw_json(raw_json) == "https://example.com/thumb.jpeg"


def test_author_avatar_url_falls_back_to_medium_and_ignores_invalid_json() -> None:
    raw_json = json.dumps(
        {
            "author": {
                "avatar_thumb": {"url_list": []},
                "avatar_medium": {"url_list": ["https://example.com/medium.jpeg"]},
            },
        }
    )

    assert author_avatar_url_from_raw_json(raw_json) == "https://example.com/medium.jpeg"
    assert author_avatar_url_from_raw_json("{bad json") is None
    assert author_avatar_url_from_raw_json(None) is None


def test_cached_author_avatar_url_wraps_remote_url_in_local_proxy() -> None:
    raw_json = json.dumps(
        {
            "author": {
                "avatar_thumb": {
                    "url_list": ["https://cdn.example.com/a.jpeg"],
                },
            },
        }
    )

    cached = cached_author_avatar_url_from_raw_json(raw_json)

    assert cached is not None
    assert cached.startswith("/avatar-cache?u=")
    assert "cdn.example.com" in cached


if __name__ == "__main__":
    tests = [
        test_author_avatar_url_prefers_thumb_url,
        test_author_avatar_url_falls_back_to_medium_and_ignores_invalid_json,
        test_cached_author_avatar_url_wraps_remote_url_in_local_proxy,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
