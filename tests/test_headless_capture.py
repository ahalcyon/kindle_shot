"""core/headless_capture.py のテスト

実ブラウザを使わず、page を模したオブジェクトで撮影ループの判断を固定する。
特に守りたいのは「ページ送りキーの向き」と「最終ページの判定」:

縦書き（右→左）の本では ArrowRight は**前のページ**に戻る。表紙で右を押しても
何も起きないため、既定を right にすると 1 ページ目だけ撮って「最終ページ」と
誤判定する（実機で踏んだ）。
"""

import datetime
import io
import json
import os

import pytest

from core.headless_capture import (
    DEFAULT_TURN_KEY,
    DISMISS_ALERTS_JS,
    LEAVE_BUTTON_JS,
    MAX_START_POSITION,
    SHOT_ELEMENT,
    SHOT_VIEWPORT,
    TOC_ITEM_SELECTOR,
    _keys_respond,
    _still_at_start,
    _wait_for_position_change,
    alert_text,
    build_manifest,
    capture_pages,
    detect_turn_key,
    digest,
    edge_colors,
    hide_ui_css,
    is_signed_in,
    pad_shot,
    page_shot,
    read_position,
    read_position_pair,
    reader_padding,
    resolve_shot_mode,
    reverse_of,
    rewind_to_start,
    turn_key,
    unsupported_reason,
)


class FakePage:
    """screenshot / keyboard.press / wait_for_timeout / url を持つ page の代役。

    frames には各回のスクリーンショット内容を順に入れる。
    swallow に指定した回数だけキー入力を食う（モーダルが出ている状況の再現）。
    """

    def __init__(
        self,
        frames,
        url="https://read.amazon.co.jp/?asin=B0X",
        swallow=0,
        page_image=True,
        element_frames=None,
        alert="",
        page_image_lost_at=None,
        leave_button=False,
    ):
        self.frames = list(frames)
        # 要素撮影で返す内容。省略時は frames と同じ（方式を変えても中身は同じ）
        self.element_frames = list(element_frames) if element_frames else None
        self.index = 0
        self.url = url
        self.pressed: list[str] = []
        self.swallow = swallow
        # ページ画像の要素があるか（無い本はビューポート撮影にフォールバックする）
        self.page_image = page_image
        # 表示されているダイアログの文言（リーダーが落ちた状態の再現）
        self.alert = alert
        # この index 以降はページ画像の要素が消える（リーダーが落ちた形）
        self.page_image_lost_at = page_image_lost_at
        # 「ライブラリに戻る」しか無いダイアログが出ているか
        self.leave_button = leave_button
        self.dismissed = 0
        self.shots: list[str] = []

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
        self.shots.append("viewport")
        return self.frames[self.index]

    def locator(self, _selector):
        page = self

        class Loc:
            def count(self):
                if page.page_image_lost_at is not None and page.index >= page.page_image_lost_at:
                    return 0
                return 1 if page.page_image else 0

            @property
            def first(self):
                return self

            def screenshot(self):
                page.shots.append("element")
                frames = page.element_frames or page.frames
                return frames[page.index]

        return Loc()

    def wait_for_timeout(self, _ms):
        pass

    def add_style_tag(self, **_kw):
        """UI を隠す CSS の注入。撮影内容には影響しないので何もしない。"""

    def evaluate(self, js, arg=None):
        """alert_text / dismiss_dialogs / ボタン判定の代役。

        **本物の定数と突き合わせて見分ける。** 「JS に ion-alert が入っていたら
        dismiss」のような中身の当て推量は、片方の JS にセレクタが
        インライン化された瞬間に静かに嘘になる（テストは緑のまま）。
        """
        if js == LEAVE_BUTTON_JS:
            return self.leave_button
        if js == DISMISS_ALERTS_JS:
            self.dismissed += 1
            return 0
        return self.alert


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


def test_a_reader_error_is_not_the_end_of_the_book(tmp_path):
    """リーダーが落ちたら最終ページとみなさない (#76)。

    エラー画面になるとページ画像が消え、静止した画面を撮り続けるので
    ダイジェストは当然一致する。理由を見ずに end_of_book と呼ぶと、
    途中までの本が ok で完成し、batch は出力があるとスキップするので
    そのまま確定する。実際に合本 4 冊が 439〜672 ページでそうなった。
    """
    page = FakePage(
        [b"a", b"b"],
        alert="申し訳ありません。問題が発生しました\nライブラリからこの本をもう一度開いてみてください。",
    )
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=2)
    assert reason == "reader_error"
    assert total == 2


def test_a_vanished_page_image_is_not_the_end_of_the_book(tmp_path):
    """文言が変わってもページ画像が消えたことで拾う。

    最終ページに達しただけなら、ページ画像の要素は残っている。
    """
    page = FakePage([b"a", b"b"], page_image_lost_at=1)
    total, reason = capture_pages(
        page, str(tmp_path), key="ArrowLeft", max_retries=2, expect_mode=SHOT_ELEMENT
    )
    assert reason == "reader_error"
    assert total >= 1


def test_a_normal_last_page_is_still_the_end_of_the_book(tmp_path):
    """ダイアログもページ画像の消失も無ければ、これまでどおり最終ページ。"""
    page = FakePage([b"a", b"b"], alert="")
    total, reason = capture_pages(
        page, str(tmp_path), key="ArrowLeft", max_retries=2, expect_mode=SHOT_ELEMENT
    )
    assert (total, reason) == (2, "end_of_book")


def test_an_unrelated_dialog_does_not_become_a_reader_error(tmp_path):
    """関係のないダイアログを「落ちた」と読み替えない。

    位置同期のモーダルは普通に出る。これで中断すると、正常な本が
    軒並み失敗になる。
    """
    page = FakePage([b"a", b"b"], alert="前回読んでいたページに移動しますか？")
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=2)
    assert (total, reason) == (2, "end_of_book")


def test_a_dialog_that_only_leaves_the_reader_is_not_dismissed(tmp_path):
    """「ライブラリに戻る」しか無いダイアログは閉じない (#76)。

    押すとリーダーから出てしまう。残しておけば capture_pages が読んで中断できる。
    """
    page = FakePage([b"a", b"b"], leave_button=True)
    capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=2)
    assert page.dismissed == 0


def test_an_ordinary_dialog_is_still_dismissed(tmp_path):
    """普通のダイアログはこれまでどおり閉じる。

    位置同期のモーダルはキー入力を吸うので、閉じないと全冊で送れなくなる。
    **文言ではなくボタンで決める**のはこのため。「通信に問題が発生しました。
    再試行してください」のような一時的なダイアログまで閉じなくなると、
    これまで復帰できていた本が失敗する。
    """
    page = FakePage([b"a", b"b"], alert="通信に問題が発生しました。再試行してください")
    capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=2)
    assert page.dismissed > 0


