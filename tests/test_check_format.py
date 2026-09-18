"""scripts/check_format.py のテスト（蔵書を一括で測る。#96）

OCR は走らせない（`measure_pdf` を差し替える）。見たいのは一括処理の作りのほう:
測れた本だけを済みにすること、1 冊の失敗で止まらないこと、蔵書を書き換えないこと。
"""

import csv
import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import check_format  # noqa: E402

from core.book_format import book_pdf_path  # noqa: E402


def _books(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    return str(path)


def _library(folder, titles):
    os.makedirs(folder, exist_ok=True)
    for title in titles:
        with open(book_pdf_path(str(folder), title), "wb") as f:
            f.write(b"%PDF-1.4\n")  # 中身は見ない（measure_pdf を差し替えるため）
    return str(folder)


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _run(tmp_path, entries, titles, *, measure, extra=()):
    books = _books(tmp_path / "books.json", entries)
    library = _library(tmp_path / "lib", titles)
    report = str(tmp_path / "out.csv")
    check_format.measure_pdf = measure
    code = check_format.main(["--books", books, "--library", library, "--report", report, *extra])
    return code, report


def _ok(**kwargs):
    return lambda path, **kw: {"pdf_pages": 100, "sampled": 20, "chars": 200, "cpp": 10.0, **kwargs}


def test_flags_a_book_that_does_not_match_its_format(tmp_path):
    entries = [{"title": "まんが", "asin": "A1", "format": "image_pdf"}]
    code, report = _run(
        tmp_path,
        entries,
        ["まんが"],
        measure=lambda path, **kw: {"pdf_pages": 9, "sampled": 5, "chars": 5000, "cpp": 1000.0},
    )
    row = _rows(report)[0]
    assert row["verdict"].startswith("image_pdf だが文字が多い")
    assert row["cpp"] == "1000.0" and row["dpi"] and row["threshold"]
    assert code == 0


def test_measures_again_a_book_that_failed_before(tmp_path):
    """失敗した本・PDF がまだ無い本は、次に流したときもう一度測る (#126 のレビュー)。

    済みにしてしまうと、PDF が後から出来ても一過性の失敗でも二度と測られない。
    しかも 1 行も出ないので気づけない。
    """
    entries = [{"title": "あとから来る本", "asin": "A1", "format": "image_pdf"}]
    books = _books(tmp_path / "books.json", entries)
    library = str(tmp_path / "lib")
    os.makedirs(library, exist_ok=True)
    report = str(tmp_path / "out.csv")

    check_format.measure_pdf = _ok()
    assert check_format.main(["--books", books, "--library", library, "--report", report]) == 0
    assert _rows(report)[0]["verdict"] == "PDF なし"

    _library(tmp_path / "lib", ["あとから来る本"])  # あとから撮れた
    assert check_format.main(["--books", books, "--library", library, "--report", report]) == 0
    assert [r["verdict"] for r in _rows(report)] == ["PDF なし", "ok"]


def test_does_not_skip_a_different_book_with_an_empty_asin(tmp_path):
    """asin が空の一覧でも、別の本が黙って飛ばされない。"""
    entries = [
        {"title": "一冊目", "asin": "", "format": "image_pdf"},
        {"title": "二冊目", "asin": "", "format": "image_pdf"},
    ]
    code, report = _run(tmp_path, entries, ["一冊目", "二冊目"], measure=_ok())
    assert [r["title"] for r in _rows(report)] == ["一冊目", "二冊目"]
    assert code == 0


def test_one_failure_does_not_stop_the_batch(tmp_path):
    entries = [
        {"title": "壊れた本", "asin": "A1", "format": "image_pdf"},
        {"title": "ふつうの本", "asin": "A2", "format": "image_pdf"},
    ]

    def measure(path, **kw):
        if "壊れた" in path:
            raise RuntimeError("読めません")
        return {"pdf_pages": 9, "sampled": 5, "chars": 10, "cpp": 2.0}

    code, report = _run(tmp_path, entries, ["壊れた本", "ふつうの本"], measure=measure)
    verdicts = [r["verdict"] for r in _rows(report)]
    assert verdicts[0].startswith("失敗: RuntimeError")
    assert verdicts[1] == "ok"
    # 失敗が残っているのに 0 で返すと、成功と見分けがつかない
    assert code == 1


def test_only_measures_the_asked_format(tmp_path):
    entries = [
        {"title": "まんが", "asin": "A1", "format": "image_pdf"},
        {"title": "文章の本", "asin": "A2", "format": "searchable_pdf"},
    ]
    code, report = _run(
        tmp_path, entries, ["まんが", "文章の本"], measure=_ok(), extra=("--only", "image_pdf")
    )
    assert [r["title"] for r in _rows(report)] == ["まんが"]
    assert code == 0


def test_refuses_an_unknown_format_for_only(tmp_path):
    with pytest.raises(SystemExit):
        _run(tmp_path, [], [], measure=_ok(), extra=("--only", "image"))


def test_does_not_touch_the_library(tmp_path):
    """**蔵書は読むだけ。** 書き換えるのはレポートの CSV だけ。"""
    entries = [{"title": "まんが", "asin": "A1", "format": "image_pdf"}]
    code, report = _run(tmp_path, entries, ["まんが"], measure=_ok())
    library = tmp_path / "lib"
    names = sorted(os.listdir(library))
    assert names == ["まんが.pdf"]
    assert (library / "まんが.pdf").read_bytes() == b"%PDF-1.4\n"
    assert os.path.dirname(os.path.abspath(report)) != str(library)
    assert code == 0
