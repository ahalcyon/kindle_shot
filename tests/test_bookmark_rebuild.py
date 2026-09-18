"""core/bookmark_rebuild.py のテスト（しおりを書くかどうかの判定。#114）"""

import json

import pytest
from reportlab.pdfgen import canvas

from core import bookmark_rebuild as br


def _pdf(path, pages, *, text=True):
    c = canvas.Canvas(str(path))
    for i in range(pages):
        if text:
            c.drawString(72, 720, f"chapter{i:02d} " + "body text " * 8)
        c.showPage()
    c.save()
    return str(path)


def _structure(pages):
    return {
        "toc": [{"label": f"chapter{i:02d}", "tocPositionId": i * 10} for i in range(pages)],
        "pages": [[p, p + 9] for p in range(0, pages * 10, 10)],
        "complete": True,
        "unrenderable": [],
    }


def test_rebuild_book_writes_a_plain_book(tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", 4)
    got = br.rebuild_book(pdf, _structure(4))
    assert got["written"] is True and got["reason"] == "" and got["flags"] == []


def test_rebuild_book_keeps_flagged_books_unless_asked(tmp_path):
    """要確認の本は黙って書き換えない。--include-flagged のときだけ書く。"""
    pdf = _pdf(tmp_path / "b.pdf", 4, text=False)  # テキスト層なし
    got = br.rebuild_book(pdf, _structure(4))
    assert got["written"] is False and got["reason"] == "要確認"
    assert br.FLAG_NO_TEXT in got["flags"]

    got = br.rebuild_book(pdf, _structure(4), include_flagged=True)
    assert got["written"] is True


def test_rebuild_book_allows_a_known_flag(tmp_path):
    """テキスト層が無いと分かっている形式（image_pdf）では、その印だけなら書く。"""
    pdf = _pdf(tmp_path / "c.pdf", 4, text=False)
    got = br.rebuild_book(pdf, _structure(4), allowed_flags=(br.FLAG_NO_TEXT,))
    assert got["written"] is True

    # 別の理由が付いた本は、許した印があっても書かない
    structure = _structure(4)
    structure["unrenderable"] = [[1, 2]]
    got = br.rebuild_book(pdf, structure, allowed_flags=(br.FLAG_NO_TEXT,))
    assert got["written"] is False and got["reason"] == "要確認"


def test_rebuild_book_never_writes_a_book_it_cannot_map(tmp_path):
    """目次が取れない・ページ範囲が無い本は、許しても書かない。"""
    pdf = _pdf(tmp_path / "d.pdf", 4)
    structure = _structure(4)
    structure["toc"] = []
    got = br.rebuild_book(pdf, structure, include_flagged=True)
    assert got["written"] is False and got["reason"] == "しおりを付けられない"


def test_rebuild_book_reports_a_write_failure(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "e.pdf", 4)
    monkeypatch.setattr(br, "write_outline", lambda *a, **k: {"ok": False, "error": "検証に失敗"})
    got = br.rebuild_book(pdf, _structure(4))
    assert got["written"] is False and got["reason"] == "検証に失敗"


def test_plan_book_keeps_the_numbers_for_the_report(tmp_path):
    pdf = _pdf(tmp_path / "f.pdf", 4)
    entries, row, flags = br.plan_book(pdf, _structure(4))
    assert row["toc_entries"] == 4 and row["confirmed"] == 4 and row["shifts"] == "0"
    assert json.dumps(flags, ensure_ascii=False) == "[]"
    assert [e.page for e in entries] == [0, 1, 2, 3]


def test_unsupported_book_is_its_own_error():
    assert issubclass(br.UnsupportedBook, Exception)
    with pytest.raises(br.UnsupportedBook):
        raise br.UnsupportedBook("Kindleアプリが必要です")
