"""scripts/rebuild_bookmarks.py のテスト（#114）"""

import csv
import json
import os
import pathlib
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import rebuild_bookmarks as rb  # noqa: E402

from core.kindle_toc import TocEntry  # noqa: E402

STARTS = [0, 10, 20, 30, 40, 50]


def test_choose_offset_prefers_the_shift_that_confirms_most_titles():
    # 途中に 2 枚差し込んだ本。後半の章は 2 ずらしたときにテキストで確かめられる
    texts = ["", "", "", "", "", "", "はじまり", "つづき", "", ""]
    entries = [TocEntry(1, "第1章 はじまり", 45), TocEntry(1, "第2章 つづき", 50)]
    got = rb.choose_offset(entries, STARTS, 10, lambda i: texts[i], range(0, 3))
    assert got == 2
    # 試しただけで entries は書き換えない
    assert all(e.page == 0 for e in entries)


def test_choose_offset_without_text_keeps_zero():
    entries = [TocEntry(1, "第1章 はじまり", 45)]
    assert rb.choose_offset(entries, STARTS, 10, None, range(0, 3)) == 0


def test_choose_offset_prefers_the_smaller_shift_on_a_tie():
    texts = [""] * 10
    entries = [TocEntry(1, "第1章 はじまり", 45)]
    assert rb.choose_offset(entries, STARTS, 10, lambda i: texts[i], range(0, 3)) == 0


def _library(tmp_path, *, pdf_pages=6, structure=None):
    from reportlab.pdfgen import canvas

    from core.book_format import book_pdf_path

    lib = tmp_path / "lib"
    cache = tmp_path / "cache"
    lib.mkdir()
    cache.mkdir()
    title = "本"
    pdf = book_pdf_path(str(lib), title)
    c = canvas.Canvas(pdf)
    for i in range(pdf_pages):
        c.drawString(72, 720, f"page {i + 1} chapter title here")
        c.showPage()
    c.save()
    books = tmp_path / "books.json"
    books.write_text(json.dumps([{"title": title, "asin": "B0TEST", "format": "searchable_pdf"}]))
    (cache / "B0TEST.json").write_text(json.dumps(structure), encoding="utf-8")
    return pdf, books, lib, cache


def _run(tmp_path, books, lib, cache, *extra):
    report = tmp_path / "report.csv"
    code = rb.main(
        [
            "--books",
            str(books),
            "--library",
            str(lib),
            "--cache",
            str(cache),
            "--report",
            str(report),
        ]
        + list(extra)
    )
    with open(report, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return code, rows


def _structure(pages, *, unrenderable=()):
    return {
        "toc": [{"label": "第1章 はじまり", "tocPositionId": 0}],
        "metadata": {"lastPositionId": pages[-1][1]},
        "pages": pages,
        "unrenderable": list(unrenderable),
        "complete": True,
    }


def test_apply_skips_flagged_books_unless_asked(tmp_path):
    """要確認の本は、見てから書き換える。既定の --apply では触らない。"""
    structure = _structure([[0, 9], [10, 19]], unrenderable=[[20, 30]])  # 描けない区間あり
    pdf, books, lib, cache = _library(tmp_path, pdf_pages=2, structure=structure)
    before = pathlib.Path(pdf).read_bytes()
    code, rows = _run(tmp_path, books, lib, cache, "--apply")
    assert rows[0]["status"] == "要確認のため未適用"
    assert pathlib.Path(pdf).read_bytes() == before

    code, rows = _run(tmp_path, books, lib, cache, "--apply", "--include-flagged")
    assert rows[0]["status"] == "書き換えた"


def test_apply_never_writes_without_page_ranges(tmp_path):
    structure = _structure([[0, 9]])
    structure["pages"] = []
    pdf, books, lib, cache = _library(tmp_path, pdf_pages=2, structure=structure)
    before = pathlib.Path(pdf).read_bytes()
    code, rows = _run(tmp_path, books, lib, cache, "--apply", "--include-flagged")
    assert rows[0]["status"] == "しおりを付けられないので未適用"
    assert pathlib.Path(pdf).read_bytes() == before


def test_a_failure_makes_the_exit_code_non_zero(tmp_path):
    pdf, books, lib, cache = _library(
        tmp_path, pdf_pages=2, structure=_structure([[0, 9], [10, 19]])
    )
    (cache / "B0TEST.json").write_text("{broken", encoding="utf-8")
    code, rows = _run(tmp_path, books, lib, cache)
    assert code == 1 and rows[0]["status"].startswith("失敗")


def test_a_single_out_of_order_entry_is_dropped_not_blocking(tmp_path):
    structure = _structure([[0, 9], [10, 19]])
    structure["toc"] = [
        {"label": "Cover", "tocPositionId": 19},
        {"label": "第1章 はじまり", "tocPositionId": 0},
        {"label": "第2章 つづき", "tocPositionId": 10},
    ]
    pdf, books, lib, cache = _library(tmp_path, pdf_pages=2, structure=structure)
    code, rows = _run(tmp_path, books, lib, cache, "--apply")
    assert rows[0]["dropped"] == "1" and rows[0]["toc_entries"] == "2"
    assert rows[0]["status"] == "書き換えた"


def test_a_badly_ordered_toc_blocks_writing(tmp_path):
    structure = _structure([[0, 9], [10, 19], [20, 29], [30, 39]])
    structure["toc"] = [
        {"label": f"第{i}章 なまえ", "tocPositionId": p} for i, p in enumerate([30, 20, 10, 0])
    ]
    pdf, books, lib, cache = _library(tmp_path, pdf_pages=4, structure=structure)
    before = pathlib.Path(pdf).read_bytes()
    code, rows = _run(tmp_path, books, lib, cache, "--apply", "--include-flagged")
    assert rows[0]["status"] == "しおりを付けられないので未適用"
    assert pathlib.Path(pdf).read_bytes() == before
