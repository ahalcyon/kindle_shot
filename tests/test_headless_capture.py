"""core/headless_capture.py のテスト

実ブラウザを使わず、page を模したオブジェクトで撮影ループの判断を固定する。
特に守りたいのは「ページ送りキーの向き」と「最終ページの判定」:

縦書き（右→左）の本では ArrowRight は**前のページ**に戻る。表紙で右を押しても
何も起きないため、既定を right にすると 1 ページ目だけ撮って「最終ページ」と
誤判定する（実機で踏んだ）。
"""

import json
import os

import pytest

from core.headless_capture import (
    DEFAULT_TURN_KEY,
    build_manifest,
    capture_pages,
    detect_turn_key,
    digest,
    hide_ui_css,
    is_signed_in,
    read_position,
    reverse_of,
    turn_key,
)


class FakePage:
    """screenshot / keyboard.press / wait_for_timeout / url を持つ page の代役。

    frames には各回のスクリーンショット内容を順に入れる。
    swallow に指定した回数だけキー入力を食う（モーダルが出ている状況の再現）。
    """

    def __init__(self, frames, url="https://read.amazon.co.jp/?asin=B0X", swallow=0):
        self.frames = list(frames)
        self.index = 0
        self.url = url
        self.pressed: list[str] = []
        self.swallow = swallow

        page = self

        class Keyboard:
            def press(self, key):
                page.pressed.append(key)
                if page.swallow > 0:
                    page.swallow -= 1
                    return
                if page.index < len(page.frames) - 1:
                    page.index += 1

        self.keyboard = Keyboard()

    def screenshot(self):
        return self.frames[self.index]

    def wait_for_timeout(self, _ms):
        pass


# ------------------------------------------------------------
# ページ送りキー
# ------------------------------------------------------------


def test_default_turn_key_is_left_for_vertical_books():
    """既定は left。縦書きの本で right にすると 1 ページも進まない。"""
    assert DEFAULT_TURN_KEY == "left"
    assert turn_key(None) == "ArrowLeft"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("left", "ArrowLeft"),
        ("right", "ArrowRight"),
        ("pagedown", "PageDown"),
        ("pageup", "PageUp"),
        ("down", "ArrowDown"),
        ("up", "ArrowUp"),
        ("RIGHT", "ArrowRight"),
    ],
)
def test_turn_key_maps_profile_names(name, expected):
    """プロファイルの pyautogui キー名を Playwright のキー名にする。"""
    assert turn_key(name) == expected


def test_unknown_turn_key_falls_back():
    assert turn_key("nosuchkey") == "ArrowLeft"


# ------------------------------------------------------------
# 撮影ループ
# ------------------------------------------------------------


def test_captures_until_max_pages(tmp_path):
    page = FakePage([b"a", b"b", b"c", b"d"])
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_pages=3)
    assert (total, reason) == (3, "max_pages")
    assert sorted(os.listdir(tmp_path)) == ["001.png", "002.png", "003.png"]
    assert page.pressed == ["ArrowLeft", "ArrowLeft"]


def test_stops_at_last_page(tmp_path):
    """変化しなくなったら最終ページとみなす（リトライ後）。"""
    page = FakePage([b"a", b"b"])
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=2)
    assert (total, reason) == (2, "end_of_book")


def test_repeated_blank_page_is_not_dropped(tmp_path):
    """本文中に再登場する白紙ページを落とさない。

    全履歴と比較すると a の再登場を「送れていない」と誤認し、
    そのページを落として後続を詰めてしまう（実際に踏んだ回帰）。
    """
    page = FakePage([b"a", b"b", b"a", b"c"])
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft")
    assert (total, reason) == (4, "end_of_book")
    assert (tmp_path / "003.png").read_bytes() == b"a"
    assert (tmp_path / "004.png").read_bytes() == b"c"


def test_consecutive_blank_pages_do_not_truncate(tmp_path):
    """白紙が連続しても打ち切らず、その先まで撮り切る。

    連続する同一ページは画素だけでは「送り失敗」と区別できないため
    1 枚にまとまる（core/capture_engine.py も同じ制約）。ここで守りたいのは
    枚数の一致ではなく、**そこで打ち切って以降を失わないこと**。
    """
    page = FakePage([b"a", b"b", b"x", b"x", b"x", b"c"])
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=3)
    assert reason == "end_of_book"
    # 白紙の先にある c まで到達している（打ち切られていない）
    saved = [(tmp_path / n).read_bytes() for n in sorted(p.name for p in tmp_path.iterdir())]
    assert saved[-1] == b"c"
    assert saved == [b"a", b"b", b"x", b"c"]


def test_no_change_when_nothing_advances(tmp_path):
    """1 ページも進めなければ最終ページではなく送り失敗として報告する。

    縦書きの本で送りキーの向きを間違えると表紙から動かない。
    これを end_of_book にすると「正常に 1 ページの本を撮った」ことになる。
    """
    page = FakePage([b"cover", b"next"], swallow=99)
    total, reason = capture_pages(page, str(tmp_path), key="ArrowRight", max_retries=2)
    assert (total, reason) == (1, "no_change")


