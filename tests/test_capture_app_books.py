"""scripts/capture_app_books.py のテスト（Kindle アプリで本を開いて撮る。#74）

画面を触る部分は実機でしか確かめられないので、ここでは「どの本を撮るか」と
「開いた本が合っているか」の判定だけを見る。**別の本を蔵書に入れない**のが要。
"""

import csv
import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import capture_app_books as cab  # noqa: E402

from core.book_format import book_pdf_path  # noqa: E402


def _library(folder, titles):
    os.makedirs(folder, exist_ok=True)
    for title in titles:
        with open(book_pdf_path(str(folder), title), "wb") as f:
            f.write(b"%PDF-1.4\n")  # 中身は見ない（撮影そのものは差し替える）
    return str(folder)


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_title_matches_ignores_marks_and_case():
    assert cab.title_matches(
        "学びを結果に変えるアウトプット大全", "学びを結果に変えるアウトプット大全"
    )
    assert cab.title_matches("Linear Algebra Done Right", "linear algebra done right")


def test_title_matches_refuses_a_title_read_only_in_part():
    """題名の一部しか読めないときは撮らない（安全側）。

    照合は**先頭に戻して表紙を読んでから**行う。表紙には題名が丸ごと入っているので、
    一部しか読めないのは OCR が失敗しているか、別の本を開いている。
    """
    assert not cab.title_matches(
        "しっかり学ぶ数理最適化　モデルからアルゴリズムまで (ＫＳ情報科学専門書)",
        "しっかり学ぶ数理最適化",
    )
    assert cab.title_matches(
        "しっかり学ぶ数理最適化　モデルからアルゴリズムまで (ＫＳ情報科学専門書)",
        "ＫＳ情報科学専門書 しっかり学ぶ数理最適化 モデルからアルゴリズムまで 梅谷俊治 講談社",
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


def test_title_matches_rejects_a_book_that_only_starts_the_same():
    """先頭が同じだけの別の本を同じ本と見ない。

    対象は英語の数学書・技術書が多く、先頭が同じ題名が普通にある。
    先頭 8 文字で見ていたときは、この 2 組がどちらも同じ本と判定されていた。
    """
    assert not cab.title_matches(
        "Linear Algebra Done Right (Undergraduate Texts in Mathematics)",
        "Linear Algebra and Its Applications Fifth Edition",
    )
    assert not cab.title_matches(
        "Introduction to Smooth Manifolds", "Introduction to Algebraic Geometry"
    )


def test_title_matches_survives_the_noise_around_a_cover():
    """表紙のページ全体を読むので、著者名・出版社名・帯の文句が混ざる。"""
    assert cab.title_matches(
        "Linear Algebra Done Right (Undergraduate Texts in Mathematics)",
        "Undergraduate Texts in Mathematics UTM Sheldon Axler Linear Algebra Done Right "
        "Fourth Edition OPEN ACCESS Springer",
    )


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


def _prepare(tmp_path, entries, titles):
    books_path = tmp_path / "books.json"
    with open(books_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    library = _library(tmp_path / "lib", titles)
    return str(books_path), library, str(tmp_path / "state.csv")


def _stub_screen(monkeypatch, *, opened=True, rewound=True, verified=True):
    monkeypatch.setattr(cab, "_app", lambda: object())
    monkeypatch.setattr(cab, "_place", lambda hwnd: None)
    monkeypatch.setattr(cab, "open_book", lambda hwnd, title, **kw: opened)
    monkeypatch.setattr(cab, "rewind_to_start", lambda hwnd, **kw: rewound)
    monkeypatch.setattr(cab, "verify_title", lambda hwnd, title, **kw: verified)


def test_counts_every_status_and_fails_when_a_book_is_not_captured(tmp_path, monkeypatch, capsys):
    """撮れなかった本があるのに 0 を返すと、無人実行で異常に気づけない。"""
    books, library, state = _prepare(tmp_path, [{"title": "本A", "asin": "A1"}], [])
    _stub_screen(monkeypatch, opened=False)
    code = cab.main(["--books", books, "--library", library, "--state", state])
    assert code == 1
    assert _rows(state)[0]["status"] == "開けない"
    assert "開けない" in capsys.readouterr().out


def test_does_not_finish_a_book_with_too_few_pages(tmp_path, monkeypatch):
    """撮れていない本を完成扱いにしない（次からずっと飛ばされる）。"""
    books, library, state = _prepare(tmp_path, [{"title": "本B", "asin": "A2"}], [])
    _stub_screen(monkeypatch)

    def fake_run_book(**kw):
        kw["emit"]("result", total_pages=3, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    code = cab.main(["--books", books, "--library", library, "--state", state])
    row = _rows(state)[0]
    assert row["status"] == "ページが少なすぎる" and row["pages"] == "3"
    assert code == 1


def test_records_a_finished_book_with_its_stopped_reason(tmp_path, monkeypatch):
    books, library, state = _prepare(tmp_path, [{"title": "本C", "asin": "A3"}], [])
    _stub_screen(monkeypatch)

    def fake_run_book(**kw):
        assert kw["page_turn"] == "right"  # アプリは縦書きでも → が次ページ
        assert kw["overwrite"] is True  # 失敗した本をやり直せるように
        assert kw["asin"] == "A3"  # 表紙を商品ページから取る
        kw["emit"]("result", total_pages=120, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 0
    row = _rows(state)[0]
    assert (row["status"], row["pages"], row["stopped_reason"]) == (cab.DONE, "120", "no_change")
    assert cab.load_state(state) == {"A3"}


def test_refuses_a_partial_capture_into_the_library(tmp_path, monkeypatch):
    """--max-pages の出力を蔵書に混ぜない（部分本が完成扱いで固定される）。"""
    books, library, state = _prepare(tmp_path, [{"title": "本D", "asin": "A4"}], [])
    with pytest.raises(SystemExit):
        cab.main(["--books", books, "--library", library, "--state", state, "--max-pages", "8"])
