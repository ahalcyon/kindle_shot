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


def _patterned(size, seed=0):
    """本文ページの代役。**単色にしない。** 単色どうしは「同じ絵」に見えるので、
    表紙が既にあるかの判定 (#27) がすべて真になってしまう。"""
    image = Image.new("RGB", size, (250, 250, 250))
    for i in range(0, size[0], 40):
        for j in range(0, size[1], 40):
            if (i + j + seed * 40) % 80 == 0:
                image.paste((20, 20, 20), (i, j, min(i + 20, size[0]), min(j + 20, size[1])))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
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


def test_another_products_cover_is_never_picked():
    """data-old-hires が無いときは**主画像の中だけ**で選ぶ。

    商品ページにはおすすめ商品のぶんも含めて data-a-dynamic-image が 7 個あり
    （実測）、うち 6 個は他商品の 420x420 だった。面積で選ぶと、この本の主画像が
    522x355 しかない本で**他商品の表紙が選ばれる**。黙って別の本の表紙が
    1 ページ目に入るくらいなら、表紙なしのほうがよい。
    """
    mine = json.dumps({"https://m.media-amazon.com/images/I/mine._SY342_.jpg": [342, 233]})
    other = json.dumps({"https://m.media-amazon.com/images/I/other._AC_UL420_.jpg": [420, 420]})
    html = (
        f'<img id="landingImage" data-a-dynamic-image="{mine.replace(chr(34), "&quot;")}">'
        f'<img data-a-dynamic-image="{other.replace(chr(34), "&quot;")}">'
    )
    picked = cover.cover_url_from_html(html)
    assert picked is not None, "主画像があるのに取れていない"
    assert "mine" in picked, f"他商品の表紙を選んでいる: {picked}"


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
    (tmp_path / "001.png").write_bytes(_patterned((1600, 1200)))
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
    (tmp_path / "001.png").write_bytes(_patterned((1600, 1200)))
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
    cover_events = [kw for name, kw in events if name == "cover"]
    assert [kw["human"] for kw in cover_events] == [
        "表紙を取得できませんでした（本文だけで続けます）"
    ]
    assert [kw["added"] for kw in cover_events] == [False]


def test_a_missing_folder_is_not_an_error(tmp_path):
    """トリミング結果が無いときに例外で落ちない。"""
    assert (
        add_cover_page(str(tmp_path / "ない"), "B0TEST", fetch=lambda _asin: _png((10, 10)))
        is False
    )


def test_a_broken_cover_does_not_stop_the_book(tmp_path):
    """壊れた画像でも本を落とさない。"""
    (tmp_path / "001.png").write_bytes(_patterned((1600, 1200)))
    assert add_cover_page(str(tmp_path), "B0TEST", fetch=lambda _asin: b"not an image") is False
    assert os.listdir(tmp_path) == ["001.png"]


# ------------------------------------------------------------
# 取りに行き方
# ------------------------------------------------------------


def test_the_product_page_is_read_then_the_image():
    """商品ページ → hires の URL → 画像、の順に取りに行く。"""
    urls = []

    def fake_get(url):
        urls.append(url)
        if "amazon" in url and "/dp/" in url:
            return b'<img data-old-hires="https://m.media-amazon.com/images/I/big.jpg">'
        return b"PNG-DATA"

    assert cover.fetch_cover("B0TEST", get=fake_get) == b"PNG-DATA"
    assert urls == [
        "https://www.amazon.co.jp/dp/B0TEST",
        "https://m.media-amazon.com/images/I/big.jpg",
    ]


def test_a_book_without_an_asin_is_not_fetched():
    """--url で開いた本では ASIN が無い。/dp/None を毎回取りに行かない。"""
    called = []

    def spy(url):
        called.append(url)
        return b""

    assert cover.fetch_cover(None, get=spy) is None
    assert called == []


def test_a_failure_is_not_raised():
    """取れないことは失敗にしない。例外を外へ出さない。"""

    def boom(_url):
        raise TimeoutError("応答なし")

    assert cover.fetch_cover("B0TEST", get=boom) is None