def test_modal_swallowing_keys_recovers(tmp_path):
    """一時的にキーを食われても、リトライ内で回復すれば継続する。"""
    page = FakePage([b"a", b"b", b"c"], swallow=1)
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=3)
    assert total == 3


def test_session_loss_is_reported(tmp_path):
    """途中でサインイン画面へ飛んだら、それを本文として保存せず中断する。"""
    page = FakePage([b"a", b"b"], url="https://www.amazon.co.jp/ap/signin?x=1")
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft")
    assert (total, reason) == (0, "signin_required")
    assert list(tmp_path.iterdir()) == []


def test_uses_the_given_key(tmp_path):
    page = FakePage([b"a", b"b"])
    capture_pages(page, str(tmp_path), key="ArrowRight", max_pages=2)
    assert page.pressed == ["ArrowRight"]


def test_saved_bytes_match_the_screenshot(tmp_path):
    page = FakePage([b"page-one", b"page-two"])
    capture_pages(page, str(tmp_path), key="ArrowLeft", max_pages=2)
    assert (tmp_path / "001.png").read_bytes() == b"page-one"
    assert (tmp_path / "002.png").read_bytes() == b"page-two"


def test_single_page_book(tmp_path):
    page = FakePage([b"only"])
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=1)
    assert (total, reason) == (1, "no_change")


# ------------------------------------------------------------
# その他
# ------------------------------------------------------------


def test_hide_ui_css_covers_reader_chrome():
    css = hide_ui_css()
    # 左右のシェブロンは幅 160px あり、隠さないと本文に食い込む
    for selector in (".top-chrome", "ion-footer", ".kr-chevron-container-left"):
        assert selector in css
    assert "display: none !important" in css


def test_is_signed_in():
    assert is_signed_in("https://read.amazon.co.jp/?asin=B0X") is True
    assert is_signed_in("https://www.amazon.co.jp/ap/signin?x=1") is False
    assert is_signed_in(None) is True


def test_manifest_is_json_serializable_and_marks_backend():
    import datetime

    started = datetime.datetime(2026, 9, 5, 10, 0, 0)
    finished = datetime.datetime(2026, 9, 5, 10, 0, 30)
    manifest = build_manifest(
        title="本",
        profile_key="kindle_cloud",
        profile=None,
        total=3,
        save_dir="/out/本",
        stopped_reason="max_pages",
        started=started,
        finished=finished,
        page_turn="ArrowLeft",
        page_wait=2.5,
    )
    assert manifest["backend"] == "headless"
    # 実行に使った値が残らないと manifest から実行内容を再現できない
    assert manifest["page_turn"] == "ArrowLeft"
    assert manifest["page_wait"] == 2.5
    assert manifest["total_pages"] == 3
    assert manifest["duration_seconds"] == 30.0
    json.dumps(manifest, ensure_ascii=False)


def test_digest_differs_per_content():
    assert digest(b"a") != digest(b"b")
    assert digest(b"a") == digest(b"a")


# ------------------------------------------------------------
# ページ送りの向き判定
# ------------------------------------------------------------


class FakeReader:
    """読書位置を持つ page の代役。

    forward に指定したキーで位置が増え、その逆で減る。
    """

    def __init__(self, forward="ArrowLeft", position=10, text=None):
        self.forward = forward
        self.position = position
        self.text = text
        self.url = "https://read.amazon.co.jp/?asin=B0X"
        self.presses: list[str] = []

        page = self

        class Keyboard:
            def press(self, key):
                page.presses.append(key)
                page.position += 1 if key == page.forward else -1

        self.keyboard = Keyboard()

    def locator(self, _selector):
        page = self

        class Loc:
            def count(self):
                return 0 if page.text == "" else 1

            @property
            def first(self):
                return self

            def inner_text(self):
                if page.text is not None:
                    return page.text
                return f"{page.position}/339ページ \u2002●\u2002 1%"

        return Loc()

    def wait_for_timeout(self, _ms):
        pass


def test_reverse_of():
    assert reverse_of("left") == "right"
    assert reverse_of("right") == "left"
    assert reverse_of("pagedown") == "pageup"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6/339ページ ● 1%", 6),
        ("位置1/3495 ● 0%", 1),
        ("11/339ページ", 11),
        ("", None),
        ("ページ", None),
    ],
)
def test_read_position(text, expected):
    """ページ表示と位置表示の 2 形式から先頭の数値を読む。"""
    assert read_position(FakeReader(text=text)) == expected


def test_detects_left_for_vertical_book():
    """縦書きの本では left が前進。"""
    page = FakeReader(forward="ArrowLeft")
    assert detect_turn_key(page, page_wait=0) == "left"


def test_detects_right_for_horizontal_book():
    """横書きの本では right が前進。left を試して位置が減れば right と分かる。"""
    page = FakeReader(forward="ArrowRight")
    assert detect_turn_key(page, page_wait=0) == "right"


def test_detection_restores_reading_position():
    """判定で動かした分を戻す（Whispersync の読書位置を動かすため）。"""
    page = FakeReader(forward="ArrowLeft", position=42)
    detect_turn_key(page, page_wait=0)
    assert page.position == 42


def test_detection_gives_up_without_position():
    """位置が読めなければ判定しない（決め打ちで進めない）。"""
    assert detect_turn_key(FakeReader(text=""), page_wait=0) is None
