"""core/kindle_toc.py のテスト（Kindle の目次からしおりを作る。#114）"""

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from core import kindle_toc
from core.kindle_toc import (
    CONFIRMED,
    MOVED,
    UNCONFIRMED,
    UNSEARCHED,
    TocEntry,
    flatten_toc,
    map_to_pages,
    order_outliers,
    page_offset,
    positions_in_order,
    scan_positions,
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


RANGES = [[p, p + 9] for p in range(0, 60, 10)]  # 描画の 6 ページ（すき間なし）


def test_map_uses_the_page_that_contains_the_position():
    entries = map_to_pages(_entries(("A", 0), ("B", 25), ("C", 50)), RANGES, 6)
    assert [e.page for e in entries] == [0, 2, 5]
    assert all(e.how == UNSEARCHED for e in entries)


def test_map_confirms_when_the_title_is_on_the_estimated_page():
    texts = ["", "", "クィディッチ", "", "", ""]
    entries = map_to_pages(
        _entries(("第11章 クィディッチ", 25)), RANGES, 6, page_text=lambda i: texts[i]
    )
    assert (entries[0].page, entries[0].how) == (2, CONFIRMED)


def test_map_refuses_empty_page_ranges():
    # 空のまま進めると全部のしおりが先頭ページに付く
    with pytest.raises(ValueError):
        map_to_pages(_entries(("A", 0)), [], 6)


def test_positions_in_order_detects_a_toc_going_backwards():
    assert positions_in_order(_entries(("A", 0), ("B", 5), ("C", 5)))
    assert not positions_in_order(_entries(("A", 45), ("B", 5)))


def test_map_shifts_by_the_cover_offset():
    entries = map_to_pages(_entries(("A", 0), ("B", 25)), RANGES, 7, offset=1)
    assert [e.page for e in entries] == [1, 3]


def test_map_moves_forward_when_the_title_is_on_a_later_page():
    texts = ["", "", "本文", "本文 クィディッチ はじまる", "", ""]
    entries = map_to_pages(
        _entries(("第11章 クィディッチ", 25)), RANGES, 6, page_text=lambda i: texts[i]
    )
    assert (entries[0].estimated, entries[0].page, entries[0].how) == (2, 3, MOVED)


def test_map_does_not_move_backward_onto_a_single_earlier_mention():
    """1 項目だけの前への一致は、目次ページや本文中の言及であることが多い (#114)。"""
    texts = ["目次 クィディッチ", "", "本文", "", "", ""]
    entries = map_to_pages(
        _entries(("第11章 クィディッチ", 25)), RANGES, 6, page_text=lambda i: texts[i]
    )
    assert (entries[0].page, entries[0].how) == (2, UNCONFIRMED)


def test_map_does_not_search_one_character_titles():
    """1 文字の章名（「円」）は本文のどこにでも当たる。"""
    texts = ["", "", "", "円を描く", "", ""]
    entries = map_to_pages(_entries(("円", 25)), RANGES, 6, page_text=lambda i: texts[i])
    assert (entries[0].page, entries[0].how) == (2, UNSEARCHED)


def test_map_keeps_pages_in_toc_order():
    texts = ["", "", "", "", "ながい なまえ", ""]
    entries = map_to_pages(
        _entries(("ながい なまえ", 25), ("つぎ", 26)), RANGES, 6, page_text=lambda i: texts[i]
    )
    # 1 件目が 4 ページへ動いたら、2 件目は位置が 2 ページでもそれより前にしない
    assert [e.page for e in entries] == [4, 4]


def test_map_puts_a_position_in_a_gap_on_the_next_page():
    """位置の範囲のすき間にある目次の位置は、次のページの始まりを指している (#114)。"""
    ranges = [[0, 7], [10, 17], [20, 27], [30, 37]]
    entries = map_to_pages(_entries(("A", 5), ("B", 18), ("C", 29)), ranges, 4)
    assert [e.page for e in entries] == [0, 2, 3]


def test_map_does_not_run_past_the_last_page_for_a_gap_at_the_end():
    entries = map_to_pages(_entries(("A", 30)), [[0, 9], [10, 19]], 2)
    assert entries[0].page == 1


def _chapters(n, step=10):
    return [TocEntry(1, f"第{i + 1}章 なまえ{i + 1:02d}", i * step) for i in range(n)]


def test_map_changes_the_shift_part_way_through_the_book():
    """ずれ幅は本の途中で変わる（ハリー・ポッター: 第 2 巻 0、第 3 巻の途中から +2）。

    本全体で 1 つのずれ幅にすると、少ない側の区間が全部ずれる。
    """
    ranges = [[p, p + 9] for p in range(0, 100, 10)]  # 描画 10 ページ、各章 1 ページ
    texts = [""] * 12
    for i in range(4):  # 前半 4 章はずれなし
        texts[i] = f"なまえ{i + 1:02d}"
    for i in range(4, 10):  # 後半 6 章は 2 ページ後ろ
        texts[i + 2] = f"なまえ{i + 1:02d}"
    entries = map_to_pages(_chapters(10), ranges, 12, page_text=lambda i: texts[i])
    assert [e.page for e in entries] == [0, 1, 2, 3, 6, 7, 8, 9, 10, 11]
    assert all(e.how == CONFIRMED for e in entries)
    assert [e.shift for e in entries] == [0] * 4 + [2] * 6


def test_map_can_choose_a_backward_shift_for_a_run_of_titles():
    """ページが欠けた区間では前へのずれ幅になる（3 項目以上続けて見つかるとき）。"""
    ranges = [[p, p + 9] for p in range(0, 80, 10)]
    texts = [""] * 8
    for i in range(4):
        texts[i] = f"なまえ{i + 1:02d}"
    for i in range(5, 8):  # 5 ページ目が欠けて、後ろの 3 章は 1 ページ前
        texts[i - 1] = f"なまえ{i + 1:02d}"
    entries = map_to_pages(
        [e for i, e in enumerate(_chapters(8)) if i != 4], ranges, 8, page_text=lambda i: texts[i]
    )
    assert [e.shift for e in entries] == [0, 0, 0, 0, -1, -1, -1]
    assert [e.page for e in entries] == [0, 1, 2, 3, 4, 5, 6]


def test_map_needs_a_longer_run_to_shift_a_middle_section():
    """途中の区間は入って戻るので切り替え 2 回分。4 項目の一致では切り替えない。"""
    ranges = [[p, p + 9] for p in range(0, 120, 10)]
    texts = [""] * 12
    for i in range(12):
        texts[i if not 4 <= i < 8 else i - 2] += f" なまえ{i + 1:02d}"
    entries = map_to_pages(_chapters(12), ranges, 12, page_text=lambda i: texts[i])
    assert all(e.shift == 0 for e in entries)
    assert [e.how for e in entries[4:8]] == [UNCONFIRMED] * 4


def test_map_prefers_a_later_page_on_a_tie():
    ranges = [[p, p + 9] for p in range(0, 60, 10)]
    texts = [""] * 6
    for i in range(1, 5):  # 各章の名前が 1 つ前にも 1 つ後ろにもある
        texts[i - 1] += f" なまえ{i + 1:02d}"
        texts[i + 1] += f" なまえ{i + 1:02d}"
    entries = map_to_pages(_chapters(5)[1:], ranges, 6, page_text=lambda i: texts[i])
    assert all(e.shift == 1 for e in entries)


def test_map_does_not_follow_one_or_two_stray_mentions():
    """1〜2 項目だけの一致（目次ページ・柱・本文中の言及）ではずれ幅を変えない。"""
    ranges = [[p, p + 9] for p in range(0, 60, 10)]
    texts = ["", "なまえ01 なまえ02", "", "", "", ""]  # 1 ページ目にだけ 2 件の名前がある
    entries = map_to_pages(_chapters(2, step=20), ranges, 6, page_text=lambda i: texts[i])
    assert [e.shift for e in entries] == [0, 0]
    assert entries[1].page == 2


def test_map_moves_a_gap_entry_back_only_when_the_title_is_only_there():
    """すき間の項目で、次のページに無く 1 つ前のページにだけ章名があれば 1 つ前にする。"""
    ranges = [[0, 7], [10, 17], [20, 27]]
    texts = ["", "なまえ", ""]
    entries = map_to_pages(_entries(("なまえ", 8)), ranges, 3, page_text=lambda i: texts[i])
    assert (entries[0].estimated, entries[0].page, entries[0].how) == (1, 1, CONFIRMED)
    texts = ["なまえ", "", ""]
    entries = map_to_pages(_entries(("なまえ", 8)), ranges, 3, page_text=lambda i: texts[i])
    assert (entries[0].estimated, entries[0].page, entries[0].how) == (1, 0, MOVED)


def test_map_does_not_move_back_an_entry_that_is_not_in_a_gap():
    ranges = [[0, 9], [10, 19], [20, 29]]
    texts = ["なまえ", "", ""]
    entries = map_to_pages(_entries(("なまえ", 12)), ranges, 3, page_text=lambda i: texts[i])
    assert (entries[0].page, entries[0].how) == (1, UNCONFIRMED)


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


def test_write_outline_refuses_when_no_bookmark_fits(tmp_path):
    """書けるしおりが無いのに既存のしおりを消して空で置き換えない。"""
    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=2)
    before = pdf.read_bytes()
    result = write_outline(str(pdf), [TocEntry(1, "ない", 0, page=9)])
    assert not result["ok"]
    assert pdf.read_bytes() == before