def test_a_viewport_book_does_not_become_a_reader_error(tmp_path):
    """ページ画像の要素を持たない本を「落ちた」と誤判定しない。

    この条件から expect_mode のガードを外すと、viewport 撮影の本が
    **全冊** reader_error になる。
    """
    page = FakePage([b"a", b"b"], page_image=False)
    total, reason = capture_pages(
        page, str(tmp_path), key="ArrowLeft", max_retries=2, expect_mode=SHOT_VIEWPORT
    )
    assert (total, reason) == (2, "end_of_book")


def test_an_english_error_dialog_is_detected(tmp_path):
    """英語のダイアログでも拾う。大文字小文字は問わない。"""
    page = FakePage([b"a", b"b"], alert="Sorry, Something Went Wrong")
    _, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_retries=2)
    assert reason == "reader_error"


def test_the_stop_position_is_always_recorded(tmp_path):
    """止まった位置を必ずログに残す。

    2 つの検出（文言・要素の消失）はどちらも取り逃しうる。取り逃したぶんを
    あとから機械的に洗える手がかりは、止まった位置しかない。
    """
    events = []
    page = FakePage([b"a", b"b"])
    capture_pages(
        page,
        str(tmp_path),
        key="ArrowLeft",
        max_retries=2,
        emit=lambda name, **kw: events.append((name, kw)),
    )
    stopped = [kw for n, kw in events if n == "capture_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["reason"] == "end_of_book"
    assert "position" in stopped[0] and "book_total" in stopped[0]


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

    def __init__(
        self,
        forward="ArrowLeft",
        position=10,
        text=None,
        min_position=1,
        swallow=0,
        dismiss_jumps=None,
        blank_reads=0,
        blank_after=0,
        blank_at=None,
        total=339,
        transient=None,
        transient_ms=0,
        toc_start=None,
        toc_empty=False,
        toc_raises=False,
        keys_die_after_toc=False,
    ):
        self.forward = forward
        self.position = position
        # 見開き表示の本は位置が 1 まで下がらない（実機のマンガは 2 で止まる）
        self.min_position = min_position
        self.text = text
        # ダイアログを閉じた直後の n 回は入力が飲まれる（実測。#53）
        self.swallow = swallow
        # ダイアログを閉じると位置が飛ぶ本（Whispersync の「はい」を押した形）。
        # dismiss の呼び出しごとに先頭から消費する。None の回は何も起きない
        self.dismiss_jumps = list(dismiss_jumps or [])
        # 位置ラベルの描画が間に合わず読めない回（#69）。
        # blank_after 回ぶん読めたあと、blank_reads 回だけ空文字を返す。
        # blank_at を渡すと「何回目の読みが空か」を番号で指定できる。実機で
        # 起きたのは連続区間ではなく、間を置いて別々に読み落とす形だった
        self.blank_reads = blank_reads
        self.blank_after = blank_after
        self.blank_at = set(blank_at or ())
        # 押した直後に一瞬だけ返る値（遷移中のラベル）。#53 の事故の種
        self.transient = transient
        self.transient_ms = transient_ms
        self.clock = 0
        self.pressed_at = None
        self.total = total
        self.reads = 0
        # wait_for_timeout に渡されたミリ秒。刻んで見ているかの確認に使う (#57)
        self.waits: list[int] = []
        self.url = "https://read.amazon.co.jp/?asin=B0X"
        self.presses: list[str] = []
        # 目次の先頭項目へ飛んだときに落ち着く位置。None なら目次を持たない本
        self.toc_start = toc_start
        # 目次ボタンはあるが項目が出てこない本 / 項目のクリックが落ちる本
        self.toc_empty = toc_empty
        self.toc_raises = toc_raises
        # 目次から飛ぶとキーが死に、パネルを閉じても戻らない本。
        # これが silent partial book の種になる (#70 のレビュー指摘)
        self.keys_die_after_toc = keys_die_after_toc
        self.keys_dead = False
        self.toc_opened = False
        self.closed_with: list[str] = []
        self.mouse_clicks: list[tuple[int, int]] = []
        self.viewport_size = {"width": 1600, "height": 1200}

        page = self

        class Keyboard:
            def press(self, key):
                page.presses.append(key)
                # Escape は目次パネルを閉じるだけ。ページは動かない。
                # 「前進キー以外は全部後退」にすると、閉じた拍子に 1 ページ
                # 戻ったことになり、飛んだ先の報告が 1 ずれる
                if key == "Escape":
                    page.closed_with.append("escape")
                    return
                if page.keys_dead:
                    return
                page.pressed_at = page.clock
                # 巻の境界（min_position）は**上から**越えられない壁。目次で
                # 下へ飛んだあとは、その巻の中を 1 まで自由に戻れる。
                # 「位置は min_position を下回らない」と書くと、飛んだ先で
                # 戻れなくなり実機と食い違う
                floor = page.min_position if page.position >= page.min_position else 1
                moves = key == page.forward or page.position > floor
                # 動けない押下（先頭で戻ろうとする等）は「飲まれた 1 回」を
                # 消費しない。実測ではそうなっている（#53 のログ）
                if page.swallow > 0 and moves:
                    page.swallow -= 1
                    return
                if key == page.forward:
                    page.position += 1
                elif page.position > floor:
                    page.position -= 1

        self.keyboard = Keyboard()

        class Mouse:
            def click(self, x, y):
                page.mouse_clicks.append((x, y))
                page.closed_with.append("click")

        self.mouse = Mouse()

    def locator(self, selector):
        page = self

        if selector == TOC_ITEM_SELECTOR:

            class TocLoc:
                def count(self):
                    return 1 if page.toc_opened and page.toc_start is not None else 0

                @property
                def first(self):
                    return self

                def click(self, timeout=None):
                    if page.toc_raises:
                        raise RuntimeError("目次の項目を押せませんでした")
                    page.position = page.toc_start
                    if page.keys_die_after_toc:
                        page.keys_dead = True

            return TocLoc()

        class Loc:
            def count(self):
                return 0 if page.text == "" else 1

            @property
            def first(self):
                return self

            def text_content(self):
                # read_position は text_content を使う（UI を CSS で隠すため）
                page.reads += 1
                if page.reads in page.blank_at:
                    return ""
                if page.reads > page.blank_after and page.blank_reads > 0:
                    page.blank_reads -= 1
                    return ""
                if page.text is not None:
                    return page.text
                shown = page.position
                if (
                    page.transient is not None
                    and page.pressed_at is not None
                    and page.clock - page.pressed_at < page.transient_ms
                ):
                    shown = page.transient
                return f"{shown}/{page.total}ページ \u2002●\u2002 1%"

        return Loc()

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.clock += ms

    def wait_for_selector(self, _selector, timeout=None):
        """目次項目の描画待ちの代役。出てこない本では Playwright と同じく落ちる。"""
        if self.toc_empty or self.toc_start is None:
            raise RuntimeError("目次の項目が出ませんでした")
        return object()

    def evaluate(self, js, arg=None):
        """dismiss_dialogs / 目次を開く JS の代役。

        **本物の定数と突き合わせて見分ける。** JS の中身を当て推量すると、
        似た JS が増えたときに静かに取り違える。
        """
        if js == LEAVE_BUTTON_JS:
            return False
        if js != DISMISS_ALERTS_JS:  # TOC_OPEN_JS（目次ボタンをセレクタで押す）
            if self.toc_start is None and not self.toc_empty:
                return False
            self.toc_opened = True
            return True
        if not self.dismiss_jumps:
            return 0
        jump = self.dismiss_jumps.pop(0)
        if jump is None:
            return 0
        self.position = jump
        return 1


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


def test_position_pair_reads_the_total():
    """総量も読む。先頭まで戻れたかの判断材料になる (#69)。"""
    assert read_position_pair(FakeReader(text="位置1/3495 ● 0%")) == (1, 3495)
    assert read_position_pair(FakeReader(text="")) == (None, None)


def test_waiting_for_a_turn_stops_as_soon_as_the_page_moves():
    """動いたら上限まで待たない。page_wait 2.5 秒ぶんを毎回払わずに済む (#57)。"""
    page = FakeReader(forward="ArrowLeft", position=10)
    page.keyboard.press("ArrowLeft")  # 前進キーなので位置が 11 になる
    moved = _wait_for_position_change(page, 10, page_wait=2.5)
    assert moved == 11
    # 2.5 秒 x 2 回ぶん (5000ms) ではなく、落ち着くまでの 0.6 秒だけ払う
    assert page.waits == [600]


def test_a_transient_label_does_not_flip_the_detected_direction():
    """押した直後の一過性の値で向きを逆に判定しない (#53)。

    ラベルが 400ms のあいだ before より小さい値を返す本を作る。1 回目を
    250ms で読むと「押したら減った」＝逆向き、と判定して逆順の部分本が
    正常終了で完成する。落ち着いてから読めば正しい向きが出る。
    """
    page = FakeReader(forward="ArrowLeft", position=100, transient=99, transient_ms=400)
    assert detect_turn_key(page, page_wait=2.5) == "left"


def test_waiting_for_a_turn_gives_up_after_the_same_budget():
    """動かない向きに粘る時間は変えない。刻んで見るだけ。"""
    page = FakeReader(forward="ArrowLeft", position=10)
    assert _wait_for_position_change(page, 10, page_wait=2.5) is None
    assert sum(page.waits) == 5000  # page_wait 2.5 x attempts 2


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


def test_detects_at_the_first_page_when_a_press_is_swallowed():
    """#53 の本体。先頭ページ + 1 回目が飲まれる、で判定不能になっていた。

    先頭では後ろ方向が定義上動けないので、もう一方が 1 回飲まれると
    2 回の試行が両方無情報になる。
    """
    page = FakeReader(forward="ArrowRight", position=1, min_position=1, swallow=1)
    assert detect_turn_key(page, page_wait=0) == "right"


def test_detects_at_the_first_page_for_a_vertical_book():
    """先頭ページの縦書き本。left が前進で、後ろ方向は動けない。"""
    page = FakeReader(forward="ArrowLeft", position=1, min_position=1, swallow=1)
    assert detect_turn_key(page, page_wait=0) == "left"


def test_detection_restores_position_after_a_swallowed_press():
    """飲まれた分を数え違えて戻しすぎない。"""
    page = FakeReader(forward="ArrowLeft", position=42, swallow=1)
    detect_turn_key(page, page_wait=0)
    assert page.position == 42


def test_verdict_is_not_reversed_when_a_dialog_moves_the_position():
    """ダイアログを閉じた拍子の移動を、ページ送りの効果と取り違えない。

    古い基準と比べると向きを逆に判定しうる。逆向きのまま巻き戻すと
    rewind_to_start の停滞判定で「先頭に戻した」と成功扱いになり、
    逆走した部分本が完成扱いになる（この判定が防ぐべき事故そのもの）。
    """
    # 縦書き（ArrowLeft が前進）。判定の最初の dismiss では何も起きず、
    # 候補を押す直前の dismiss で 42 -> 10 へ飛ぶ
    page = FakeReader(forward="ArrowLeft", position=42, dismiss_jumps=[None, 10])
    assert detect_turn_key(page, page_wait=0) == "left"


def test_detection_gives_up_when_nothing_moves():
    """本当に動かない本では、決め打ちで進めずに諦める。"""
    page = FakeReader(forward="ArrowLeft", position=5, swallow=99)
    assert detect_turn_key(page, page_wait=0) is None


# ------------------------------------------------------------
# 先頭ページへの巻き戻し
# ------------------------------------------------------------


def test_the_toc_jump_crosses_a_volume_boundary():
    """合本はキーでは巻の境界を越えられない。目次からなら越えられる (#70)。

    実機のハリー・ポッター全 7 巻は位置 3216 付近から先へ戻れず、
    397 回押しても 3634 で止まった。目次からは 1 回で位置 1 まで飛ぶ。
    """
    page = FakeReader(forward="ArrowLeft", position=4512, min_position=3216, toc_start=1)
    ok, presses = rewind_to_start(page, "left", page_wait=0)
    assert ok is True
    assert page.position == 1
    assert presses == 0


def test_an_omnibus_without_a_toc_still_fails_loudly():
    """目次が無ければ従来どおり失敗する。黙って部分本を作らない。

    この壁は #69 以前なら「先頭に戻した」と成功扱いになり、半分の本が
    完成扱いで確定していた。
    """
    page = FakeReader(forward="ArrowLeft", position=4512, min_position=3216)
    ok, _ = rewind_to_start(page, "left", page_wait=0, max_rewind=50)
    assert ok is False


def test_the_toc_panel_is_closed_and_the_reader_is_clicked():
    """飛んだあとパネルを閉じ、描画領域を実際にクリックする。

    実測では、飛んだ直後はキーが一切効かない（8 回押しても位置が動かず、
    「次のページ」ボタンだけは効いた）。Escape で閉じたうえで実クリック
    しないと戻らない。閉じずに返すと後続のページ送りが全部飲まれ、
    「動かなくなった」と誤判定される（#72 と同じ形）。
    """
    page = FakeReader(forward="ArrowLeft", position=300, toc_start=1)
    rewind_to_start(page, "left", page_wait=0)
    assert "Escape" in page.presses
    assert page.mouse_clicks == [(800, 600)]


def test_a_book_without_a_toc_falls_back_to_keys():
    """目次を持たない本は、これまでどおりキーで戻す。"""
    page = FakeReader(forward="ArrowLeft", position=27)
    events = []
    ok, presses = rewind_to_start(
        page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw))
    )
    assert ok is True
    assert presses > 0
    assert [n for n, _ in events if n == "toc_jump"] == []


