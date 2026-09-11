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
    REWIND_REREAD_BUDGET,
    SHOT_ELEMENT,
    SHOT_VIEWPORT,
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
        self.total = total
        self.reads = 0
        # wait_for_timeout に渡されたミリ秒。刻んで見ているかの確認に使う (#57)
        self.waits: list[int] = []
        self.url = "https://read.amazon.co.jp/?asin=B0X"
        self.presses: list[str] = []

        page = self

        class Keyboard:
            def press(self, key):
                page.presses.append(key)
                moves = key == page.forward or page.position > page.min_position
                # 動けない押下（先頭で戻ろうとする等）は「飲まれた 1 回」を
                # 消費しない。実測ではそうなっている（#53 のログ）
                if page.swallow > 0 and moves:
                    page.swallow -= 1
                    return
                if key == page.forward:
                    page.position += 1
                elif page.position > page.min_position:
                    page.position -= 1

        self.keyboard = Keyboard()

    def locator(self, _selector):
        page = self

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
                return f"{page.position}/{page.total}ページ \u2002●\u2002 1%"

        return Loc()

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate(self, _js):
        """dismiss_dialogs の代役。閉じた拍子に位置が飛ぶ本を再現する。"""
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


def test_waiting_for_a_turn_does_not_read_before_the_page_settles():
    """1 回目を早く読まない。遷移中の値を拾うと向きを逆に判定する (#53)。

    押した直後のラベルが一過性に before より小さい値を返すと、
    detect_turn_key は「押したら減った」＝逆向き、と判定する。逆向きのまま
    撮ると正常終了して逆順の部分本が完成扱いになる。実機で裏が取れている
    のは 0.6 秒までなので、それより早くは読まない。
    """
    page = FakeReader(forward="ArrowLeft", position=100)
    _wait_for_position_change(page, 100, page_wait=2.5)
    # 1 回目の待ちが 250ms なら遷移中を読みうる
    assert page.waits[0] == 600


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


def test_rewind_stops_when_it_keeps_having_to_reread():
    """読めたり読めなかったりを繰り返す本で、いつまでも読み直さない。

    読み直しは 1 押下あたり最大 5 秒かかる。読めた回で帳消しになると累積の
    上限が無くなり、押すたびにちらつく本では 1 冊で 90 分を超えうる。
    バッチは 1 冊あたりのタイムアウトを持っていない。
    """
    # 本ループの読み（偶数回目）だけが空。読み直し（奇数回目）では読める
    page = FakeReader(forward="ArrowLeft", position=500, blank_at=set(range(2, 400, 2)))
    ok, presses = rewind_to_start(page, "left", page_wait=0, max_retries=3)
    assert ok is False
    # 500 回押し切らずに打ち切る
    assert presses <= REWIND_REREAD_BUDGET + 1


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