def test_write_outline_keeps_the_file_when_verification_fails(tmp_path, monkeypatch):
    from core import text_layer

    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=3)
    before = pdf.read_bytes()
    calls = iter([["a"], ["b"]])  # 書き換え前と後で描画結果が違う
    monkeypatch.setattr(text_layer, "render_digests", lambda path, idx: next(calls))
    result = write_outline(str(pdf), [TocEntry(1, "x", 0, page=0)])
    assert not result["ok"] and "描画結果" in result["error"]
    assert pdf.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name != "book.pdf"]


# --- scan_positions: 描画 API を順に叩く走査（偽の get で確かめる） ---


def _book(page_ranges, *, bad=(), last=None, toc=None):
    """page_ranges の本を描く偽の get。bad に入る位置を含むページは描けない（None）。"""
    last = last if last is not None else page_ranges[-1][1]
    calls = []

    def page_of(position):
        for start, end in page_ranges:
            if position <= end:
                return start, end
        return None

    def get(num_pages, position):
        calls.append((num_pages, position))
        out: list[dict] = []
        p = position
        while len(out) < num_pages:
            found = page_of(p)
            if found is None:
                break
            start, end = found
            if any(start <= b <= end for b in bad):
                return None if not out else (out, toc, {"lastPositionId": last})
            out.append({"startPositionId": start, "endPositionId": end})
            p = end + 1
        return (out, toc, {"lastPositionId": last}) if out else ([], toc, {"lastPositionId": last})

    return get, calls


