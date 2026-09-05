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
    digest,
    hide_ui_css,
    is_signed_in,
    turn_key,
)


class FakePage:
    """screenshot / keyboard.press / wait_for_timeout だけを持つ page の代役。

    frames には各回のスクリーンショット内容を順に入れる。
    キーが押されるたびに次へ進む。
    """

    def __init__(self, frames):
        self.frames = list(frames)
        self.index = 0
        self.pressed: list[str] = []

        page = self

        class Keyboard:
            def press(self, key):
                page.pressed.append(key)
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
    assert (total, reason) == (2, "timeout")


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
    assert (total, reason) == (1, "timeout")


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
    )
    assert manifest["backend"] == "headless"
    assert manifest["total_pages"] == 3
    assert manifest["duration_seconds"] == 30.0
    json.dumps(manifest, ensure_ascii=False)


def test_digest_differs_per_content():
    assert digest(b"a") != digest(b"b")
    assert digest(b"a") == digest(b"a")