def test_the_toc_jump_is_skipped_when_already_at_the_start():
    """先頭付近で開いた本の目次は触らない。読書位置を無駄に動かさない。"""
    page = FakeReader(forward="ArrowLeft", position=3, toc_start=1)
    rewind_to_start(page, "left", page_wait=0)
    assert page.toc_opened is False
    assert page.mouse_clicks == []


def test_the_toc_jump_reports_where_it_actually_landed():
    """報告する位置は、飛んだ先を読み直した値。1 と決めつけない。

    目次の先頭項目が本当の先頭とは限らない（表紙や前付けを目次に
    載せない本がある）。決めつけると、残りをキーで詰める判断が狂う。
    """
    page = FakeReader(forward="ArrowLeft", position=300, toc_start=4)
    events = []
    rewind_to_start(page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw)))
    jumps = [kw for n, kw in events if n == "toc_jump"]
    assert jumps == [{"human": jumps[0]["human"], "before": 300, "after": 4}]
    assert page.position == 1  # 残りはキーで詰める


def test_dead_keys_after_a_jump_fail_loudly_instead_of_finishing_a_partial_book():
    """目次から飛んだあとキーが効かないなら、先頭に見えても失敗にする。

    これがこの変更で一番危ない経路。着地が位置 2〜10 で、パネルを閉じても
    キーが戻っていないと、ループは 3 回押して動かないので stuck から
    at_start = True になる。着地点は MAX_START_POSITION の枠内なので
    ok = True になり、**冒頭数ページが欠けた本が完成扱いで確定する**
    （batch は出力があるとスキップする）。

    この変更以前は、同じ故障が位置 1200 のような遠い場所で起きて必ず
    大声で落ちていた。目次ジャンプは着地点を枠の内側へ持ち込むので、
    loud failure が silent partial book に化ける。
    """
    page = FakeReader(forward="ArrowLeft", position=1200, toc_start=6, keys_die_after_toc=True)
    events = []
    ok, _ = rewind_to_start(
        page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw))
    )
    assert ok is False
    rewound = [kw for n, kw in events if n == "rewound"]
    assert [kw["reason"] for kw in rewound] == ["keys_dead"]


