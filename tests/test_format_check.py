"""core/format_check.py のテスト（形式の判定が当たっていたかを後から確かめる。#96）"""

import pytest
from reportlab.pdfgen import canvas

from core import format_check as fc


def test_sample_avoids_the_cover_and_the_colophon():
    """前後は本文の文字数を代表しない（表紙・扉・奥付・広告）。"""
    picks = fc.sample_indexes(100, size=5)
    assert picks[0] >= 5 and picks[-1] < 95
    assert len(picks) == 5
    assert picks == sorted(picks)


def test_sample_takes_every_page_of_a_short_book():
    assert fc.sample_indexes(3, size=20) == [0, 1, 2]
    assert fc.sample_indexes(0) == []


def test_sample_spreads_over_the_book():
    """1 か所に固まると、たまたま文字の多い章を引いて判定が振れる。"""
    picks = fc.sample_indexes(200, size=10)
    gaps = [b - a for a, b in zip(picks, picks[1:], strict=False)]
    assert max(gaps) - min(gaps) <= 1


def _pdf(path, pages):
    c = canvas.Canvas(str(path))
    for _ in range(pages):
        c.showPage()
    c.save()
    return str(path)


class _Layout:
    def __init__(self, filename, chars):
        self.filename = filename
        self.lines = [_Line(chars)]
        self.positioned = True


class _Line:
    def __init__(self, chars):
        self.text = "あ" * chars
        self.is_body = True
        self.category = "body"
        self.confidence = 0.99


def test_measure_counts_body_characters_per_page(tmp_path, monkeypatch):
    """1 ページあたりの本文の文字数。**全文字ではない**（柱・ノイズ行を数えない）。"""
    pdf = _pdf(tmp_path / "a.pdf", 40)
    seen = {}

    def fake(folder, layout=False):
        assert layout is True  # 行の種別が要る。layout なしだと全文字になる
        names = sorted(n for n in __import__("os").listdir(folder))
        seen["pages"] = len(names)
        return True, [_Layout(n, 10) for n in names]

    monkeypatch.setattr("core.ocr_engine.process_folder_collect", fake)
    got = fc.measure_pdf(pdf, sample=5)
    assert seen["pages"] == 5
    assert got == {"pdf_pages": 40, "sampled": 5, "chars": 50, "cpp": 10.0}


def test_measure_reports_an_unusable_ocr(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "b.pdf", 10)
    monkeypatch.setattr(
        "core.ocr_engine.process_folder_collect",
        lambda *a, **k: (False, "ndlocr-lite がありません"),
    )
    with pytest.raises(RuntimeError, match="ndlocr-lite"):
        fc.measure_pdf(pdf, sample=3)


def test_verdict_flags_a_manga_book_that_is_full_of_text():
    """image_pdf の外れはテキスト層の無い本が残る（直すには撮り直し）。"""
    assert fc.verdict(fc.IMAGE, 300.0) == fc.SUSPECT_TEXT
    assert fc.verdict(fc.IMAGE, 70.1) == fc.OK  # 実測した image_pdf の最大


def test_verdict_flags_a_text_book_with_almost_no_text():
    assert fc.verdict(fc.SEARCHABLE, 10.0) == fc.SUSPECT_IMAGE
    assert fc.verdict(fc.SEARCHABLE, 212.7) == fc.OK  # 実測した searchable_pdf の最小


def test_verdict_uses_the_measured_gap():
    """閾値は実測の谷（70.1〜212.7）の中。境目をまたぐと印が変わる。"""
    assert fc.verdict(fc.IMAGE, fc.DEFAULT_THRESHOLD + 0.1) == fc.SUSPECT_TEXT
    assert fc.verdict(fc.IMAGE, fc.DEFAULT_THRESHOLD) == fc.OK
