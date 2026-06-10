"""
Content kind registry tests.

Run:
    python tests/test_content_kinds.py
"""
from __future__ import annotations

from src.content.kinds import DEFAULT_CONTENT_KIND, get_content_kind, list_content_kinds


def test_content_kinds_define_favorites_and_likes_modules() -> None:
    favorites = get_content_kind("favorites")
    likes = get_content_kind("likes")

    assert DEFAULT_CONTENT_KIND == "favorites"
    assert [kind.key for kind in list_content_kinds()] == ["favorites", "likes"]
    assert favorites.label == "收藏"
    assert favorites.table == "favorites"
    assert favorites.vector_table == "favorites_vec"
    assert favorites.category_table == "categories"
    assert favorites.time_column == "favorited_at"
    assert likes.label == "喜欢"
    assert likes.table == "likes"
    assert likes.vector_table == "likes_vec"
    assert likes.category_table == "like_categories"
    assert likes.time_column == "liked_at"


def test_unknown_content_kind_falls_back_to_favorites() -> None:
    assert get_content_kind(None).key == "favorites"
    assert get_content_kind("").key == "favorites"
    assert get_content_kind("bad").key == "favorites"


if __name__ == "__main__":
    tests = [
        test_content_kinds_define_favorites_and_likes_modules,
        test_unknown_content_kind_falls_back_to_favorites,
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