def test_a_page_without_a_hires_link_gives_nothing():
    assert cover.fetch_cover("B0TEST", get=lambda _u: "<html>なし</html>".encode()) is None


def test_the_request_has_a_timeout_and_a_user_agent(monkeypatch):
    """**上限の無い待ちを置かない。** 無人で 342 冊回すので、ここが無いと止まる。"""
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        seen["ua"] = request.get_header("User-agent")
        return FakeResponse()

    monkeypatch.setattr(cover.urllib.request, "urlopen", fake_urlopen)
    assert cover._get("https://example.test/x.jpg") == b"ok"
    assert seen["timeout"] == cover.REQUEST_TIMEOUT
    assert seen["timeout"] is not None
    assert "Mozilla" in seen["ua"]


# ------------------------------------------------------------
# 本文に合わせる
# ------------------------------------------------------------


def test_the_cover_matches_the_body_page_size(tmp_path):
    """本文が 1600x1200 以外の本でも、本文と同じ寸法にする。

    PDF のページ寸法を揃えるため（validate は表紙を足す前を見るので掛からない）。
    """
    (tmp_path / "001.png").write_bytes(_patterned((1240, 1754)))
    assert add_cover_page(str(tmp_path), "B0TEST", fetch=lambda _asin: _png((1021, 1500))) is True
    with Image.open(tmp_path / COVER_NAME) as image:
        assert image.size == (1240, 1754)


def test_a_cmyk_cover_becomes_rgb(tmp_path):
    """CMYK の JPEG を渡しても、置く表紙は RGB にする。

    Amazon は CMYK の JPEG を返しうる。PIL は CMYK でも paste 自体はできる
    （実測）ので落ちはしないが、**保存される表紙のモードが本文と揃わない**。
    """
    buffer = io.BytesIO()
    Image.new("CMYK", (600, 900)).save(buffer, format="JPEG")
    (tmp_path / "001.png").write_bytes(_patterned((1600, 1200)))
    assert add_cover_page(str(tmp_path), "B0TEST", fetch=lambda _a: buffer.getvalue()) is True
    with Image.open(tmp_path / COVER_NAME) as image:
        # 貼る先のキャンバスが RGB なので、保存されるモードは元画像に依らず RGB。
        # つまり convert("RGB") を外しても**この経路では観測できない**（実測）。
        # 守っているのは「CMYK の表紙で落ちないこと」まで
        assert image.mode == "RGB"
        assert image.size == (1600, 1200)


def test_adding_the_cover_is_reported(tmp_path):
    """表紙を付けたことを記録する。バッチ全体で何冊に付いたかを数えるため。"""
    (tmp_path / "001.png").write_bytes(_patterned((1600, 1200)))
    events = []
    add_cover_page(
        str(tmp_path),
        "B0TEST",
        fetch=lambda _a: _png((1021, 1500)),
        emit=lambda name, **kw: events.append((name, kw)),
    )
    cover_events = [kw for name, kw in events if name == "cover"]
    assert [kw["human"] for kw in cover_events] == ["表紙を 1 ページ目にしました（1600x1200）"]
    # **human は --json の出力に入らない。** 判定に使えるのは構造化した値だけ
    assert [kw["added"] for kw in cover_events] == [True]


# ------------------------------------------------------------
# パイプラインへの配線
# ------------------------------------------------------------


def _record(calls, args, kwargs):
    calls.append((args, kwargs))
    return True