def test_scan_collects_every_page_and_completes():
    ranges = [[0, 9], [10, 19], [20, 29], [30, 39], [40, 49], [50, 59]]
    get, _ = _book(ranges, toc=[{"label": "A", "tocPositionId": 0}])
    got = scan_positions(get)
    assert got["pages"] == ranges and got["complete"] and not got["unrenderable"]
    assert got["toc"] == [{"label": "A", "tocPositionId": 0}]


def test_scan_skips_an_unrenderable_page_without_losing_the_next_one():
    """描けない区間の先は、最初に描けるページから続ける（間のページを落とさない）。"""
    ranges = [[i * 100, i * 100 + 99] for i in range(60)]
    get, _ = _book(ranges, bad=[250])
    got = scan_positions(get)
    assert [200, 299] not in got["pages"]
    assert [300, 399] in got["pages"]
    assert len(got["pages"]) == 59 and got["complete"]
    assert got["unrenderable"] == [[200, 300]]


def test_scan_gives_up_on_an_unrenderable_tail_with_a_bounded_number_of_requests():
    ranges = [[0, 9], [10, 19]]
    get, calls = _book(ranges, bad=[15], last=100_000_000)
    got = scan_positions(get)
    assert got["unrenderable"] == [[10, None]] and not got["complete"]
    assert len(calls) <= kindle_toc.SKIP_MAX_REQUESTS + 10


def test_scan_stops_when_the_server_repeats_pages():
    def get(num_pages, position):
        return ([{"startPositionId": 0, "endPositionId": 9}], None, None)

    got = scan_positions(get)
    assert got["pages"] == [[0, 9]] and not got["complete"]


def test_scan_has_a_request_ceiling(monkeypatch):
    monkeypatch.setattr(kindle_toc, "BOOK_MAX_REQUESTS", 5)
    ranges = [[i, i] for i in range(100)]
    get, _ = _book(ranges)
    with pytest.raises(RuntimeError):
        scan_positions(get)


def test_scan_backfills_pages_the_skip_step_jumped_over():
    """描けないページの直後のページを、探す刻み（25 位置）で飛び越えて落とさない。"""
    ranges = [[i * 10, i * 10 + 9] for i in range(60)]
    get, _ = _book(ranges, bad=[205])
    got = scan_positions(get)
    assert [210, 219] in got["pages"]
    assert got["unrenderable"] == [[200, 210]]
    assert len(got["pages"]) == 59


def test_write_outline_keeps_the_file_when_the_readback_differs(tmp_path, monkeypatch):
    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf, pages=3)
    before = pdf.read_bytes()
    monkeypatch.setattr(kindle_toc, "_outline_pages", lambda reader: [2])
    result = write_outline(str(pdf), [TocEntry(1, "x", 0, page=0)])
    assert not result["ok"] and "読み戻す" in result["error"]
    assert pdf.read_bytes() == before


def test_scan_keeps_pages_whose_ranges_overlap():
    """隣り合うページの位置の範囲が少し重なっても、重複として捨てない。"""
    # 1 回の要求は 4 ページ。4 ページ目の [30, 40] と次の要求で返る [38, 50] が重なる
    pages = [[0, 9], [10, 19], [20, 29], [30, 40], [38, 50], [51, 60]]

    def get(num_pages, position):
        out = [p for p in pages if p[1] >= position][:num_pages]
        meta = {"lastPositionId": 60}
        return ([{"startPositionId": a, "endPositionId": b} for a, b in out], None, meta)

    got = scan_positions(get)
    assert got["pages"] == pages and got["complete"]


def test_order_outliers_finds_the_one_entry_pointing_elsewhere():
    # 実測: 「Cover」が本の末尾を指し、残りは順に並んでいた
    entries = _entries(("Cover", 281307), ("Front Matter", 3), ("1 Intro", 50), ("2 Next", 900))
    assert order_outliers(entries) == [0]
    assert order_outliers(_entries(("A", 0), ("B", 5), ("C", 9))) == []
    assert order_outliers([]) == []