class CoarsePositionReader:
    """位置の数値がページより粗いリーダー。2 ページで位置が 1 進む。

    実機の合本がこれ。表紙と扉がどちらも「位置 1」で、位置の数値だけを見ると
    ページが進んだことが分からない。
    """

    def __init__(self, *, swallow=0):
        self.page_index = 0
        self.swallow = swallow
        self.waits: list[int] = []
        self.presses: list[str] = []
        reader = self

        class Keyboard:
            def press(self, key):
                reader.presses.append(key)
                if reader.swallow > 0:
                    reader.swallow -= 1
                    return
                if key == "ArrowLeft":
                    reader.page_index += 1
                else:
                    reader.page_index = max(0, reader.page_index - 1)

        self.keyboard = Keyboard()

    @property
    def position(self):
        return 1 + self.page_index // 2

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def locator(self, _selector):
        reader = self

        class Loc:
            def count(self):
                return 1

            @property
            def first(self):
                return self

            def text_content(self):
                return f"{reader.position}/6002ページ"

        return Loc()


def test_the_key_probe_puts_the_page_back_and_does_not_drop_the_cover():
    """探りを入れたぶんは、押した回数で戻す。位置の数値で判断しない。

    位置はページより粗く、表紙と扉が同じ「位置 1」になる本がある。
    「数値が戻ったら復帰」にすると、見た目は戻ったのに 1 ページ進んだままになり、
    **表紙が落ちる**。実機で踏んだ: 探りを入れた回の 1 ページ目が、入れなかった
    回の 2 ページ目とバイト単位で一致した。
    """
    page = CoarsePositionReader(swallow=1)
    assert _keys_respond(page, "left", page_wait=0) is True
    assert page.page_index == 0, "探りのぶんページが進んだまま＝表紙が落ちる"


def test_the_key_probe_over_presses_backwards_rather_than_under():
    """戻しは多めに押す。先頭で余分に押しても何も起きない。"""
    page = CoarsePositionReader()
    _keys_respond(page, "left", page_wait=0)
    assert page.presses.count("ArrowRight") > page.presses.count("ArrowLeft")


def test_a_swallowed_first_press_is_not_mistaken_for_dead_keys():
    """パネルを閉じた直後の 1 回目は飲まれる。1 回で「死んでいる」と決めない。

    実測（合本、目次から位置 1 へ飛んだ直後）:

        press1: 1   ← 飲まれた
        press2: 2
        press3: 3

    1 回で決めると、生きているキーを死んだと誤判定して**全部の本が
    巻き戻せなくなる**（実機で踏んだ）。ダイアログ直後に入力が飲まれるのは
    #53 で既に分かっていた挙動で、目次パネルでも同じだった。
    """
    page = FakeReader(forward="ArrowLeft", position=300, toc_start=1, swallow=1)
    ok, _ = rewind_to_start(page, "left", page_wait=0)
    assert ok is True


def test_a_spread_page_book_is_not_mistaken_for_dead_keys():
    """見開きの本を「キーが死んだ」と取り違えない。

    見開き表示の本は位置が 1 まで下がらず 2 で止まる（実測で 10 冊中 8 冊）。
    「一度も下がらなければ失敗」という判定にすると軒並み落ちるので、
    前進させて効くかどうかで見る。
    """
    page = FakeReader(forward="ArrowLeft", position=400, min_position=2, toc_start=2)
    ok, _ = rewind_to_start(page, "left", page_wait=0)
    assert ok is True
    assert page.position == 2


def test_the_panel_is_closed_even_when_the_items_never_appear():
    """項目が出てこなくても、開けたパネルは必ず閉じる。

    開きっぱなしで返すと、後続のページ送りが全部飲まれて「動かなくなった」と
    誤判定される（#72 と同じ形）。従来キーだけで戻せていた本が、
    ジャンプを試みたせいで戻せなくなる。
    """
    page = FakeReader(forward="ArrowLeft", position=300, toc_empty=True)
    ok, _ = rewind_to_start(page, "left", page_wait=0)
    assert page.closed_with == ["escape", "click"]
    assert ok is True  # キーでの巻き戻しに委ねて成功する


