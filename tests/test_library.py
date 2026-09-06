"""core/library.py のテスト（蔵書一覧の取得）

ブラウザは使わず、page を模したオブジェクトでスクロールの打ち切りと
出力形式を固定する。出力は cli.py batch がそのまま読める形である必要がある。
"""

import json

import pytest

from core.library import clean_title, collect_items, dedupe, to_books


class FakeLibraryPage:
    """スクロールするたびに件数が増える page の代役。

    pages に各スクロール後の件数を順に入れる。
    """

    def __init__(self, counts, scrollable=True):
        self.counts = list(counts)
        self.index = 0
        self.scrollable = scrollable
        self.scrolls = 0

    def evaluate(self, js):
        if "coverContainer-" in js:
            n = self.counts[min(self.index, len(self.counts) - 1)]
            return [{"asin": f"B0{i:08d}", "title": f"本 {i} (Japanese Edition)"} for i in range(n)]
        # スクロール
        self.scrolls += 1
        if not self.scrollable:
            return False
        self.index += 1
        return True

    def wait_for_timeout(self, _ms):
        pass


# ------------------------------------------------------------
# スクロールによる全件取得
# ------------------------------------------------------------


def test_collects_until_count_stops_growing():
    """件数が増えなくなるまでスクロールする。

    初期表示は 50 件で、ライブラリのスクロール要素を送ると追加読み込みされる
    （実測: 50 -> 98 -> ... -> 405 で頭打ち）。
    """
    page = FakeLibraryPage([50, 98, 144, 405, 405])
    items = collect_items(page, scroll_wait=0)
    assert len(items) == 405


def test_stops_when_not_scrollable():
    """スクロール要素が無ければ、取れた分で打ち切る。"""
    page = FakeLibraryPage([50, 98], scrollable=False)
    items = collect_items(page, scroll_wait=0)
    assert len(items) == 50


def test_respects_max_scrolls():
    """増え続けても上限で止める（暴走防止）。"""
    page = FakeLibraryPage(list(range(1, 200)))
    collect_items(page, scroll_wait=0, max_scrolls=5)
    assert page.scrolls <= 5


# ------------------------------------------------------------
# 整形
# ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("みんなのフィードバック大全 (Japanese Edition)", "みんなのフィードバック大全"),
        ("物価とは何か (講談社選書メチエ) (Japanese Edition)", "物価とは何か (講談社選書メチエ)"),
        ("Some Book (English Edition)", "Some Book"),
        # 版表記でない末尾の括弧は残す
        ("てーきゅう　1 (アース・スターコミックス)", "てーきゅう　1 (アース・スターコミックス)"),
        ("", ""),
    ],
)
def test_clean_title(raw, expected):
    """保存フォルダ名になるので版表記は落とす。"""
    assert clean_title(raw) == expected


def test_dedupe_by_asin():
    """同じ本の複数版が並ぶことがあるので ASIN で一意化する。"""
    items = [
        {"asin": "B01", "title": "A"},
        {"asin": "B01", "title": "A 別版"},
        {"asin": "B02", "title": "B"},
        {"asin": "", "title": "ASIN 無し"},
    ]
    assert [i["asin"] for i in dedupe(items)] == ["B01", "B02"]


def test_to_books_is_batch_shaped():
    """batch が読める形（title と asin だけ）にする。"""
    books = to_books([{"asin": "B01", "title": "本 (Japanese Edition)"}])
    assert books == [{"title": "本", "asin": "B01"}]


def test_to_books_can_keep_edition():
    books = to_books([{"asin": "B01", "title": "本 (Japanese Edition)"}], keep_edition=True)
    assert books[0]["title"] == "本 (Japanese Edition)"


def test_to_books_drops_titleless_entries():
    """タイトルが空だと保存フォルダ名を作れないので落とす。"""
    assert to_books([{"asin": "B01", "title": "  "}]) == []


# ------------------------------------------------------------
# 書き出し
# ------------------------------------------------------------


def test_dump_writes_batch_readable_json(tmp_path, monkeypatch):
    """書き出した JSON が load_batch_file の検証をそのまま通る。"""
    import contextlib

    from core import library, pipeline

    page = FakeLibraryPage([3, 3])

    @contextlib.contextmanager
    def fake_open_reader(url, **kwargs):
        yield page

    monkeypatch.setattr("core.headless_browser.open_reader", fake_open_reader)
    out = tmp_path / "books.json"
    assert library.run_library_dump(str(out), load_wait=0) == 0

    books = json.loads(out.read_text(encoding="utf-8"))
    assert len(books) == 3
    assert set(books[0]) == {"title", "asin"}

    loaded, code = pipeline.load_batch_file(str(out), lambda *a, **k: None)
    assert code is None
    assert len(loaded) == 3


def test_dump_fails_when_library_is_empty(tmp_path, monkeypatch):
    """1 冊も取れなければ空のファイルを作らずエラーにする。"""
    import contextlib

    from core import library

    @contextlib.contextmanager
    def fake_open_reader(url, **kwargs):
        yield FakeLibraryPage([0, 0])

    monkeypatch.setattr("core.headless_browser.open_reader", fake_open_reader)
    out = tmp_path / "books.json"
    assert library.run_library_dump(str(out), load_wait=0) != 0
    assert not out.exists()