def _run_book_collecting(tmp_path, monkeypatch, **kwargs):
    """run_book を通し、(add_cover_page の呼ばれ方, イベント) を返す。"""
    from core import headless_capture, pipeline

    save_dir = tmp_path / "本"
    calls: list = []
    events: list = []

    def fake_capture(*_a, **_k):
        os.makedirs(save_dir, exist_ok=True)
        Image.new("RGB", (1600, 1200), (1, 2, 3)).save(save_dir / "001.png")
        with open(save_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump({"shot_mode": "element", "total_pages": 1}, f)
        return 0

    monkeypatch.setattr(headless_capture, "run_headless_capture", fake_capture)
    monkeypatch.setattr(pipeline, "run_validate", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "run_trim", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "run_convert", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "add_cover_page", lambda *a, **k: _record(calls, a, k))
    pipeline.run_book(
        asin="B0TEST",
        title="本",
        output=str(tmp_path),
        fmt="image_pdf",
        headless=True,
        emit=lambda name, **kw: events.append((name, kw)),
        **kwargs,
    )
    return calls, events


def test_the_pipeline_adds_the_cover(tmp_path, monkeypatch):
    """既定では表紙を足す。機能をパイプラインから外したら落ちること。"""
    calls, _ = _run_book_collecting(tmp_path, monkeypatch)
    assert len(calls) == 1
    assert calls[0][0][1] == "B0TEST", "ASIN を渡していない"


def test_no_cover_skips_it(tmp_path, monkeypatch):
    """--no-cover を渡したら足さない。"""
    calls, _ = _run_book_collecting(tmp_path, monkeypatch, no_cover=True)
    assert calls == []


def test_the_step_numbers_stay_within_the_total(tmp_path, monkeypatch):
    """[5/4] を出さない。current / total は README の JSON Lines 仕様（外部契約）。"""
    for kwargs in ({}, {"no_cover": True}):
        _, events = _run_book_collecting(tmp_path, monkeypatch, **kwargs)
        steps = [kw for name, kw in events if name == "run_step"]
        assert steps, "ステップが出ていない"
        for kw in steps:
            assert kw["current"] <= kw["total"], f"{kw['current']}/{kw['total']} ({kwargs})"


def test_a_book_whose_first_page_is_already_the_cover_is_left_alone(tmp_path):
    """リーダーのページ送りに表紙が入っている本には足さない (#27)。

    マンガはたいていそうで、しかもフル解像度（1600x1200）。そこへ 353x500 の
    ストア画像を足すと、**同じ絵が小さく劣化して 2 枚並ぶ**。実測（未来日記・
    ヨコハマ買い出し紀行・てーきゅう）で、取得した表紙とリーダー 1 ページ目の
    差は 1.32 / 1.48 / 2.79 だった。
    """
    # **実際の形に合わせる。** ストアの表紙は小さく（実測 353x500）、リーダーの
    # ページはフル解像度。同じ絵でも寸法も枠も違うので、そこを揃えて比べられる
    # ことが要点
    page = _patterned((1600, 1200))
    with Image.open(io.BytesIO(page)) as full:
        buffer = io.BytesIO()
        full.resize((353, 500), Image.Resampling.LANCZOS).save(buffer, format="PNG")
    store = buffer.getvalue()
    (tmp_path / "001.png").write_bytes(page)
    events = []
    added = add_cover_page(
        str(tmp_path),
        "B0TEST",
        fetch=lambda _a: store,
        emit=lambda name, **kw: events.append((name, kw)),
    )
    assert added is False
    assert os.listdir(tmp_path) == ["001.png"], "同じ表紙を 2 枚並べている"
    assert [kw["human"] for name, kw in events if name == "cover"] == [
        "1 ページ目が既に表紙なので足しません"
    ]


def test_a_lookalike_title_page_still_gets_the_cover(tmp_path):
    """表紙と同じデザインの扉は「同じ絵」ではない。足す。

    実測で、表紙と扉の差は 16.09（真の重複は最大 2.79）。**迷ったら足す側に
    倒す。** 冗長な 1 ページより、表紙が無いほうが困る。
    """
    (tmp_path / "001.png").write_bytes(_patterned((1600, 1200), seed=1))
    assert (
        add_cover_page(str(tmp_path), "B0TEST", fetch=lambda _a: _patterned((1021, 1500))) is True
    )
    assert sorted(os.listdir(tmp_path)) == ["000.png", "001.png"]