def test_the_panel_is_closed_even_when_the_item_click_raises():
    """項目のクリックが落ちても、開けたパネルは必ず閉じる。"""
    page = FakeReader(forward="ArrowLeft", position=300, toc_start=1, toc_raises=True)
    events = []
    ok, _ = rewind_to_start(
        page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw))
    )
    assert page.closed_with == ["escape", "click"]
    assert [n for n, _ in events if n == "toc_jump_failed"] != []
    assert ok is True


def test_the_panel_is_closed_before_the_reader_is_clicked():
    """閉じる順序を固定する。Escape で閉じてから描画領域をクリックする。"""
    page = FakeReader(forward="ArrowLeft", position=300, toc_start=1)
    rewind_to_start(page, "left", page_wait=0)
    assert page.closed_with == ["escape", "click"]


def test_an_unreadable_landing_is_still_reported_and_not_assumed_to_be_the_start():
    """飛んだ先が読めなくても、飛んだことはログに残す。先頭と決めつけない。

    出さないと、目次を通った本とキーだけの本をログから切り分けられない
    （#69 以降、原因別の集計で運用している）。
    """
    # 飛んだ直後から位置が読めなくなる本
    page = FakeReader(
        forward="ArrowLeft", position=4512, toc_start=1, blank_reads=99, blank_after=1
    )
    events = []
    ok, _ = rewind_to_start(
        page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw))
    )
    jumps = [kw for n, kw in events if n == "toc_jump"]
    assert [kw["after"] for kw in jumps] == [None]
    assert ok is False  # 読めないまま先頭とみなさない


def test_a_jump_that_lands_later_is_still_adopted():
    """飛んだ先が元より後ろでも、読み直した値を採用する。

    「下がったときだけ採用」にすると、飛んで実際に動いたのに before だけ
    古い値のまま残り、位置と食い違う。
    """
    page = FakeReader(forward="ArrowLeft", position=15, toc_start=90)
    events = []
    ok, _ = rewind_to_start(
        page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw))
    )
    jumps = [kw for n, kw in events if n == "toc_jump"]
    assert [(kw["before"], kw["after"]) for kw in jumps] == [(15, 90)]
    assert ok is True
    assert page.position == 1


def test_the_rewound_event_says_whether_the_toc_was_used():
    """目次を通ったかを rewound に残す。集計の主キーになる。"""
    by_keys = FakeReader(forward="ArrowLeft", position=27)
    events = []
    rewind_to_start(by_keys, "left", page_wait=0, emit=lambda n, **kw: events.append((n, kw)))
    assert [kw["via_toc"] for n, kw in events if n == "rewound"] == [False]

    by_toc = FakeReader(forward="ArrowLeft", position=300, toc_start=1)
    events = []
    rewind_to_start(by_toc, "left", page_wait=0, emit=lambda n, **kw: events.append((n, kw)))
    assert [kw["via_toc"] for n, kw in events if n == "rewound"] == [True]


def test_rewind_reaches_the_first_page():
    """読みかけの位置から先頭まで戻す。

    read.amazon.co.jp/?asin=... は前回の読書位置で開く（実測: 位置 27）。
    巻き戻さないと本の途中から末尾までだけ撮れ、しかも end_of_book で
    正常終了してしまう。batch は出力があるとスキップするので、
    半分だけの本がそのまま確定する。
    """
    page = FakeReader(forward="ArrowLeft", position=27)
    ok, presses = rewind_to_start(page, "left", page_wait=0)
    assert ok is True
    assert page.position == 1
    assert presses == 26


def test_rewind_is_noop_at_the_first_page():
    page = FakeReader(forward="ArrowLeft", position=1)
    ok, presses = rewind_to_start(page, "left", page_wait=0)
    assert (ok, presses) == (True, 0)


def test_rewind_stops_at_spread_pages():
    """見開き表示の本は位置が 1 まで下がらず 2 で止まる。

    「位置が 1 になったら先頭」で判定すると、マンガが軒並み
    「戻り切れなかった」として失敗する（実測で 10 冊中 8 冊が該当）。
    押しても下がらなくなったら先頭とみなす。
    """
    page = FakeReader(forward="ArrowLeft", position=8, min_position=2)
    ok, pressed = rewind_to_start(page, "left", page_wait=0, max_retries=3)
    assert ok is True
    assert page.position == 2


def test_rewind_respects_max_rewind():
    """暴走しないよう上限で打ち切り、届かなかったことを返す。"""
    page = FakeReader(forward="ArrowLeft", position=500)
    ok, presses = rewind_to_start(page, "left", page_wait=0, max_rewind=10)
    assert ok is False
    assert presses == 10


def test_rewind_gives_up_without_position():
    page = FakeReader(text="")
    ok, presses = rewind_to_start(page, "left", page_wait=0)
    assert (ok, presses) == (False, 0)


def test_rewind_does_not_call_a_far_position_the_start():
    """押しても下がらなくなっただけでは先頭の証拠にならない。

    実測 342 冊のバッチで巻き戻しが成功した 227 冊は、位置 1 / 2 / 3 で
    終わっている。位置 1658 で止まった本を先頭扱いしたために、冒頭 60 ページ
    （前付け＋第1〜4章）が落ちた本が ok で完成した (#69)。
    """
    page = FakeReader(forward="ArrowLeft", position=1658, min_position=1658)
    ok, _ = rewind_to_start(page, "left", page_wait=0, max_retries=3)
    assert ok is False


def test_rewind_reports_where_it_stopped():
    """位置だけでなく総量と理由も残す。後からログで判断できるように。"""
    events = []
    page = FakeReader(forward="ArrowLeft", position=1658, min_position=1658, total=3495)
    rewind_to_start(page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw)))
    rewound = [kw for name, kw in events if name == "rewound"]
    assert rewound and rewound[0]["total"] == 3495
    assert rewound[0]["reason"] == "stopped_short"
    assert rewound[0]["ok"] is False


def test_rewind_rereads_a_position_it_failed_to_read():
    """読めなかった回を「動かなかった」と数えない。

    長い巻き戻しの途中でラベルの描画が 3 回続けて間に合わないと、そこが
    先頭ということにされていた (#69)。読み直せば先頭まで戻れる。
    """
    page = FakeReader(forward="ArrowLeft", position=5, blank_reads=3, blank_after=1)
    ok, _ = rewind_to_start(page, "left", page_wait=0, max_retries=3)
    assert ok is True
    assert page.position == 1


