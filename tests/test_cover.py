"""表紙を 1 ページ目にする (#27)。"""

import io
import json
import os

from PIL import Image

from core import cover
from core.pipeline import COVER_NAME, add_cover_page


def _png(size, color=(10, 20, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


# ------------------------------------------------------------
# 商品ページから表紙の URL を取る
# ------------------------------------------------------------


def test_the_hires_attribute_wins():
    """data-old-hires を優先する。

    **画像 ID が違う**ので、小さいほうの URL のサイズ修飾子を書き換えても
    大きくならない（実測: 31kuSxEiAzL は ._SL2000_ でも 340x500 のまま）。
    """
    html = (
        '<img src="https://m.media-amazon.com/images/I/31small._SY445_.jpg" '
        'data-old-hires="https://m.media-amazon.com/images/I/61big._SL1500_.jpg">'
    )
    assert (
        cover.cover_url_from_html(html) == "https://m.media-amazon.com/images/I/61big._SL1500_.jpg"
    )


def test_the_largest_dynamic_image_is_used_as_a_fallback():
    """data-old-hires が無ければ、対応表の一番大きいものを採る。"""
    block = json.dumps(
        {
            "https://m.media-amazon.com/images/I/a._SY342_.jpg": [342, 233],
            "https://m.media-amazon.com/images/I/a._SY522_.jpg": [522, 355],
        }
    ).replace('"', "&quot;")
    html = f'<img data-a-dynamic-image="{block}">'
    assert cover.cover_url_from_html(html).endswith("_SY522_.jpg")


def test_a_page_without_a_cover_gives_nothing():
    assert cover.cover_url_from_html("<html><body>なし</body></html>") is None


# ------------------------------------------------------------
# 枠に収める
# ------------------------------------------------------------


def test_a_large_cover_is_scaled_down_to_fit():
    fitted = cover.fit_cover(_png((1021, 1500)), (1600, 1200))
    with Image.open(io.BytesIO(fitted)) as image:
        assert image.size == (1600, 1200)
        # 高さいっぱいまで縮小され、左右は余白
        assert image.getpixel((800, 600)) == (10, 20, 30)
        assert image.getpixel((5, 600)) == (255, 255, 255)


def test_a_small_cover_is_not_blown_up():
    """**引き伸ばさない。** ぼやけた表紙を足すのは、表紙が無いより悪い。

    ライブラリのサムネイル (340x500) を枠 (1600x1200) に合わせると 2.4 倍に
    なる。本文ページが等倍で入っている PDF の中で、1 ページだけぼやける。
    """
    fitted = cover.fit_cover(_png((340, 500)), (1600, 1200))
    with Image.open(io.BytesIO(fitted)) as image:
        assert image.size == (1600, 1200)
        # 等倍のまま中央に置かれる。340 幅なら x=630..970 だけが絵
        assert image.getpixel((800, 600)) == (10, 20, 30)
        assert image.getpixel((620, 600)) == (255, 255, 255), "引き伸ばされている"
        assert image.getpixel((800, 100)) == (255, 255, 255), "引き伸ばされている"


# ------------------------------------------------------------
# 1 ページ目に置く
# ------------------------------------------------------------


def test_the_cover_is_written_as_the_first_page(tmp_path):
    """本文は 001.png からなので、0 始まりなら必ず先に並ぶ。"""
    (tmp_path / "001.png").write_bytes(_png((1600, 1200)))
    assert add_cover_page(str(tmp_path), "B0TEST", fetch=lambda _asin: _png((1021, 1500))) is True
    assert COVER_NAME == "000.png"
    assert sorted(os.listdir(tmp_path)) == ["000.png", "001.png"]
    with Image.open(tmp_path / COVER_NAME) as image:
        assert image.size == (1600, 1200), "本文と同じ寸法にする（validate の size_mismatch）"


def test_a_book_without_a_cover_still_converts(tmp_path):
    """表紙が取れなくても本を落とさない。

    表紙が無いまま本文だけの PDF になるだけで、本の中身は 1 ページも欠けない。
    ここで落とすほうが損。
    """
    (tmp_path / "001.png").write_bytes(_png((1600, 1200)))
    events = []
    assert (
        add_cover_page(
            str(tmp_path),
            "B0TEST",
            fetch=lambda _asin: None,
            emit=lambda name, **kw: events.append((name, kw)),
        )
        is False
    )
    assert os.listdir(tmp_path) == ["001.png"]
    # 「取れなかった」と「置けなかった」を区別して報告する
    assert [kw["human"] for name, kw in events if name == "cover"] == [
        "表紙を取得できませんでした（本文だけで続けます）"
    ]


def test_a_missing_folder_is_not_an_error(tmp_path):
    """トリミング結果が無いときに例外で落ちない。"""
    assert (
        add_cover_page(str(tmp_path / "ない"), "B0TEST", fetch=lambda _asin: _png((10, 10)))
        is False
    )


def test_a_broken_cover_does_not_stop_the_book(tmp_path):
    """壊れた画像でも本を落とさない。"""
    (tmp_path / "001.png").write_bytes(_png((1600, 1200)))
    assert add_cover_page(str(tmp_path), "B0TEST", fetch=lambda _asin: b"not an image") is False
    assert os.listdir(tmp_path) == ["001.png"]
