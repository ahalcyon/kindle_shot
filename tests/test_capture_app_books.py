"""scripts/capture_app_books.py のテスト（Kindle アプリで本を開いて撮る。#74）

画面を触る部分は実機でしか確かめられないので、ここでは「どの本を撮るか」と
「開いた本が合っているか」の判定だけを見る。**別の本を蔵書に入れない**のが要。
"""

import csv
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import capture_app_books as cab  # noqa: E402

from core.book_format import book_pdf_path  # noqa: E402


def test_title_matches_ignores_marks_and_case():
    assert cab.title_matches(
        "学びを結果に変えるアウトプット大全", "学びを結果に変えるアウトプット大全"
    )
    assert cab.title_matches("Linear Algebra Done Right", "linear algebra done right")


def test_title_matches_accepts_a_header_cut_short():
    """書名ヘッダーは途中で切れる。先頭が一致すれば同じ本とみなす。"""
    assert cab.title_matches(
        "しっかり学ぶ数理最適化　モデルからアルゴリズムまで (ＫＳ情報科学専門書)",
        "しっかり学ぶ数理最適化",
    )


def test_title_matches_finds_the_title_in_a_cover_page():
    """表紙には書名ヘッダーが出ないので、ページ全体の OCR から探す（実測）。"""
    cover = "Undergraduate Texts in Mathematics UTM Emily Clader Dustin Ross Beginning in Algebraic Geometry OPEN ACCESS Springer"
    assert cab.title_matches(
        "Beginning in Algebraic Geometry (Undergraduate Texts in Mathematics)", cover
    )


def test_title_matches_rejects_a_different_book():
    """**別の本を開いたまま撮らない。** 検索が違う本を出すことがある。"""
    assert not cab.title_matches("ベイズ深層学習", "強化学習")
    assert not cab.title_matches("入門　現代の量子力学", "集合・位相・圏")
    assert not cab.title_matches("ベイズ深層学習", "")


def test_is_cover_tells_a_book_from_the_background():
    """検索結果がちょうど 1 冊かを、この判定で見る（別の本を撮らないため）。"""
    from PIL import Image

    plain = Image.new("RGB", (40, 40), (250, 250, 250))
    assert not cab.is_cover(plain)

    cover = Image.new("RGB", (40, 40), (250, 250, 250))
    for x in range(40):
        for y in range(20):
            cover.putpixel((x, y), (10, 40, 180))  # 表紙は色がばらつく
    assert cab.is_cover(cover)


def test_pending_skips_books_that_are_already_there(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    books = [
        {"title": "撮った本", "asin": "A1"},
        {"title": "まだの本", "asin": "A2"},
        {"title": "済みの本", "asin": "A3"},
    ]
    with open(book_pdf_path(str(library), "撮った本"), "wb") as f:
        f.write(b"%PDF")
    got = cab.pending(books, {"A3"}, str(library))
    assert [b["title"] for b in got] == ["まだの本"]


def test_load_state_retries_a_failed_book(tmp_path):
    """失敗した本は次にもう一度試す（完了だけを済みにする）。"""
    path = tmp_path / "state.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cab.COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({"asin": "A1", "title": "済", "status": "完了"})
        writer.writerow({"asin": "A2", "title": "落ちた", "status": "失敗"})
        writer.writerow({"asin": "A3", "title": "開けない", "status": "開けない"})
    assert cab.load_state(str(path)) == {"A1"}


def test_dry_run_lists_without_touching_the_screen(tmp_path, capsys):
    books = [{"title": "まだの本", "asin": "A2", "format": "searchable_pdf"}]
    books_path = tmp_path / "books.json"
    with open(books_path, "w", encoding="utf-8") as f:
        json.dump(books, f, ensure_ascii=False)
    library = tmp_path / "lib"
    library.mkdir()
    code = cab.main(
        [
            "--books",
            str(books_path),
            "--library",
            str(library),
            "--state",
            str(tmp_path / "s.csv"),
            "--dry-run",
        ]
    )
    assert code == 0
    assert "まだの本" in capsys.readouterr().out