def test_rewind_fails_when_the_position_stays_unreadable():
    """どこにいるか分からないなら、先頭だと決めつけない。"""
    page = FakeReader(forward="ArrowLeft", position=5, blank_reads=10**6, blank_after=1)
    ok, _ = rewind_to_start(page, "left", page_wait=0, max_retries=3)
    assert ok is False


def test_rewind_rebaselines_when_a_dialog_moves_the_reader():
    """ダイアログを閉じた拍子に位置が飛んだら、基準を取り直す。

    before は「それまでの最小値」であって今の位置ではない。飛んだあとに
    これと比べると 3 回で stuck が立ち、「位置 3 で先頭に着いた」と報告
    しながら実際は 1654 にいる、という事故になる。この PR が防ごうと
    しているものそのもの (#69)。
    """
    events = []
    page = FakeReader(forward="ArrowLeft", position=5, blank_at={4}, dismiss_jumps=[1656])
    ok, _ = rewind_to_start(
        page,
        "left",
        page_wait=0,
        max_retries=3,
        max_rewind=20,
        emit=lambda name, **kw: events.append((name, kw)),
    )
    assert ok is False
    # 報告した位置が実際の位置と合っていること。修正前は「位置 3 で先頭に
    # 着いた」と報告しながら実際は 1654 にいた
    rewound = [kw for name, kw in events if name == "rewound"][0]
    assert rewound["position"] == page.position


def test_a_dialog_that_does_not_move_the_reader_still_counts_as_stuck():
    """ダイアログを閉じただけで基準を取り直さない。

    先頭かどうかは「押しても下がらなくなった」でしか判定できない
    （見開きの本は位置 2 で止まる）。閉じただけで stuck を 0 に戻すと、
    ラベルが読めない回にダイアログが閉じられ続ける見開き本は、先頭に
    いるのに永久に先頭と判定されない (#69)。
    """
    page = FakeReader(
        forward="ArrowLeft",
        position=2,
        min_position=2,
        blank_at=set(range(2, 400, 2)),
        # 押すたびに閉じる。数が尽きて助かることのないよう多めに
        dismiss_jumps=[2] * 500,
    )
    ok, presses = rewind_to_start(page, "left", page_wait=0, max_retries=3, max_rewind=50)
    assert ok is True
    # 3 回続けて下がらなければ先頭。それ以上は押さない
    assert presses <= 5


def test_rewind_keeps_going_after_a_dialog_moves_the_reader():
    """飛ばされても、そこから戻り続ければ先頭に着ける。

    基準を取り直さないと「戻れていたのに打ち切られる」side も起きる。
    """
    page = FakeReader(forward="ArrowLeft", position=200, blank_at={3}, dismiss_jumps=[1656])
    ok, _ = rewind_to_start(page, "left", page_wait=0, max_retries=3, max_rewind=5000)
    assert ok is True
    assert page.position == 1


def test_rewind_reports_when_it_cannot_read_the_position_at_all():
    """最初から位置が読めないときも rewound を出す。

    出さないと、巻き戻しの結果を rewound で追う運用（e2e もそうしている）
    から、この経路だけが落ちる。
    """
    events = []
    page = FakeReader(text="")
    ok, presses = rewind_to_start(
        page, "left", page_wait=0, emit=lambda name, **kw: events.append((name, kw))
    )
    assert (ok, presses) == (False, 0)
    rewound = [kw for name, kw in events if name == "rewound"]
    assert rewound and rewound[0]["reason"] == "no_position"
    assert rewound[0]["ok"] is False


def test_still_at_start_accepts_the_first_pages():
    """巻き戻した後の確認。先頭付近ならそのまま撮り始めてよい。"""
    assert _still_at_start(FakeReader(position=1)) is True
    assert _still_at_start(FakeReader(position=MAX_START_POSITION)) is True


def test_still_at_start_refuses_a_far_position():
    """巻き戻した後にダイアログで飛ばされたら撮り始めない。"""
    events = []
    page = FakeReader(position=1656)
    ok = _still_at_start(page, emit=lambda name, **kw: events.append((name, kw)))
    assert ok is False
    assert any(name == "error" for name, _ in events)


def test_still_at_start_waits_for_the_label_to_come_back():
    """1 回読んで駄目でも諦めない。

    ダイアログを閉じた直後にラベルが一瞬消えるのが一番起きやすい失敗で、
    1 回読みで通す形にすると**それがそのまま素通りする**。
    """
    assert _still_at_start(FakeReader(position=1656, blank_reads=1)) is False


def test_still_at_start_does_not_block_a_book_without_a_position_label():
    """位置が最後まで読めないときは通す。

    ここに来るのは rewind_to_start の最初の読みを通った本（＝ラベルを
    読める本）なので、この枝には本来来ない。来たときに撮影を止めるより
    通すほうが、この関数の役割（飛ばされたのを捕まえる）に照らして
    副作用が小さい。
    """
    assert _still_at_start(FakeReader(text="")) is True


def test_rewind_uses_the_reverse_key():
    """前進が left なら right で戻す。"""
    page = FakeReader(forward="ArrowLeft", position=3)
    rewind_to_start(page, "left", page_wait=0)
    assert set(page.presses) == {"ArrowRight"}


# ------------------------------------------------------------
# 撮影方式（ページ画像の要素 / ビューポート全体）
# ------------------------------------------------------------


def test_shoots_the_page_image_element_when_present():
    """要素があるならそれだけを撮る。ビューアの UI も余白も入らない。"""
    page = FakePage([b"a"], page_image=True)
    shot, mode = page_shot(page)

    assert mode == SHOT_ELEMENT
    assert page.shots == ["element"]
    assert shot == b"a"


def test_falls_back_to_the_viewport_when_the_element_is_missing():
    """画像レンダラでない本やレイアウト変更でも撮り続ける（無人実行のため）。"""
    page = FakePage([b"a"], page_image=False)
    shot, mode = page_shot(page)

    assert mode == SHOT_VIEWPORT
    assert page.shots == ["viewport"]
    assert shot == b"a"


def test_falls_back_when_the_element_screenshot_raises():
    """要素はあるが撮れない（サイズ 0・描画前）ときもフォールバックする。"""

    class Broken(FakePage):
        def locator(self, selector):
            loc = super().locator(selector)

            class Raising:
                def count(self):
                    return loc.count()

                @property
                def first(self):
                    return self

                def screenshot(self):
                    raise RuntimeError("element is not visible")

            return Raising()

    page = Broken([b"a"], page_image=True)
    shot, mode = page_shot(page)

    assert mode == SHOT_VIEWPORT
    assert shot == b"a"


