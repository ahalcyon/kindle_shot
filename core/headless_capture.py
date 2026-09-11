"""headless ブラウザで Kindle Cloud Reader のページを取得する

画面を撮る core/capture_engine.py と違い、ブラウザプロセス内でレンダリングした
結果を page.screenshot() で得る。そのため:

- **画面もデスクトップセッションも不要**。ディスプレイオフ・画面ロック・
  リモート接続の切断中でも動く（ImageGrab 方式はいずれでも破綻する）
- **OS やブラウザの通知が写り込まない**。ビューポートしか撮らないため、
  通知が余白検出を壊す問題 (#7) が構造的に起きない
- **ビューアの UI を CSS で消せる**。トリミングで推測して削る必要が減る
- **ダイアログを DOM で確実に閉じられる**

出力は core/capture_runner.run_capture と同じ形にしてある
（<output_folder>/<title>/ に 001.png... と manifest.json）ので、
後段の trim / convert はそのまま使える。
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import re

from PIL import Image

from core.pipeline import (
    EXIT_BAD_ARGS,
    EXIT_ERROR,
    EXIT_NO_IMAGES,
    EXIT_OK,
    EXIT_UNSUPPORTED_BOOK,
    MANIFEST_NAME,
    SHOT_ELEMENT,
    SHOT_VIEWPORT,
    clear_output_images,
    emit_error,
    null_emit,
)
from core.safe_names import book_path_name

BOOK_URL = "https://read.amazon.co.jp/?asin={asin}"
SIGNIN_MARKER = "/ap/signin"

# Cloud Reader が対応していない本の目印 (#42)。蔵書 405 冊のうち 63 冊が該当した。
#
# 非対応の本は ?asin=<ASIN> を開いてもエラーにならず、リーダー (#kr-renderer) を
# 作らないまま ion-alert を出すだけで止まる（実測 2026-09-07）:
#
#     Kindle App Is Required
#     The book you're trying to read can only be opened using Kindle app.
#
# ロケールが ja-JP でもこの文言は英語で出た。/manga/<ASIN> で開くと日本語になるので
# 表示言語に依存しないよう両方の文言を見る。
#
# 検出しないと本文の無い画面を撮り続け、ページ送りの向きの判定で初めて詰まって
# 「--page-turn で明示してください」と誤った案内を出すことになる。
#
# **文全体で照合する。** 「サポートされていません」「読むことができません」だけを
# 見ると、本ではなく環境を指すダイアログ（例:「お使いのブラウザはサポートされて
# いません」）に一致してしまう。それは全冊に同じように起きる種類の障害なのに、
# 1 冊ずつ「この本は非対応」として片付けられ、しかも非対応は終了コードに出ない
# ので、405 冊すべてを黙って取りこぼす。
UNSUPPORTED_MARKERS = (
    "kindle app is required",
    "can only be opened using kindle app",
    "cloud reader でサポートされていません",
    "この本は現在読むことができません",
)

# 非対応の本が出すダイアログ。ion-alert の id は連番で変わるのでクラスで拾う。
ALERT_SELECTOR = "ion-alert, .alert-wrapper"

# 報告に載せるダイアログ文言の上限。未知のダイアログでログを埋めないため
REASON_MAX_CHARS = 200

# 本文ページのレンダリング結果。Kindle Cloud Reader は 1 ページを
# サーバ側でレンダリングした画像 1 枚（blob: URL）として配信しており、
# DOM に本文テキストは無い（実測: canvas 0 / iframe 0 / テキストノード 0 文字）。
# この要素だけを撮ればビューアの UI も余白も最初から入らない。
# blob を fetch すると TypeError: Failed to fetch になるので要素撮影を使う。
PAGE_IMAGE_SELECTOR = ".kg-full-page-img img"

# 撮影前に隠すビューアの UI。実機の DOM から採取した。
# 左右のシェブロンは幅 160px ずつあり、隠さないと本文の左右に大きく食い込む。
UI_SELECTORS = (
    ".top-chrome",
    "#top-menu-bar",
    "ion-footer",
    "#kr-scrubber-bar",
    ".footer-label-color-default",
    ".kr-chevron-container-left",
    ".kr-chevron-container-right",
    # .top-chrome を隠しても書名とブックマークが残るため個別に指定する
    ".top-chrome__book-title",
    "ion-title",
    ".top-chrome__button",
)

# 位置同期や告知のモーダル。出るとページ送りを吸い込むため閉じる。
# pyautogui のキー名 (capture_profiles.PAGE_TURN_KEYS) を Playwright のキー名へ。
# 注: 縦書き（右→左）の本では "right" は**前のページ**に戻る。
# 表紙で右を押しても何も起きないため、最終ページと誤判定する。
# 縦書きの本を先頭から撮るときは "left" を使うこと。
PLAYWRIGHT_KEYS = {
    "right": "ArrowRight",
    "left": "ArrowLeft",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "down": "ArrowDown",
    "up": "ArrowUp",
}
DEFAULT_TURN_KEY = "left"
AUTO_TURN_KEY = "auto"
DEFAULT_PAGE_WAIT = 2.5
DEFAULT_LOAD_WAIT = 12
# 巻き戻しは描画を待つ必要が無いので短くする
DEFAULT_REWIND_WAIT = 0.6
DEFAULT_MAX_REWIND = 1000
# 先頭に着いたとみなせる位置の上限。実測 342 冊のバッチで巻き戻しが成功した
# 227 冊は、位置 1 (73 冊) / 2 (153 冊) / 3 (1 冊) で終わっている。見開きの本が
# 2 で止まるぶんの余裕を見て 10 にする (#69)。
# **この 227 冊は「短い巻き戻し」の標本である**（押した回数の中央値 3）。
# この上限が防ぎたいのは長い巻き戻しのほうで、そちらの実測は 18 冊しかない。
# 位置 11 以上で正当に始まる本があれば、その本は撮れなくなる（--no-rewind は
# 「止まった位置から撮る」なので逃げ道にならない）。次のバッチで
# rewound の reason=stopped_short の件数と位置を必ず集計すること。
MAX_START_POSITION = 10
# 巻き戻し中に位置ラベルを読み落としたときの、1 押下あたりの読み直し回数
REWIND_REREAD_ATTEMPTS = 3
# 読み直しの回数で打ち切る仕組みは入れていない。一度入れたが、**進んでいる
# 本を殺す**ことが分かったので外した。押すたびにラベルの描画が遅れる本は、
# 読み直しては正しく戻っているのに回数だけが積み上がる。上限を「進んだら
# 0 に戻す」形にすると、今度は stuck (3 回) が先に効くので到達しない。
# 時間の上限は max_rewind (1000 押下) が持つ。最悪は 1 押下あたり
# 0.6 + 5 + 1.5 = 7.1 秒 x 1000 = 約 2 時間で、1 冊としては長いが有限。
# そこまで掛かる本が実在したら rewound の presses に出るので、数字を見てから決める。

# 読書位置の表示。実測で 2 形式ある:
#     "6/339ページ ● 1%"   (ページ表示)
#     "位置1/3495 ● 0%"    (位置表示)
# どちらも先頭の数値が前進で増えるので、そこだけ読めば向きを判定できる。
# スクラバー (#kr-scrubber-bar) の値は縦書きだと逆行するため使わない。
POSITION_SELECTOR = ".footer-label.position"
_POSITION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def read_position_pair(page):
    """現在の読書位置と総量を (位置, 総量) で返す。読めなければ (None, None)。

    総量は「先頭に戻り切れたか」の判断材料になる。位置だけを記録していたため、
    位置 3344 で止まった本が先頭付近なのか終盤なのか、ログから判断できなかった
    (#69)。
    """
    try:
        locator = page.locator(POSITION_SELECTOR)
        if not locator.count():
            return None, None
        # text_content を使う。撮影前に UI を CSS で隠すため、
        # inner_text だと描画されていない要素から空文字が返ることがある
        matched = _POSITION_RE.search(locator.first.text_content() or "")
    except Exception:
        return None, None
    if not matched:
        return None, None
    return int(matched.group(1)), int(matched.group(2))


def read_position(page):
    """現在の読書位置を数値で返す。読めなければ None。"""
    return read_position_pair(page)[0]


def _settled_position_pair(page, *, page_wait=DEFAULT_PAGE_WAIT, attempts=4):
    """読書位置が読めるまで数回待って (位置, 総量) を返す。読めなければ (None, None)。

    読み込み直後はラベルの描画が間に合わず None になることがある。
    1 回で諦めると「動いていない」と誤判定して向きの判定に失敗する。
    """
    for i in range(attempts):
        position, total = read_position_pair(page)
        if position is not None:
            return position, total
        if i < attempts - 1:
            page.wait_for_timeout(int(page_wait * 1000))
    return None, None


def _settled_position(page, *, page_wait=DEFAULT_PAGE_WAIT, attempts=4):
    """読書位置が読めるまで数回待って返す。読めなければ None。"""
    return _settled_position_pair(page, page_wait=page_wait, attempts=attempts)[0]


# 1 つの向きにつき押す回数。ダイアログを閉じた直後の 1 回目は飲まれることが
# あり（実測）、1 回で「その向きではない」と決めると判定不能になる (#53)。
TURN_PROBE_PRESSES = 3

# 押した後に位置の変化を待つ回数。変化しないことの確認にも使う。
TURN_WAIT_ATTEMPTS = 2

# 最初の 1 回を読むまでに待つ時間。**ここを短くしてはいけない。**
# 押した直後のラベルは遷移中の値を返すことがあり、それが before より小さいと
# `detect_turn_key` は向きを逆に判定する。逆向きのまま撮ると正常終了して
# 逆順の部分本が完成扱いになる (#53)。実機で裏が取れているのは
# rewind_to_start が使っている 0.6 秒（DEFAULT_REWIND_WAIT と同じ値だが、
# あちらを速くしてもここは動かさない）までなので、それより早くは読まない。
# ただし page_wait x attempts がこれより短いときはそちらが優先される
# （--page-wait 0.2 なら 0.4 秒で読む）。下限は設けていない。
TURN_SETTLE_WAIT = 0.6
# 落ち着いたあとに見に行く間隔
TURN_POLL_INTERVAL = 0.25


def _wait_for_position_change(page, before, *, page_wait, attempts=TURN_WAIT_ATTEMPTS):
    """読書位置が before から変わるまで待つ。変わらなければ None。

    「読めるまで待つ」(_settled_position) では足りない。押した直後は前の値が
    そのまま読めるので、1 回読んで同じなら動いていない、と決めると誤判定する。

    **上限は変えずに、落ち着いてから刻んで見る。** 固定スリープだと実機が
    0.6 秒で反映しても page_wait（既定 2.5 秒）を必ず払っていた (#57)。

    速くなるのは**動く向きの検出だけ**で、1 押下あたり 2.5 秒が 0.6 秒になる。
    動かない向きは上限（page_wait x attempts）まで粘るので今までと同じ時間が
    かかる。#57 が挙げている「先頭ページの横書き本が left で 15 秒を捨てる」は
    この変更では消えない。消すには候補の順を決め打ちにしているところ
    （detect_turn_key の ("left", "right")）を変える必要がある。
    """
    budget = page_wait * attempts
    waited = 0.0
    # 1 回目は遷移が落ち着くまで待つ。2 回目以降だけ刻む
    interval = min(TURN_SETTLE_WAIT, budget)
    while True:
        page.wait_for_timeout(int(interval * 1000))
        waited += interval
        now = read_position(page)
        if now is not None and now != before:
            return now
        # budget が 0 のとき（テスト）でも 1 回は読んでから抜ける
        if waited >= budget:
            return None
        interval = min(TURN_POLL_INTERVAL, budget - waited)


def _probe_turn(page, candidate, *, page_wait):
    """candidate を数回押して (押す直前の位置, 動いた後の位置) を返す。動かなければ None。

    **基準は押す直前に読み直す。** 呼び出し側が持っている古い値と比べてはいけない。
    前の候補の押下が遅れて効いた場合や、dismiss_dialogs が位置を動かした場合、
    古い基準と比べると向きを逆に判定しうる。逆向きのまま巻き戻すと
    rewind_to_start の停滞判定に引っかかって「先頭に戻した」と成功扱いになり、
    逆走した部分本が完成扱いになる。この関数が防ぐべき事故そのもの (#53)。
    """
    for _ in range(TURN_PROBE_PRESSES):
        # サインイン直後や Whispersync の「最後に読んでいたページへ移動しますか」が
        # キーを吸うため、押す前に毎回閉じる。閉じた拍子に位置が動くこともあるので、
        # 基準はその後に読む
        dismiss_dialogs(page)
        before = read_position(page)
        if before is None:
            continue
        page.keyboard.press(turn_key(candidate))
        moved = _wait_for_position_change(page, before, page_wait=page_wait)
        if moved is not None:
            return before, moved
    return None


def _restore_position(page, candidate, target, *, descending, page_wait):
    """判定で動かした分を戻す。

    descending が True なら押すたびに位置が下がる。「target に到達」だけを
    停止条件にすると、位置表示が古い値を返したときに 1 回余計に押して
    通り越す。--no-rewind ではそれがそのままキャプチャ開始位置になり、
    先頭数ページが黙って欠ける (#53)。跨いだら止める。
    """
    # 押したのと逆のキー。どちらのキーで位置が上がるかは本によって違うので、
    # 「left なら下がる」のような決め打ちにしない
    back = turn_key(reverse_of(candidate))
    for _ in range(TURN_PROBE_PRESSES + 1):
        now = _settled_position(page, page_wait=page_wait)
        if now is None:
            return
        if (now <= target) if descending else (now >= target):
            return
        dismiss_dialogs(page)
        page.keyboard.press(back)
        page.wait_for_timeout(int(page_wait * 1000))


def detect_turn_key(page, *, page_wait=DEFAULT_PAGE_WAIT, emit=null_emit):
    """前進するページ送りキーを実測で判定する。判定できなければ None。

    縦書き（右→左）の本では right が前のページに戻る。数百冊には縦書きと
    横書きが混ざるため、決め打ちだと「正常終了したのに中身が逆順」の本が
    紛れ込む。読書位置の数値が増える方を前進とみなす。

    「動かなかった」は「その向きではない」の証拠にならない (#53)。実測では、
    ダイアログを閉じた直後の 1 回目のキー入力が飲まれる。先頭ページでは
    後ろ方向が定義上動けないので、もう一方が 1 回飲まれただけで 2 回の試行が
    両方無情報になり、判定不能が確定していた。向きごとに数回押して確かめる。

    判定のために動かした分は元に戻す。戻さないと表紙を落とすため。
    （キャプチャ自体が本を読み進めるので、読書位置は結局末尾まで動く。
    ここで戻すのは読書位置の保全のためではない。）
    """
    # 位置を読む前に閉じる。開いたままの値を基準にすると、閉じた拍子に
    # 位置が動いたときに基準がずれる
    dismiss_dialogs(page)
    start = _settled_position(page, page_wait=page_wait)
    if start is None:
        emit("status", human="読書位置を読めないため送りキーを判定できません")
        return None

    for candidate in ("left", "right"):
        probed = _probe_turn(page, candidate, page_wait=page_wait)
        if probed is None:
            continue
        before, after = probed
        forward = candidate if after > before else reverse_of(candidate)
        # 戻すのは「自分が押して動かした分」だけ。判定を始めた位置 (start) を
        # 目標にすると、ダイアログが位置を飛ばした場合にキーでは到達できない
        _restore_position(page, candidate, before, descending=after > before, page_wait=page_wait)
        emit(
            "page_turn_detected",
            human=f"ページ送りキーを判定しました: {forward}",
            page_turn=forward,
        )
        return forward
    emit(
        "status",
        human=f"ページ送りキーを判定できませんでした（読書位置 {start} から動きません）",
        position=start,
    )
    return None


def reverse_of(name):
    """逆方向のキー名。"""
    return {"left": "right", "right": "left", "pagedown": "pageup", "pageup": "pagedown"}.get(
        name, "right"
    )


def hide_ui_css():
    """ビューアの UI を隠す CSS を返す。"""
    return ", ".join(UI_SELECTORS) + " { display: none !important; }"


def book_url(asin):
    return BOOK_URL.format(asin=asin)


def is_signed_in(url):
    """URL がサインインページへ飛ばされていないか。"""
    return SIGNIN_MARKER not in (url or "")


def digest(data):
    """スクリーンショットの同一判定に使うダイジェスト。"""
    return hashlib.sha256(data).hexdigest()


def build_manifest(
    *,
    title,
    profile_key,
    profile,
    total,
    save_dir,
    stopped_reason,
    started,
    finished,
    page_turn=None,
    page_turn_source=None,
    page_wait=None,
    shot_mode=None,
    shot_padding=None,
    shot_mode_changed=None,
):
    """run_capture が書くものと同じ形の manifest を組み立てる。"""
    return {
        "tool": "kindle_shot",
        "title": title,
        "profile_key": profile_key,
        "profile": profile.to_dict() if profile is not None else {},
        "backend": "headless",
        # 実行に使った値。profile の既定と違うことがあるので別に残す
        "page_turn": page_turn,
        "page_turn_source": page_turn_source,
        "page_wait": page_wait,
        # element: ページ画像の要素だけを撮った（UI も余白も入らない）
        # viewport: 要素が見つからずビューポート全体を撮った（要トリミング）
        "shot_mode": shot_mode,
        # 要素の外側に戻した余白 (左, 上, ビューポート幅, 高さ)。#60 より前に
        # 取った本は null になるので、撮り直すべき本をあとから洗い出せる
        "shot_padding": list(shot_padding) if shot_padding else None,
        # 途中で撮影方式が変わったページ番号。element の本で viewport に落ちると
        # ヘッダーの写ったページが紛れ込むが、寸法が同じなので validate では
        # 拾えない。ここだけが手がかりになる
        "shot_mode_changed": shot_mode_changed or [],
        "total_pages": total,
        "save_dir": save_dir,
        "stopped_reason": stopped_reason,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_seconds": round((finished - started).total_seconds(), 1),
    }


def turn_key(name):
    """プロファイルのページ送りキーを Playwright のキー名にする。"""
    return PLAYWRIGHT_KEYS.get((name or DEFAULT_TURN_KEY).lower(), "ArrowLeft")


# 「前回読んでいたページ」の位置同期モーダル。全画面のバックドロップを伴い、
# 出ている間はクリックもキー入力も一切通らない（実測）。
# 先頭から撮りたいので「いいえ」を選んで現在位置に留まる。
DISMISS_ALERTS_JS = """() => {
  let closed = 0;
  document.querySelectorAll('ion-alert').forEach(alert => {
    if (getComputedStyle(alert).display === 'none') return;
    const buttons = Array.from(alert.querySelectorAll('button'));
    const keep = buttons.find(b => /いいえ|No|キャンセル|Cancel/.test(b.innerText));
    const target = keep || buttons[0];
    if (target) { target.click(); closed++; }
  });
  return closed;
}"""


def dismiss_dialogs(page):
    """開いているダイアログを閉じる。閉じた数を返す。"""
    try:
        closed = page.evaluate(DISMISS_ALERTS_JS)
    except Exception:
        return 0
    if closed:
        page.wait_for_timeout(1500)
    return closed


# 縁の色を数えるときの間引き幅。1 画素ずつ見なくても地色は分かる
_EDGE_STEP = 7

# bounding_box の待ち時間。既定 (30 秒) のままだと、ページ送り直後に要素が
# 一瞬 detach する本で 1 ページあたり 30 秒待つ。数百ページ x 342 冊で効く
BBOX_TIMEOUT_MS = 1000


def _strip_color(strip):
    """細長い帯の最頻色。"""
    colors = strip.convert("RGB").getcolors(maxcolors=strip.width * strip.height)
    return max(colors)[1] if colors else (255, 255, 255)


def edge_colors(image):
    """四辺それぞれの地色 (左, 上, 右, 下)。

    1 色で埋めると、見開きで左右の地色が違う本（片側が白・片側が黒ベタ）や、
    裁ち落としの写真で、地色でない色の帯が付く。辺ごとに分けるとどちらも自然になる。
    """
    w, h = image.size
    return (
        _strip_color(image.crop((0, 0, 1, h))),
        _strip_color(image.crop((0, 0, w, 1))),
        _strip_color(image.crop((w - 1, 0, w, h))),
        _strip_color(image.crop((0, h - 1, w, h))),
    )


def reader_padding(page, locator):
    """リーダーがページ画像の外側に置いている余白 (左, 上, ビューポートの幅, 高さ)。

    要素だけを撮ると、この余白が落ちる。縦書きの本ではページ画像そのものに
    上下の余白が無く、画面で見えている上下の余白はここなので、落とすと本文が
    上下端に接する（#60。197 ページ中 168 ページで発生していた）。

    固定値にはしない。マンガの見開きのように要素がビューポートの高さいっぱいを
    使う本では上下の余白が 0 になるなど、本によって変わる。

    右下の余白は返さない。CSS 座標に端数があると切り捨てで 1px ずれ、同じ本の
    中で 1600 と 1599 が混ざって validate の size_mismatch が出るうえ、
    同一ページ判定のダイジェストも変わって end_of_book を取り逃す。
    実際の画像の寸法から引き算して決めるほうが確実（pad_shot 側で行う）。
    """
    try:
        box = locator.bounding_box(timeout=BBOX_TIMEOUT_MS)
        view = page.viewport_size
    except Exception:  # noqa: BLE001 - 測れないなら余白なしで撮る
        return None
    if not box or not view:
        return None
    left = round(box["x"])
    top = round(box["y"])
    if left < 0 or top < 0 or box["width"] > view["width"] or box["height"] > view["height"]:
        # 要素がビューポートからはみ出している。足すべき余白が決まらない
        return None
    return left, top, view["width"], view["height"]


def pad_shot(data, padding):
    """スクリーンショットの外側を、ページの地色で埋める。

    仕上がりは必ずビューポートと同じ寸法にする。左上の余白だけを CSS 座標から
    取り、右下は実際の画像の寸法から引いて決めるので、端数や device_scale_factor
    に左右されない。
    """
    if not padding:
        return data
    left, top, view_w, view_h = padding
    with Image.open(io.BytesIO(data)) as image:
        right = view_w - image.width - left
        bottom = view_h - image.height - top
        if min(left, top, right, bottom) < 0 or not any((left, top, right, bottom)):
            return data
        cl, ct, cr, cb = edge_colors(image)
        canvas = Image.new("RGB", (view_w, view_h), ct)
        if left:
            canvas.paste(Image.new("RGB", (left, view_h), cl), (0, 0))
        if right:
            canvas.paste(Image.new("RGB", (right, view_h), cr), (view_w - right, 0))
        if top:
            canvas.paste(Image.new("RGB", (image.width, top), ct), (left, 0))
        if bottom:
            canvas.paste(Image.new("RGB", (image.width, bottom), cb), (left, view_h - bottom))
        canvas.paste(image.convert("RGB"), (left, top))
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        canvas.close()
        return buffer.getvalue()


def page_shot(page, *, selector=PAGE_IMAGE_SELECTOR):
    """ページ画像の要素だけを撮り、(バイト列, 撮影方式) を返す。

    要素が見つからない・撮れない本（画像レンダラでない本、レイアウト変更）は
    従来どおりビューポート全体にフォールバックする。無人実行なので、撮れなく
    なった瞬間に落とすより、方式を記録して撮り続けるほうが被害が小さい。

    撮ったあと、リーダーが要素の外側に置いている余白と同じ幅で外周を埋める。
    ビューポートを広く撮ってから切り出す方法は使わない。hide_ui_css で
    消しきれていないヘッダー（書名）が写り込むことを実測で確認している (#60)。
    """
    element = None
    shot = None
    try:
        locator = page.locator(selector)
        if locator.count():
            element = locator.first
            shot = element.screenshot()
    except Exception:  # noqa: BLE001 - 撮れない理由は問わずフォールバックする
        shot = None
    if shot is None or element is None:
        return page.screenshot(), SHOT_VIEWPORT
    # 余白を足せなくても、撮れた本文は捨てない。ここで viewport に落ちると
    # ヘッダーの写ったページが紛れ込むうえ、寸法が同じなので validate も
    # 気づけない (#60)
    with contextlib.suppress(Exception):
        shot = pad_shot(shot, reader_padding(page, element))
    return shot, SHOT_ELEMENT


def alert_text(page, *, selector=ALERT_SELECTOR):
    """開いた本の上に**表示されている**ダイアログの文言を返す。無ければ空文字。

    本文そのものではなくダイアログに限って読む。本文は画像で配信されていて
    DOM にテキストが無いが、将来テキストレンダラの本が出てきたときに本文の
    言い回しを非対応の目印と取り違えないようにするため。

    閉じたあとも DOM に残るダイアログがあるので display:none は除く
    (DISMISS_ALERTS_JS が同じ理由で同じ判定をしている)。innerText は
    表示されていない要素では textContent と同じになり、残骸まで読んでしまう。
    最初の 1 つだけでなく全部を見る。残骸が先に並んでいると本命を取り逃す。

    入れ子になった一致 (.alert-wrapper は ion-alert の子孫) は外側だけ残す。
    両方読むと同じ文言が二重に入る。
    """
    try:
        return page.evaluate(
            "(sel) => {"
            "  const all = Array.from(document.querySelectorAll(sel))"
            "      .filter(a => getComputedStyle(a).display !== 'none');"
            "  return all.filter(a => !all.some(b => b !== a && b.contains(a)))"
            "      .map(a => a.innerText).join('\\n');"
            "}",
            selector,
        )
    except Exception:  # noqa: BLE001 - 読めないなら判定材料にしない
        return ""


def unsupported_reason(page, *, markers=UNSUPPORTED_MARKERS):
    """Cloud Reader 非対応の本なら理由を返す。開けているなら None。

    ライブラリのカードの DOM には対応/非対応の違いが無く、**開いてみるまで
    分からない**（実測）。判定材料は開いたあとの画面だけになる。

    判定できないときは None を返して先へ進める。無人で数百冊を回すので、
    読み込みが遅いだけの本を「取得手段が無い」と誤って切り捨てるより、
    従来どおり撮影を試みて失敗するほうが被害が小さい。非対応と判定された本は
    終了コードにも出ず「books.json から外してください」と案内されるので、
    誤判定は 1 冊を黙って永久に失うことになる。

    issue #42 には「?asin= を開くと /kindle-library へ戻される」とも書いたが、
    非対応の本 2 冊を 9 秒間観測しても再現しなかったので判定に使わない。
    load_wait は固定待ちなので、読み込みが遅いだけの本がライブラリの URL の
    ままでいることはあり、それを永久の判定に使うと取得できる本を落とす。
    """
    text = alert_text(page)
    lowered = text.lower()
    if any(m in lowered for m in markers):
        # 未知のダイアログが出たときにログを埋めないよう長さを抑える
        return " ".join(text.split())[:REASON_MAX_CHARS]
    return None


def resolve_shot_mode(page, *, selector=PAGE_IMAGE_SELECTOR):
    """この本を要素撮影で撮れるかを 1 回だけ判定する。"""
    try:
        return SHOT_ELEMENT if page.locator(selector).count() else SHOT_VIEWPORT
    except Exception:  # noqa: BLE001 - 判定できないならフォールバック側に倒す
        return SHOT_VIEWPORT


def capture_pages(
    page,
    save_dir,
    *,
    key="ArrowLeft",
    max_pages=None,
    page_wait=DEFAULT_PAGE_WAIT,
    max_retries=3,
    expect_mode=None,
    emit=null_emit,
):
    """ページを順に撮る。(枚数, stopped_reason) を返す。

    **直前のページとだけ**比べる。全履歴と比べると、本文中に何度も現れる
    白紙・章扉・見開き調整の余白ページを「送れていない」と誤認し、
    そのページを落としたり途中で打ち切ったりする（core/capture_engine.py の
    _wait_stable_page も直前ページとだけ比べている）。

    stopped_reason:
        max_pages       上限に達した
        end_of_book     送っても変わらなくなった（最終ページ到達とみなす）
        no_change       1 ページも進めなかった（送りキーの向き違い・モーダル等）
        signin_required 途中でセッションが切れた

    expect_mode を渡すと、途中で撮影方式が変わったページを警告し、そのページ
    番号を返り値に含める。

    **validate の size_mismatch では拾えない。** 要素撮影の余白を戻して以降
    (#60)、element も viewport も同じビューポート寸法になったため、寸法では
    区別が付かない。途中で viewport に落ちるとリーダーのヘッダーが写ったページが
    紛れ込むが、要素撮影の本はトリミングも無効化されているので後段でも削れない。
    気づく手段はここだけなので、manifest に残す。
    """
    prev = None
    total = 0
    while True:
        # 途中でセッションが切れると、サインイン画面を本文として保存してしまう
        if not is_signed_in(page.url):
            emit("signin_required", human="キャプチャ中にセッションが切れました")
            return total, "signin_required"

        shot, mode = page_shot(page)
        current = digest(shot)

        if prev is not None and current == prev:
            retried = 0
            while retried < max_retries and current == prev:
                retried += 1
                emit(
                    "status",
                    human=f"ページ変化なし、めくり再送 ({retried}/{max_retries})",
                    message=f"ページ変化なし、めくり再送 ({retried}/{max_retries})",
                )
                # 途中で出たモーダルはキー入力を吸うので閉じてから押し直す
                dismiss_dialogs(page)
                page.keyboard.press(key)
                page.wait_for_timeout(int(page_wait * 1000))
                shot, mode = page_shot(page)
                current = digest(shot)
            if current == prev:
                # 1 枚も進めていないなら最終ページではなく送りに失敗している
                return total, "end_of_book" if total > 1 else "no_change"

        total += 1
        filename = f"{total:03d}.png"
        if expect_mode is not None and mode != expect_mode:
            # status ではなく専用のイベントにする。ログを grep するだけで
            # 「ヘッダーが写ったページが紛れ込んだ本」を見つけられるように
            emit(
                "shot_mode_changed",
                human=f"{filename}: 撮影方式が {expect_mode} から {mode} に変わりました",
                page=total,
                shot_mode=mode,
            )
        with open(os.path.join(save_dir, filename), "wb") as f:
            f.write(shot)
        emit("page", human=f"Page {total}: {filename}", page=total, file=filename)

        if max_pages and total >= max_pages:
            return total, "max_pages"

        prev = current
        page.keyboard.press(key)
        page.wait_for_timeout(int(page_wait * 1000))


def rewind_to_start(
    page,
    forward,
    *,
    page_wait=DEFAULT_REWIND_WAIT,
    max_rewind=DEFAULT_MAX_REWIND,
    max_retries=3,
    emit=null_emit,
):
    """先頭ページまで戻す。(ok, 押した回数) を返す。

    read.amazon.co.jp/?asin=... は**前回の読書位置で開く**（実測: 位置 27 で
    開いた）。位置同期モーダルを「いいえ」で閉じても現在位置に留まるため、
    巻き戻さないと読みかけの本が途中から末尾までだけ撮れ、しかも
    end_of_book で正常終了してしまう。batch は出力があるとスキップするので、
    半分だけの本がそのまま確定する。

    画面キャプチャ経路では open_book が画素比較で巻き戻しているが、
    ここでは読書位置の数値が使えるので確実に判定できる。

    先頭かどうかは「位置が 1 になった」ではなく**「押しても下がらなくなった」**で
    判定する。見開き表示の本は位置が 1 まで下がらず 2 で止まるため、数値だけを
    見るとマンガが軒並み失敗する（実測で 10 冊中 8 冊が該当した）。
    """
    back = turn_key(reverse_of(forward))
    before, total = read_position_pair(page)
    if before is None:
        # ここでも rewound を出す。出さないと、巻き戻しの結果を rewound で
        # 追う運用（tests/e2e/test_amazon_e2e.py もそうしている）から
        # この経路だけが落ちる
        emit(
            "rewound",
            human="読書位置を読めないため巻き戻せません",
            ok=False,
            presses=0,
            position=None,
            total=None,
            reason="no_position",
        )
        return False, 0

    pressed = 0
    stuck = 0
    unreadable = 0
    at_start = before <= 1
    while pressed < max_rewind and not at_start:
        page.keyboard.press(back)
        page.wait_for_timeout(int(page_wait * 1000))
        pressed += 1
        current, seen_total = read_position_pair(page)
        if current is None:
            # ラベルの描画が間に合っていないだけかもしれないので読み直す。
            # **「読めなかった」を「動かなかった」と同じに数えてはいけない。**
            # 長い巻き戻しの途中で 3 回続けて読み落とすと、そこが先頭という
            # ことにされる。実測で 2 冊がこれに当たり、冒頭 60 ページが落ちた
            # 本が ok で完成した (#69)。
            # 読み直しは巻き戻しの刻み (0.6 秒) ではなく通常の待ち時間で待つ。
            # 実測では合本の巻の境目でラベルが数秒消える。0.6 秒の刻みでは
            # 復帰を待ちきれず、読めないまま打ち切っていた (#69)
            # ダイアログが被っていると位置ラベルも読めない。巻き戻しの
            # ループは今まで一度も閉じていなかった。実測で、横書きの本が
            # 位置 148/336 で「読めない」まま打ち切られた (#69)
            closed = dismiss_dialogs(page)
            current, seen_total = _settled_position_pair(
                page,
                page_wait=max(page_wait, DEFAULT_PAGE_WAIT),
                attempts=REWIND_REREAD_ATTEMPTS,
            )
            if closed and current is not None:
                # **閉じた拍子に位置が飛ぶ本がある**（Whispersync の
                # 「最後に読んでいたページへ移動しますか」に「はい」を押した形）。
                # before はそれまでの最小値であって今の位置ではないので、
                # 飛んだあとにこれと比べると 3 回で stuck が立ち、
                # 「位置 3 で先頭に着いた」と報告しながら実際は 1654 にいる、
                # という事故になる（この PR が防ごうとしているものそのもの）。
                # 飛んだら基準を取り直す
                before = current
                stuck = 0
                if seen_total is not None:
                    total = seen_total
                at_start = before <= 1
                continue
        if current is None:
            unreadable += 1
            if unreadable >= max_retries:
                # どこにいるか分からない。先頭だと決めつけない
                break
            continue
        unreadable = 0
        if seen_total is not None:
            total = seen_total
        if current >= before:
            stuck += 1
        else:
            stuck = 0
            before = current
        if before <= 1 or stuck >= max_retries:
            # 位置が 1 になったか、押しても下がらなくなったら先頭。
            # 見開き表示の本は位置が 1 まで下がらず 2 で止まるため、
            # 数値だけで判定すると「戻り切れなかった」と誤判定する（実測）。
            at_start = True
        if pressed % 25 == 0:
            emit("status", human=f"先頭へ巻き戻し中... (位置 {before})")

    # 「押しても下がらない」だけでは先頭の証拠にならない。実測 227 冊の
    # 巻き戻しはすべて位置 1 / 2 / 3 で終わっており、4 以上は 1 冊も無い。
    # 位置 1658 や 3344 で止まったものは、動かなくなっただけで先頭ではない (#69)。
    ok = at_start and before <= MAX_START_POSITION
    where = f"位置 {before}" + (f"/{total}" if total is not None else "")
    if ok:
        human = f"先頭へ巻き戻しました（{pressed} 回、{where}）"
        reason = "ok"
    elif unreadable >= max_retries:
        human = f"読書位置を読めなくなりました（{pressed} 回、最後に読めたのは {where}）"
        reason = "unreadable"
    elif at_start:
        human = (
            f"{where} で動かなくなりましたが、先頭ではありません"
            f"（先頭とみなす上限は {MAX_START_POSITION}）"
        )
        reason = "stopped_short"
    else:
        human = f"先頭まで戻り切れませんでした（{pressed} 回、{where}）"
        reason = "not_reached"
    emit(
        "rewound",
        human=human,
        ok=ok,
        presses=pressed,
        position=before,
        total=total,
        reason=reason,
    )
    return ok, pressed


def _still_at_start(page, *, emit=null_emit):
    """巻き戻した後もまだ先頭にいるか。読めなければ先頭とみなす。

    ダイアログを閉じた拍子に位置が飛ぶ本があるため、巻き戻しの成功だけでは
    撮り始めてよいことにならない (#69)。位置が読めない場合にここで止めると、
    位置ラベルを持たない本が撮れなくなる。巻き戻し本体が既に
    「読めないなら失敗」を見ているので、ここは分かったときだけ止める。
    """
    position, total = read_position_pair(page)
    if position is None or position <= MAX_START_POSITION:
        return True
    where = f"位置 {position}" + (f"/{total}" if total is not None else "")
    emit_error(
        emit,
        f"巻き戻したあとにダイアログで {where} まで飛ばされました。"
        "途中から撮ると本の一部だけが完成扱いになるため中止します",
    )
    return False


def run_headless_capture(
    profile,
    title,
    output_folder,
    *,
    asin=None,
    url=None,
    profile_key=None,
    max_pages=None,
    page_wait=None,
    page_turn=None,
    overwrite=False,
    load_wait=None,
    no_rewind=False,
    max_rewind=DEFAULT_MAX_REWIND,
    profile_dir=None,
    headless=True,
    emit=null_emit,
):
    """headless ブラウザで本を開き、ページを取得する。

    Returns:
        終了コード
    """
    from core.headless_browser import open_reader

    if not asin and not url:
        emit_error(emit, "asin か url のどちらかが必要です")
        return EXIT_BAD_ARGS

    # run_book は指定が無ければ None を渡してくる（画面キャプチャ側は
    # プロファイルの値へ落とす作りのため）。ここで既定へ寄せる。
    page_wait = DEFAULT_PAGE_WAIT if page_wait is None else page_wait
    load_wait = DEFAULT_LOAD_WAIT if load_wait is None else load_wait

    # タイトルには Windows のファイル名に使えない文字が入る (#52)。
    # 直接呼ばれることもあるので、呼び出し側任せにせずここでも通す。
    out = os.path.abspath(output_folder)
    save_dir = os.path.join(out, book_path_name(title, out))
    # 前回の残骸が混ざると後段の PDF に古いページが紛れる（README の契約）
    code = clear_output_images(
        save_dir,
        overwrite,
        emit,
        label="保存先",
        reason="前回の残骸が混ざるのを防ぐため中止しました。",
    )
    if code is not None:
        return code
    os.makedirs(save_dir, exist_ok=True)

    started = datetime.datetime.now()
    target = url or book_url(asin)
    requested = page_turn or AUTO_TURN_KEY
    # with ブロック内で確定するが、そこへ到達する前に中断された場合の
    # manifest 用に初期化しておく
    forward = DEFAULT_TURN_KEY
    turn_source = "default"
    key = turn_key(forward)
    total = 0
    stopped_reason = "error"
    shot_mode = None
    shot_padding = None
    shot_mode_changed: list = []

    def note(event, human=None, **fields):
        """撮影方式が途中で変わったページを控えつつ、そのまま emit する。"""
        if event == "shot_mode_changed" and "page" in fields:
            shot_mode_changed.append(fields["page"])
        emit(event, human=human, **fields)

    def write_manifest():
        """途中終了でもどこまで撮れたか分かるよう必ず書く。"""
        manifest = build_manifest(
            title=title,
            profile_key=profile_key,
            profile=profile,
            total=total,
            save_dir=save_dir,
            stopped_reason=stopped_reason,
            started=started,
            finished=datetime.datetime.now(),
            page_turn=key,
            page_turn_source=turn_source,
            page_wait=page_wait,
            shot_mode=shot_mode,
            shot_padding=shot_padding,
            shot_mode_changed=shot_mode_changed,
        )
        path = os.path.join(save_dir, MANIFEST_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return path

    try:
        with open_reader(target, profile_dir=profile_dir, headless=headless, emit=emit) as page:
            if page is None:
                return EXIT_ERROR
            page.wait_for_timeout(int(load_wait * 1000))

            # 非対応の本はここで打ち切る。先へ進めても本文の無い画面を撮り、
            # ページ送りの向きの判定で詰まって的外れな案内を出すだけになる。
            # dismiss_dialogs より前に見る（目印のダイアログを閉じてしまうため）。
            reason = unsupported_reason(page)
            if reason is not None:
                emit(
                    "book_unsupported",
                    human="この本は Kindle Cloud Reader が対応していないため開けません",
                    asin=asin,
                    reason=reason,
                )
                emit_error(
                    emit,
                    "この本は Kindle Cloud Reader が対応していないため開けません"
                    f"（{reason}）。再試行しても結果は変わりません",
                )
                stopped_reason = "unsupported_book"
                write_manifest()
                return EXIT_UNSUPPORTED_BOOK

            dismiss_dialogs(page)
            page.add_style_tag(content=hide_ui_css())
            emit("status", human="ビューアの UI を隠しました", message="ビューアの UI を隠しました")

            # ページ画像の要素が撮れるなら UI も余白も最初から入らない。
            # 撮れない本のために従来のビューポート撮影も残してある。
            shot_mode = resolve_shot_mode(page)
            if shot_mode == SHOT_ELEMENT:
                # #60 より前に取った本と見分けるために残す
                shot_padding = reader_padding(page, page.locator(PAGE_IMAGE_SELECTOR).first)
            emit(
                "shot_mode",
                human=(
                    "ページ画像の要素を撮ります（UI・余白なし）"
                    if shot_mode == SHOT_ELEMENT
                    else "ページ画像の要素が見つかりません。ビューポート全体を撮ります"
                ),
                shot_mode=shot_mode,
                selector=PAGE_IMAGE_SELECTOR,
            )

            if requested == AUTO_TURN_KEY:
                detected = detect_turn_key(page, page_wait=page_wait, emit=emit)
                if detected is None:
                    # 判定できないまま決め打ちで進めると、向きが逆でも
                    # 正常終了して逆順の本が完成扱いになる（無人では致命的）
                    emit_error(
                        emit,
                        "ページ送りの向きを判定できませんでした。"
                        "--page-turn left / right で明示してください",
                    )
                    stopped_reason = "turn_key_undetected"
                    return EXIT_ERROR
                forward = detected
                turn_source = "detected"
            else:
                forward = requested
                turn_source = "explicit"
            key = turn_key(forward)
            emit("status", human=f"ページ送りキー: {key}", message=f"ページ送りキー: {key}")

            if not no_rewind:
                rewound, _ = rewind_to_start(page, forward, max_rewind=max_rewind, emit=emit)
                if not rewound:
                    emit_error(
                        emit,
                        "先頭ページまで戻せませんでした。途中から撮ると本の一部だけが"
                        "完成扱いになるため中止します（--no-rewind で無視できます）",
                    )
                    stopped_reason = "rewind_failed"
                    return EXIT_ERROR
                # ここで閉じるダイアログも位置を飛ばしうる（Whispersync の
                # 「最後に読んでいたページへ移動しますか」）。巻き戻しが
                # 成功したあとに飛ばされると、そのまま途中から撮り始める。
                # 閉じたときだけ位置を見直す
                if dismiss_dialogs(page) and not _still_at_start(page, emit=emit):
                    stopped_reason = "rewind_failed"
                    return EXIT_ERROR
            total, stopped_reason = capture_pages(
                page,
                save_dir,
                key=key,
                max_pages=max_pages,
                page_wait=page_wait,
                expect_mode=shot_mode,
                emit=note,
            )
    except KeyboardInterrupt:
        stopped_reason = "user"
        write_manifest()
        raise

    manifest_path = write_manifest()

    if total == 0:
        emit_error(emit, "1 ページも取得できませんでした")
        return EXIT_NO_IMAGES
    if stopped_reason == "no_change":
        emit_error(
            emit,
            f"{total} ページで進まなくなりました。ページ送りキー ({key}) の向きが逆か、"
            "モーダルが出ている可能性があります（--page-turn で切り替えられます）",
        )
        return EXIT_ERROR
    if stopped_reason == "signin_required":
        emit_error(emit, f"{total} ページでセッションが切れたため中断しました")
        return EXIT_ERROR

    emit(
        "result",
        human=f"キャプチャ完了: {total} ページ\n保存先: {save_dir}",
        ok=True,
        total_pages=total,
        save_dir=save_dir,
        stopped_reason=stopped_reason,
        manifest=manifest_path,
    )
    return EXIT_OK
