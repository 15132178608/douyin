"""
解析器单测：用脱敏后的 listcollection 响应作 fixture。
跑法：python -m pytest tests/ -v
或者直接 python tests/test_parser.py
"""
from __future__ import annotations

import json
from pathlib import Path

from src.crawler.parser import extract_aweme, extract_response

FIXTURE = Path(__file__).parent / "fixtures" / "listcollection_sample.json"


def load_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_response_meta() -> None:
    payload = load_payload()
    favorites, meta = extract_response(payload)
    assert meta["status_code"] == 0
    assert meta["has_more"] in (0, 1)
    assert isinstance(meta["cursor"], int)
    assert meta["count"] == len(favorites)
    assert len(favorites) > 0, "样本里应该至少有一条收藏"


def test_response_meta_accepts_likes_max_cursor() -> None:
    payload = {
        "status_code": 0,
        "has_more": 1,
        "max_cursor": 123,
        "aweme_list": [{"aweme_id": "liked-1", "desc": "liked"}],
    }

    favorites, meta = extract_response(payload)

    assert len(favorites) == 1
    assert meta["cursor"] == 123


def test_first_item_fields() -> None:
    payload = load_payload()
    item = payload["aweme_list"][0]
    fav = extract_aweme(item)
    assert fav is not None
    # 已知 fixture 里第 0 条的真值（脱敏样本）
    assert fav.id == "sample-aweme-1"
    assert fav.author == "示例作者1"
    assert fav.author_id and fav.author_id.startswith("MS4wLj")
    assert fav.title and "NotebookLM" in fav.title
    assert fav.video_url and "iesdouyin.com/share/video/" in fav.video_url
    assert fav.cover_url and fav.cover_url.startswith("https://")
    assert fav.duration_ms == 382548
    # 平台标签
    assert fav.video_tags is not None
    tags = json.loads(fav.video_tags)
    assert any(t.get("tag_name") == "个人管理" for t in tags)
    # raw_json 完整保留
    assert fav.raw_json is not None
    assert "aweme_id" in fav.raw_json
    # 时间字段由 sync 层填，解析器不管
    assert fav.favorited_at is None
    assert fav.first_seen_at is None


def test_all_items_parseable() -> None:
    payload = load_payload()
    favorites, _ = extract_response(payload)
    # 每条都有 id + 至少一个标识性字段
    for fav in favorites:
        assert fav.id, f"missing id: {fav}"
        assert fav.title or fav.author, f"item {fav.id} has neither title nor author"


def test_missing_aweme_id_returns_none() -> None:
    """没有 aweme_id 的 item 返回 None，不抛异常。"""
    assert extract_aweme({}) is None
    assert extract_aweme({"desc": "xxx"}) is None


def test_unknown_fields_dont_break() -> None:
    """抖音加新字段不应该让我们崩。"""
    item = {
        "aweme_id": "fake_id",
        "desc": "test",
        "some_future_field": {"nested": [1, 2, 3]},
    }
    fav = extract_aweme(item)
    assert fav is not None
    assert fav.id == "fake_id"
    assert fav.title == "test"


def test_folder_names_are_added_to_video_tags() -> None:
    """收藏夹 folder 名称应进入 tags，给搜索/分类提供强信号。"""
    fav = extract_aweme(
        {
            "aweme_id": "foldered",
            "desc": "folder item",
            "video_tag": [{"tag_name": "平台标签"}],
            "collect_folder_list": [{"name": "健身收藏"}, {"folder_name": "菜谱"}],
        }
    )

    assert fav is not None
    assert fav.video_tags is not None
    assert "平台标签" in fav.video_tags
    assert "健身收藏" in fav.video_tags
    assert "菜谱" in fav.video_tags


if __name__ == "__main__":
    # 不依赖 pytest 也能跑（沙箱里 pytest 没装也无所谓）
    tests = [
        test_response_meta,
        test_response_meta_accepts_likes_max_cursor,
        test_first_item_fields,
        test_all_items_parseable,
        test_missing_aweme_id_returns_none,
        test_unknown_fields_dont_break,
        test_folder_names_are_added_to_video_tags,
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