def test_resolve_shot_mode_reports_which_path_will_be_used():
    assert resolve_shot_mode(FakePage([b"a"], page_image=True)) == SHOT_ELEMENT
    assert resolve_shot_mode(FakePage([b"a"], page_image=False)) == SHOT_VIEWPORT


def test_capture_pages_uses_the_element_path(tmp_path):
    page = FakePage([b"a", b"b", b"c"], page_image=True)
    total, reason = capture_pages(page, str(tmp_path), key="ArrowLeft", max_pages=3)

    assert (total, reason) == (3, "max_pages")
    assert set(page.shots) == {"element"}


def test_capture_pages_warns_when_the_shot_mode_changes(tmp_path):
    """途中で方式が変わるとページの寸法も変わる。どのページで変わったか残す。"""
    page = FakePage([b"a", b"b"], page_image=False)
    events = []
    capture_pages(
        page,
        str(tmp_path),
        key="ArrowLeft",
        max_pages=2,
        expect_mode=SHOT_ELEMENT,
        emit=lambda e, human=None, **f: events.append({"event": e, "human": human, **f}),
    )

    changed = [e for e in events if e.get("shot_mode") == SHOT_VIEWPORT]
    assert len(changed) == 2
    assert "001.png" in changed[0]["human"]


def test_manifest_records_the_shot_mode():
    """トリミングを飛ばすかの判断に使うので manifest に残す。"""
    manifest = build_manifest(
        title="本",
        profile_key="kindle_cloud",
        profile=None,
        total=3,
        save_dir="C:/out/本",
        stopped_reason="max_pages",
        started=datetime.datetime(2026, 1, 1, 0, 0, 0),
        finished=datetime.datetime(2026, 1, 1, 0, 0, 30),
        shot_mode=SHOT_ELEMENT,
    )
    assert manifest["shot_mode"] == SHOT_ELEMENT


# ------------------------------------------------------------
# Cloud Reader 非対応の本 (#42)
# ------------------------------------------------------------


class AlertPage:
    """url と、開いた本の上のダイアログだけを持つ page の代役。"""

    def __init__(self, url="https://read.amazon.co.jp/?asin=B0BVLM8RR2", alert=""):
        self.url = url
        self.alert = alert

    def evaluate(self, _script, _arg=None):
        return self.alert


class BrokenPage:
    """url もダイアログも取得できない page の代役。"""

    @property
    def url(self):
        raise RuntimeError("navigation in progress")

    def evaluate(self, _script, _arg=None):
        raise RuntimeError("execution context destroyed")


def test_unsupported_book_is_detected_by_its_dialog():
    """非対応の本はリーダーを作らず、このダイアログを出すだけで止まる（実測）。"""
    page = AlertPage(
        alert=(
            "Kindle App Is Required\n"
            "The book you\u2019re trying to read can only be opened using Kindle app.\n"
            "Back to Library"
        )
    )
    reason = unsupported_reason(page)
    assert reason is not None
    assert "Kindle App Is Required" in reason


def test_japanese_dialog_is_detected_too():
    """/manga/<ASIN> 経由では同じ状態が日本語で出る。表示言語に依存させない。"""
    page = AlertPage(
        alert=(
            "この本は現在読むことができません\n"
            "申し訳ありません。この本は現在、Kindle Cloud Reader でサポートされていません。"
        )
    )
    assert unsupported_reason(page) is not None


def test_library_url_alone_is_not_enough():
    """URL だけで永久の判定をしない。

    issue #42 の「/kindle-library へ戻される」は実測で再現しなかった。
    load_wait は固定待ちなので、読み込みが遅いだけの本がライブラリの URL の
    ままでいることはあり、それで切り捨てると取得できる本を落とす。
    """
    page = AlertPage(url="https://read.amazon.co.jp/kindle-library")
    assert unsupported_reason(page) is None


def test_environment_level_dialog_is_not_a_per_book_verdict():
    """本ではなく環境を指す文言で本を切り捨てない。

    「サポートされていません」だけで照合すると、ブラウザが弾かれたときに
    全冊が 1 冊ずつ「この本は非対応」として片付けられる。非対応は終了コードに
    出ないので、気づかないまま蔵書すべてを取りこぼすことになる。
    """
    page = AlertPage(alert="お使いのブラウザはサポートされていません")
    assert unsupported_reason(page) is None


def test_marker_is_found_when_several_dialogs_are_open():
    """先に別のダイアログが並んでいても本命を取り逃さない。

    alert_text は表示中のダイアログを全部つないで返す（閉じた残骸が先に
    並んでいると、最初の 1 つだけ見る実装では本命に届かない）。
    """
    page = AlertPage(alert="前回読んでいたページ\nKindle App Is Required")
    assert unsupported_reason(page) is not None


def test_alert_text_asks_only_for_visible_dialogs():
    """表示されていないダイアログを読まないことを、渡す JS で担保する。

    innerText は非表示の要素では textContent と同じになり、閉じた残骸まで
    読んでしまう。DOM が要るので実際の絞り込みはここでは検証できない。
    """
    sent = {}

    class ScriptCapturingPage:
        def evaluate(self, script, arg=None):
            sent["script"] = script
            return ""

    alert_text(ScriptCapturingPage())
    assert "querySelectorAll" in sent["script"]
    assert "display" in sent["script"]


def test_open_book_is_not_reported_as_unsupported():
    assert unsupported_reason(AlertPage()) is None


def test_unrelated_dialog_is_not_reported_as_unsupported():
    """読書位置の確認ダイアログ等で本を切り捨てない。"""
    page = AlertPage(alert="前回読んでいたページ\n498 に移動しますか?\nいいえ\nはい")
    assert unsupported_reason(page) is None


def test_page_failure_does_not_declare_the_book_unsupported():
    """判定できないだけで「取得手段が無い」と断定すると本を取りこぼす。"""
    assert unsupported_reason(BrokenPage()) is None


# ------------------------------------------------------------
# 余白の復元 (#60)
# ------------------------------------------------------------


