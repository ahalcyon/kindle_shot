"""core/format_check.py のテスト（形式の判定が当たっていたかを後から確かめる。#96）"""

import pytest
from reportlab.pdfgen import canvas

from core import format_check as fc
from core.ocr_layout import Line, PageLayout


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


def _line(text, category):
    return Line(text=text, left=0, top=0, right=10, bottom=10, confidence=0.9, category=category)


def _layout(filename):
    """本文・柱・ノンブル・図版が混ざった実物と同じ形のページ。"""
    return PageLayout(
        filename=filename,
        width=1600,
        height=1200,
        lines=[
            _line("ほんぶん", "本文"),  # 4 字
            _line("みだし", "タイトル本文"),  # 3 字
            _line("柱に出る書名ヘッダー", "柱"),  # UI。数えない
            _line("123", "ノンブル"),  # UI。数えない
            _line("絵から拾ったノイズ", "図版"),  # 本文でない。数えない
        ],
    )


def test_measure_counts_only_body_lines(tmp_path, monkeypatch):
    """**本文行だけ**を数える。全文字を数えると漫画で桁が変わる（#96 の実測）。

    柱・ノンブル・図版の行を混ぜてあるので、全文字に戻すとこのテストが落ちる。
    """
    pdf = _pdf(tmp_path / "a.pdf", 40)
    seen = {}

    def fake(folder, layout=False):
        assert layout is True  # 行の種別が要る。layout なしだと種別が無く全文字になる
        import os

        names = sorted(n for n in os.listdir(folder))
        seen["pages"] = len(names)
        return True, [_layout(n) for n in names]

    monkeypatch.setattr("core.ocr_engine.process_folder_collect", fake)
    got = fc.measure_pdf(pdf, sample=5)
    assert seen["pages"] == 5
    assert got == {"pdf_pages": 40, "sampled": 5, "chars": 35, "cpp": 7.0}  # (4+3) × 5 ページ


def test_measure_renders_at_the_size_of_the_captured_image(tmp_path, monkeypatch):
    """既定の解像度は等倍。蔵書の PDF はページの大きさが画像のピクセル数そのもの。"""
    pdf = _pdf(tmp_path / "s.pdf", 10)
    sizes = []

    def fake(folder, layout=False):
        import os

        from PIL import Image

        for name in sorted(os.listdir(folder)):
            with Image.open(os.path.join(folder, name)) as image:
                sizes.append(image.size)
        return True, [_layout(n) for n in sorted(os.listdir(folder))]

    monkeypatch.setattr("core.ocr_engine.process_folder_collect", fake)
    fc.measure_pdf(pdf, sample=2)
    # reportlab の既定ページは 595x842pt。72dpi なら同じ大きさで出る（引き伸ばさない）
    assert sizes and all(abs(w - 595) <= 1 and abs(h - 842) <= 1 for w, h in sizes)


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
    assert fc.verdict(fc.IMAGE, fc.THRESHOLD + 0.1) == fc.SUSPECT_TEXT
    assert fc.verdict(fc.IMAGE, fc.THRESHOLD) == fc.OK


def test_verdict_does_not_pass_an_unknown_format(tmp_path):
    """形式が分からない本を黙って ok にしない（format の無い一覧を渡したとき）。"""
    assert fc.verdict("", 1000.0) == fc.UNKNOWN
    assert fc.verdict("text_pdf", 1.0) == fc.UNKNOWN
