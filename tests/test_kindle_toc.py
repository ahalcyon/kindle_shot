"""core/kindle_toc.py のテスト（Kindle の目次からしおりを作る。#114）"""

from pypdf import PdfReader
from reportlab.pdfgen import canvas

from core.kindle_toc import (
    EXACT,
    MOVED,
    UNCONFIRMED,
    TocEntry,
    flatten_toc,
    map_to_pages,
    page_offset,
    title_probe,
    write_outline,
)


def test_flatten_toc_keeps_order_and_levels():
    toc = [
        {"label": "表紙", "tocPositionId": 0},
        {
            "label": "第1部",
            "tocPositionId": 10,
            "entries": [
                {"label": "第1章 はじまり", "tocPositionId": 11},
                {"label": "", "tocPositionId": 12},
                {"label": "位置なし"},
            ],
        },
    ]
    got = [(e.level, e.title, e.position) for e in flatten_toc(toc)]
    assert got == [(1, "表紙", 0), (1, "第1部", 10), (2, "第1章 はじまり", 11)]


def test_title_probe_drops_numbering_that_ocr_breaks():
    # 番号は OCR が崩しやすい（「第１１章」を「第1章」と読む）ので名前の部分で探す
    assert title_probe("第１１章 クィディッチ") == "クィディッチ"
    assert title_probe("1-2-3 コンテナの提供価値") == "コンテナの提供価値"
    assert title_probe("Chapter12 フィードバックを受け取る") == "フィードバックを受け"
    assert title_probe("9-5 サービスの安全な更新/9-5-1 Blue/Green") == "サービスの安全な更新"


def test_page_offset_accepts_only_a_cover():
    assert page_offset(104, 103) == 1
    assert page_offset(201, 201) == 0
    assert page_offset(2659, 2657) is None
    assert page_offset(100, 101) is None


def _entries(*titles_positions):
    return [TocEntry(1, t, p) for t, p in titles_positions]


STARTS = [0, 10, 20, 30, 40, 50]  # 描画の 6 ページ


def test_map_uses_the_page_that_contains_the_position():
    entries = map_to_pages(_entries(("A", 0), ("B", 25), ("C", 50)), STARTS, 6)
    assert [e.page for e in entries] == [0, 2, 5]
    assert all(e.how == EXACT for e in entries)


def test_map_shifts_by_the_cover_offset():
    entries = map_to_pages(_entries(("A", 0), ("B", 25)), STARTS, 7, offset=1)
    assert [e.page for e in entries] == [1, 3]


def test_map_moves_forward_when_the_title_is_on_a_later_page():
    texts = ["", "", "本文", "本文 クィディッチ はじまる", "", ""]
    entries = map_to_pages(
        _entries(("第11章 クィディッチ", 25)), STARTS, 6, page_text=lambda i: texts[i]
    )
    assert (entries[0].estimated, entries[0].page, entries[0].how) == (2, 3, MOVED)


def test_map_never_moves_backward_onto_an_earlier_mention():
    """前への補正は目次ページや本文中の言及に当たって全部誤りだった (#114)。"""
    texts = ["目次 クィディッチ", "", "本文", "", "", ""]
    entries = map_to_pages(
        _entries(("第11章 クィディッチ", 25)), STARTS, 6, page_text=lambda i: texts[i]
    )
    assert (entries[0].page, entries[0].how) == (2, UNCONFIRMED)


def test_map_does_not_search_one_character_titles():
    """1 文字の章名（「円」）は本文のどこにでも当たる。"""
    texts = ["", "", "", "円を描く", "", ""]
    entries = map_to_pages(_entries(("円", 25)), STARTS, 6, page_text=lambda i: texts[i])
    assert (entries[0].page, entries[0].how) == (2, EXACT)


def test_map_keeps_pages_in_toc_order():
    texts = ["", "", "", "", "ながい なまえ", ""]
    entries = map_to_pages(
        _entries(("ながい なまえ", 25), ("つぎ", 26)), STARTS, 6, page_text=lambda i: texts[i]
    )
    # 1 件目が 4 ページへ動いたら、2 件目は位置が 2 ページでもそれより前にしない
    assert [e.page for e in entries] == [4, 4]


def _make_pdf(path, pages=5):
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 720, f"page {i + 1}")
        c.bookmarkPage(f"p{i}")
        c.addOutlineEntry(f"old {i}", f"p{i}", level=0)
        c.showPage()
    c.save()


def _outline(path):
    reader = PdfReader(str(path))
    out = []

    def walk(items, depth):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                out.append((depth, item.title, reader.get_destination_page_number(item)))

    walk(reader.outline, 0)
    return out


def test_write_outline_replaces_bookmarks_and_keeps_pages(tmp_path):
    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf)
    before = [p.extract_text() for p in PdfReader(str(pdf)).pages]
    entries = [
        TocEntry(1, "第1部", 0, page=0),
        TocEntry(2, "第1章", 1, page=1),
        TocEntry(3, "1-1", 2, page=2),
        TocEntry(1, "付録", 3, page=4),
    ]
    result = write_outline(str(pdf), entries)
    assert result["ok"], result
    assert _outline(pdf) == [(0, "第1部", 0), (1, "第1章", 1), (2, "1-1", 2), (0, "付録", 4)]
    assert [p.extract_text() for p in PdfReader(str(pdf)).pages] == before


def test_write_outline_does_not_jump_levels(tmp_path):
    """親の無い深さには付けない（1 つ上が無ければ詰める）。"""
    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=3)
    result = write_outline(str(pdf), [TocEntry(3, "深い", 0, page=0), TocEntry(1, "上", 1, page=1)])
    assert result["ok"], result
    assert _outline(pdf) == [(0, "深い", 0), (0, "上", 1)]


def test_write_outline_skips_pages_outside_the_pdf(tmp_path):
    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=2)
    result = write_outline(
        str(pdf), [TocEntry(1, "ある", 0, page=1), TocEntry(1, "ない", 1, page=9)]
    )
    assert result["ok"] and result["bookmarks"] == 1
    assert _outline(pdf) == [(0, "ある", 1)]


def test_write_outline_leaves_the_file_when_it_fails(tmp_path):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"not a pdf")
    result = write_outline(str(pdf), [TocEntry(1, "x", 0, page=0)])
    assert not result["ok"] and "error" in result
    assert pdf.read_bytes() == b"not a pdf"