def _png(size, color):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _size(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return im.size


class FakeShotPage:
    """page_shot 用の代役。要素の箱とビューポートを持つ。"""

    def __init__(self, box, view=(1600, 1200), color=(255, 255, 255), count=1):
        self._box = box
        self.viewport_size = {"width": view[0], "height": view[1]}
        self._color = color
        self._count = count
        self.bbox_timeouts: list = []
        page = self

        class Loc:
            def count(self):
                return page._count

            @property
            def first(self):
                return self

            def bounding_box(self, timeout=None):
                page.bbox_timeouts.append(timeout)
                x, y, w, h = page._box
                return {"x": x, "y": y, "width": w, "height": h}

            def screenshot(self):
                # Playwright は要素の実寸を画素に丸めた画像を返す
                return _png((round(page._box[2]), round(page._box[3])), page._color)

        self._loc = Loc()

    def locator(self, _selector):
        return self._loc

    def screenshot(self):
        return _png((self.viewport_size["width"], self.viewport_size["height"]), (9, 9, 9))


def test_edge_colors_use_the_page_ground():
    """白で決め打ちすると、色の付いた表紙に白い額縁が付く。"""
    from PIL import Image

    with Image.open(io.BytesIO(_png((40, 30), (230, 245, 235)))) as im:
        assert edge_colors(im) == ((230, 245, 235),) * 4


def test_edge_colors_differ_per_side():
    """見開きで左右の地色が違う本に、片側だけ異物の色の帯が付かないように。"""
    from PIL import Image

    im = Image.new("RGB", (40, 30), (255, 255, 255))
    im.paste(Image.new("RGB", (20, 30), (0, 0, 0)), (20, 0))  # 右半分だけ黒
    left, _top, right, _bottom = edge_colors(im)
    assert left == (255, 255, 255)
    assert right == (0, 0, 0)


def test_reader_padding_is_measured_not_assumed():
    """本によって余白は変わる。固定値にしない。"""
    page = FakeShotPage((160, 60, 1280, 1050))
    assert reader_padding(page, page.locator("x").first) == (160, 60, 1600, 1200)


def test_reader_padding_gives_up_when_the_element_overflows():
    """はみ出していると足すべき余白が決まらない。無理に足さない。"""
    page = FakeShotPage((-10, 0, 1700, 1200))
    assert reader_padding(page, page.locator("x").first) is None


def test_reader_padding_asks_for_a_short_timeout():
    """既定の 30 秒待ちだと、要素が一瞬 detach する本で 1 ページ 30 秒かかる。"""
    page = FakeShotPage((160, 60, 1280, 1050))
    reader_padding(page, page.locator("x").first)
    assert page.bbox_timeouts == [1000]


def test_padded_size_matches_the_viewport_even_with_fractions():
    """CSS 座標の端数で 1600 と 1599 が混ざると、size_mismatch も
    ダイジェストによる end_of_book 判定も壊れる。"""
    page = FakeShotPage((160.5, 60.4, 1279, 1050))
    data, _mode = page_shot(page)
    assert _size(data) == (1600, 1200)


def test_full_height_element_gets_only_side_margins():
    """マンガの見開きは高さいっぱいを使う。上下は足さない。"""
    page = FakeShotPage((160, 0, 1280, 1200))
    data, _mode = page_shot(page)
    assert _size(data) == (1600, 1200)


def test_page_shot_restores_the_reader_margins():
    """#60 の本体。縦書きで上下端に本文が接するのを防ぐ。"""
    page = FakeShotPage((160, 60, 1280, 1050))
    data, mode = page_shot(page)
    assert mode == "element"
    assert _size(data) == (1600, 1200)


def test_page_shot_without_padding_is_unchanged():
    data = _png((100, 80), (255, 255, 255))
    assert pad_shot(data, None) is data
    assert pad_shot(data, (0, 0, 0, 0)) is data


def test_page_shot_falls_back_to_the_viewport():
    """要素が無い本は従来どおりビューポート全体を撮る（余白は足さない）。"""
    page = FakeShotPage((160, 60, 1280, 1050), count=0)
    data, mode = page_shot(page)
    assert mode == "viewport"
    assert _size(data) == (1600, 1200)


# ------------------------------------------------------------
# 停止理由から終了コードへの対応
# ------------------------------------------------------------


def _run_with_stop_reason(reason, tmp_path, monkeypatch, *, pages=3):
    """run_headless_capture を通し、停止理由に対する終了コードを見る。

    ここを間違えると batch が出力を見てスキップし、途中までの本が
    そのまま確定する (#76)。detect / rewind は本筋でないので飛ばす。
    """
    import contextlib

    from core import headless_browser
    from core import headless_capture as hc

    page = FakePage([b"a", b"b"])

    @contextlib.contextmanager
    def fake_open_reader(*a, **kw):
        yield page

    monkeypatch.setattr(headless_browser, "open_reader", fake_open_reader)
    monkeypatch.setattr(hc, "capture_pages", lambda *a, **kw: (pages, reason))
    from core.capture_profiles import get_profile

    return hc.run_headless_capture(
        get_profile("kindle_cloud"),
        "t",
        str(tmp_path),
        asin="B0TEST",
        page_turn="left",
        no_rewind=True,
    )


def test_a_reader_error_does_not_exit_zero(tmp_path, monkeypatch):
    """リーダーが落ちた本を完成扱いにしない。

    0 で返すと batch は出力があるものとしてスキップし、途中までの本が
    そのまま確定する。合本 4 冊がこれで 439〜672 ページのまま ok になった。
    """
    from core.pipeline import EXIT_ERROR

    code = _run_with_stop_reason("reader_error", tmp_path, monkeypatch)
    # 8 (EXIT_UNSUPPORTED_BOOK) だとバッチ集計で「非対応」に紛れ、失敗に出ない
    assert code == EXIT_ERROR


def test_a_book_that_opens_into_an_error_says_so(tmp_path, monkeypatch):
    """開いた時点で落ちている本は、そう報告する。

    ここで見ないと位置ラベルが読めないまま detect_turn_key まで進み、
    「ページ送りの向きを判定できません（--page-turn で明示してください）」に
    なる。**原因も対処も間違った案内**で、ログの原因別集計も汚れる。
    #76 の 4 冊を撮り直すとまさにこの状態で開く。
    """
    import contextlib

    from core import headless_browser
    from core import headless_capture as hc
    from core.capture_profiles import get_profile
    from core.pipeline import EXIT_ERROR

    page = FakePage([b"a", b"b"], alert="申し訳ありません。問題が発生しました")
    events = []

    @contextlib.contextmanager
    def fake_open_reader(*a, **kw):
        yield page

    monkeypatch.setattr(headless_browser, "open_reader", fake_open_reader)
    monkeypatch.setattr(
        hc, "capture_pages", lambda *a, **kw: pytest.fail("撮影まで進んではいけない")
    )
    code = hc.run_headless_capture(
        get_profile("kindle_cloud"),
        "t",
        str(tmp_path),
        asin="B0TEST",
        page_turn="left",
        no_rewind=True,
        emit=lambda name, **kw: events.append((name, kw)),
    )
    assert code == EXIT_ERROR
    assert [n for n, _ in events if n == "reader_error"] != []


def test_a_real_end_of_book_still_exits_zero(tmp_path, monkeypatch):
    """最終ページまで撮れた本はこれまでどおり成功。"""
    code = _run_with_stop_reason("end_of_book", tmp_path, monkeypatch)
    assert code == 0
