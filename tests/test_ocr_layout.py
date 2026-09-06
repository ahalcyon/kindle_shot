"""core/ocr_layout.py のテスト

NDLOCR-Lite の JSON から行ごとの座標を取り出す部分。ここが落ちると
検索可能 PDF の不可視テキストが文字の位置に載らなくなる。
"""

from core.ocr_layout import Line, PageLayout, map_text, parse_ndl_json


def ndl_json(*lines, width=1280, height=1050):
    """NDLOCR-Lite の出力 JSON の形を作る。"""
    return {
        "imginfo": {"img_width": width, "img_height": height},
        "contents": [list(lines)],
    }


def ndl_line(text, left, top, right, bottom, vertical=True, conf=0.9, textline=True):
    return {
        "text": text,
        "boundingBox": [[left, top], [left, bottom], [right, top], [right, bottom]],
        "isVertical": "true" if vertical else "false",
        "isTextline": "true" if textline else "false",
        "confidence": conf,
        "class_index": 1,
    }


# ------------------------------------------------------------
# parse_ndl_json
# ------------------------------------------------------------


def test_parses_lines_with_coordinates():
    layout = parse_ndl_json(ndl_json(ndl_line("本文です", 1071, 0, 1091, 80)), "002.png")

    assert layout.filename == "002.png"
    assert (layout.width, layout.height) == (1280, 1050)
    assert len(layout.lines) == 1
    line = layout.lines[0]
    assert line.text == "本文です"
    assert (line.left, line.top, line.right, line.bottom) == (1071, 0, 1091, 80)
    assert line.vertical is True
    assert line.confidence == 0.9


def test_keeps_reading_order():
    """縦書きは右の列から読む。並べ替えず、返ってきた順を保つ。"""
    layout = parse_ndl_json(
        ndl_json(
            ndl_line("いち", 1071, 0, 1091, 40),
            ndl_line("に", 1038, 0, 1058, 40),
            ndl_line("さん", 1005, 0, 1025, 40),
        ),
        "002.png",
    )
    assert [line.text for line in layout.lines] == ["いち", "に", "さん"]


def test_skips_non_text_and_empty_entries():
    """テキスト行でないもの・空文字は落とす（図版の枠など）。"""
    layout = parse_ndl_json(
        ndl_json(
            ndl_line("図版", 0, 0, 100, 100, textline=False),
            ndl_line("   ", 200, 0, 220, 40),
            ndl_line("本文", 300, 0, 320, 40),
        ),
        "002.png",
    )
    assert [line.text for line in layout.lines] == ["本文"]


def test_survives_missing_fields():
    """boundingBox が無い行や壊れた形でも落ちない。"""
    layout = parse_ndl_json({"contents": [[{"text": "座標なし", "isTextline": "true"}]]}, "002.png")
    assert layout.lines == []
    assert (layout.width, layout.height) == (0, 0)


def test_survives_empty_document():
    assert parse_ndl_json({}, "002.png").lines == []


# ------------------------------------------------------------
# 1 文字の大きさ
# ------------------------------------------------------------


def test_font_size_is_the_short_side_of_the_line():
    """行の短辺が 1 文字の一辺。縦書きは行の幅、横書きは行の高さ。"""
    assert Line(text="あいう", left=100, top=0, right=120, bottom=60, vertical=True).font_size == 20
    assert Line(text="abc", left=0, top=10, right=60, bottom=30, vertical=False).font_size == 20


# ------------------------------------------------------------
# PageLayout
# ------------------------------------------------------------


def test_text_joins_lines_in_order():
    layout = parse_ndl_json(
        ndl_json(ndl_line("いち", 0, 0, 20, 40), ndl_line("に", 30, 0, 50, 40)), "002.png"
    )
    assert layout.text == "いち\nに"
    assert layout.as_pair() == ("002.png", "いち\nに")
    assert layout.positioned is True


def test_page_without_coordinates_keeps_its_text():
    """座標が取れない経路でもテキストは失わない。位置は無いと分かるようにする。"""
    layout = PageLayout(filename="002.png", fallback_text="座標なしの本文")

    assert layout.positioned is False
    assert layout.text == "座標なしの本文"


def test_map_text_applies_to_every_line():
    layout = parse_ndl_json(ndl_json(ndl_line("フィードバツク", 0, 0, 20, 140)), "002.png")
    fixed = map_text(layout, lambda t: t.replace("バツク", "バック"))

    assert fixed.lines[0].text == "フィードバック"
    # 座標は変えない
    assert fixed.lines[0].left == layout.lines[0].left
    # 元は書き換えない
    assert layout.lines[0].text == "フィードバツク"


def test_map_text_applies_to_the_fallback_text():
    layout = PageLayout(filename="002.png", fallback_text="バツク")
    assert map_text(layout, lambda t: t.replace("バツク", "バック")).text == "バック"
