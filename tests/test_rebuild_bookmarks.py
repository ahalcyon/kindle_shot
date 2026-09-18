"""scripts/rebuild_bookmarks.py のテスト（CLI・一覧 CSV・打ち切り。#114）

判定そのもの（何を書くか）は core/bookmark_rebuild.py 側のテストにある。
"""

import csv
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import rebuild_bookmarks as rb  # noqa: E402

from core import bookmark_rebuild as br  # noqa: E402
from core.kindle_toc import TocEntry  # noqa: E402


def test_shift_summary_lists_each_run_of_the_same_shift():
    entries = [TocEntry(1, "a", 0, shift=s) for s in (2, 2, 0, 0, 2)]
    assert br.shift_summary(entries) == "2→0→2"


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


def test_a_shift_chosen_from_text_is_listed_and_flagged(tmp_path):
    """表紙の分と違うずれ幅を選んだ本は、confirmed だけでは確かめたことにならないので要確認。"""
    structure = _structure([[p, p + 9] for p in range(0, 40, 10)])
    structure["toc"] = [{"label": f"章{i}", "tocPositionId": i * 10} for i in range(4)]
    pdf, books, lib, cache = _library(tmp_path, pdf_pages=4, structure=structure)
    code, rows = _run(tmp_path, books, lib, cache)
    assert rows[0]["shifts"] == "0"
    assert br.FLAG_SHIFTED not in rows[0]["flags"]

    # 章名が位置のページの 1 つ後ろに続けて見つかる本は、ずれ幅 1 を選んで要確認にする
    from reportlab.pdfgen import canvas

    shifted = str(tmp_path / "shifted.pdf")
    c = canvas.Canvas(shifted)
    for i in range(5):
        c.drawString(72, 720, "body text " * 8)
        if i >= 1:
            c.drawString(72, 700, f"chapter{i - 1:02d}")
        c.showPage()
    c.save()
    structure = _structure([[p, p + 9] for p in range(0, 50, 10)])
    structure["toc"] = [{"label": f"chapter{i:02d}", "tocPositionId": i * 10} for i in range(4)]
    entries, row, flags = br.plan_book(shifted, structure)
    assert row["shifts"] == "1"
    assert [e.page for e in entries] == [1, 2, 3, 4]
    assert br.FLAG_SHIFTED in flags


def test_cached_structure_looks_for_the_dialog_only_after_a_failed_fetch(tmp_path, monkeypatch):
    """非対応のダイアログは開いた直後には出ていない。取れなかった本だけ見る (#114)。"""
    import contextlib

    import core.headless_browser as hb
    import core.headless_capture as hc
    import core.kindle_toc as kt

    @contextlib.contextmanager
    def fake_open_reader(*args, **kwargs):
        yield object()

    monkeypatch.setattr(hb, "open_reader", fake_open_reader)
    looked = []

    def dialog(page):
        looked.append(1)
        return "Kindleアプリが必要です"

    monkeypatch.setattr(hc, "unsupported_reason", dialog)

    # 取れた本ではダイアログを見に行かない（誤判定で取れる本を飛ばす経路を作らない）
    monkeypatch.setattr(kt, "fetch_book_structure", lambda page: {"toc": [], "pages": []})
    assert br.cached_structure(str(tmp_path), "B0TEST") == {"toc": [], "pages": []}
    assert looked == []

    # 取れなかった本はダイアログを見て、非対応なら失敗ではなく UnsupportedBook にする
    def no_render(page):
        raise RuntimeError("描画要求が出ませんでした（本を開けていない可能性）")

    monkeypatch.setattr(kt, "fetch_book_structure", no_render)
    with pytest.raises(br.UnsupportedBook):
        br.cached_structure(str(tmp_path), "B0TEST")
    assert looked == [1]

    # ダイアログが無ければ元の失敗のまま（非対応にすり替えない）
    monkeypatch.setattr(hc, "unsupported_reason", lambda page: None)
    with pytest.raises(RuntimeError):
        br.cached_structure(str(tmp_path), "B0TEST")


def test_an_unsupported_book_is_not_counted_as_a_failure(tmp_path, monkeypatch):
    """Kindle アプリでしか開けない本は直しようが無い。失敗に数えると内訳がずれる (#114)。"""
    import shutil

    from core.book_format import book_pdf_path

    pdf, books, lib, cache = _library(
        tmp_path, pdf_pages=2, structure=_structure([[0, 9], [10, 19]])
    )
    # 非対応 1 冊 + 本物の失敗 1 冊。終了コードは失敗の 1 冊だけで決まる
    listed = json.loads(books.read_text(encoding="utf-8"))
    listed.append({"title": "本2", "asin": "B0FAIL", "format": "searchable_pdf"})
    books.write_text(json.dumps(listed), encoding="utf-8")
    shutil.copyfile(pdf, book_pdf_path(str(lib), "本2"))

    def structures(cache_dir, asin, **kwargs):
        if asin == "B0FAIL":
            raise RuntimeError("描画要求が出ませんでした（本を開けていない可能性）")
        raise br.UnsupportedBook("Kindleアプリが必要です")

    monkeypatch.setattr(rb, "cached_structure", structures)
    before = pathlib.Path(pdf).read_bytes()
    code, rows = _run(tmp_path, books, lib, cache, "--apply", "--include-flagged")
    assert code == 1
    status = {r["title"]: r["status"] for r in rows}
    assert status["本"].startswith("Cloud Reader 非対応")
    assert status["本2"].startswith("失敗")
    assert pathlib.Path(pdf).read_bytes() == before


def test_unsupported_books_still_count_towards_the_stop_rule(tmp_path, monkeypatch):
    """非対応でもブラウザは開いている。続くなら打ち切る (#114)。"""
    import shutil

    from core.book_format import book_pdf_path

    pdf, books, lib, cache = _library(
        tmp_path, pdf_pages=2, structure=_structure([[0, 9], [10, 19]])
    )
    listed = json.loads(books.read_text(encoding="utf-8"))
    for i in range(rb.MAX_CONSECUTIVE_FAILURES + 3):
        listed.append({"title": f"本{i}", "asin": f"B0U{i}", "format": "searchable_pdf"})
        shutil.copyfile(pdf, book_pdf_path(str(lib), f"本{i}"))
    books.write_text(json.dumps(listed), encoding="utf-8")
    opened = []

    def unsupported(cache_dir, asin, **kwargs):
        opened.append(asin)
        raise br.UnsupportedBook("Kindleアプリが必要です")

    monkeypatch.setattr(rb, "cached_structure", unsupported)
    code, rows = _run(tmp_path, books, lib, cache)
    assert len(opened) == rb.MAX_CONSECUTIVE_FAILURES
    assert len(rows) == rb.MAX_CONSECUTIVE_FAILURES


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
