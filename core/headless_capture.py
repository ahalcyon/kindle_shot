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

# リーダー自身が落ちたときのダイアログ。**「最終ページ」と区別するために要る。**
# 静止した画面を撮り続けるとダイジェストが一致し、`end_of_book` として正常終了して
# しまう（#76。合本 4 冊が 439〜672 ページでそうなった）。batch は出力があると
# スキップするので、途中までの本が確定する。
#
# **停滞中の画面がエラー画面だったかは未確認。** 観測したのは「撮り終えた直後に
# 開き直すとこのダイアログが出ていて、ページ画像の要素も消えていた」こと。
# 停滞中の画面は保存されない（変化したページしか保存しない）ので後から見られない。
# だから文言だけに頼らず、ページ画像の消失と停止位置も併せて残している。
#
# 非対応 (UNSUPPORTED_MARKERS) とは別に持つ。あちらは「この本は開けない」で
# 終了コード 8、こちらは「開けたが途中で落ちた」で中断。意味も対処も違う。
READER_ERROR_MARKERS = (
    "問題が発生しました",
    "something went wrong",
    "ライブラリからこの本をもう一度",
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
# 時間の上限は max_rewind (1000 押下) が持つ。ラベルを読み落とす本の最悪は
# 1 押下あたり 0.6 + 5 + 1.5 = 7.1 秒 x 1000 = 約 2 時間。
# **位置が飛び続ける本はこれより長い。** 飛びを検出した押下では
# _stable_position_pair (最悪 7.5 秒) を 2 回と dismiss_dialogs (1.5 秒) と
# キーの生存確認が乗るので、1 押下あたり約 17 秒 x 1000 = 約 4.7 時間になる (#72)。
# どちらも 1 冊としては長いが有限。そこまで掛かる本が実在したら rewound の
# presses と jumps に出るので、数字を見てから決める。

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


def _stable_position_pair(page, *, page_wait=DEFAULT_PAGE_WAIT, attempts=3):
    """**同じ値を 2 回続けて**読めるまで待って (位置, 総量) を返す。

    `_settled_position_pair` は「読めるまで待つ」であって「落ち着くまで待つ」では
    ない。読めた時点で待ち時間ゼロで返るので、遷移中の一過性の値を掴んだ直後に
    呼んでも**同じ一過性の値がそのまま返る**。一過性かどうかの判定には使えない。

    押した直後に一瞬だけ別の値を返す本がある (#53)。巻き戻しで「先頭から
    遠ざかった」を判定するとき、1 回の上振れで基準を取り直すと、**先頭にいる本が
    失敗になる**（実測の再現: 先頭 位置 2 の本が一過性ラベル 1000 を拾って
    ok=false reason=stopped_short）。

    落ち着かなければ最後に読めた値を返す。判断材料が無いよりはましで、
    呼び出し側は None でなければ従来どおり扱える。
    """
    last, last_total = _settled_position_pair(page, page_wait=page_wait)
    for _ in range(attempts):
        page.wait_for_timeout(int(page_wait * 1000))
        current, current_total = _settled_position_pair(page, page_wait=page_wait)
        if current is not None and current == last:
            return current, current_total if current_total is not None else last_total
        last, last_total = current, current_total
    return last, last_total


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
    rewind=None,
    stopped_at=None,
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
        # 巻き戻しの結果（位置・総量・理由）。**事後に部分本を洗うにはこれが要る。**
        # 標準出力にしか無いと、ログを捨てた時点で追えなくなる (#72)
        "rewind": rewind or {},
        # 撮影が止まったときの位置。最終ページまで行った本は position が
        # book_total の近くにあり、途中で止まった本は大きく手前にある (#76)
        "stopped_at": stopped_at or {},
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
# 押すとリーダーから出てしまうボタン。これしか無いダイアログは閉じない (#76)。
#
# **文言ではなくボタンで決める。** 文言で決めると
# 「通信に問題が発生しました。再試行してください」のような再試行ボタン付きの
# 一時的なダイアログまで閉じなくなり、これまで復帰できていた本が失敗する。
# 「出る」ボタンしか無いダイアログだけが、閉じてはいけないもの。
LEAVE_READER_BUTTONS = ("ライブラリに戻る", "back to library")

LEAVE_BUTTON_JS = """(labels) => {
  return Array.from(document.querySelectorAll('ion-alert'))
    .filter(a => getComputedStyle(a).display !== 'none')
    .some(a => {
      const btns = Array.from(a.querySelectorAll('button'));
      return btns.length > 0 && btns.every(
        b => labels.some(l => (b.innerText || '').toLowerCase().includes(l)));
    });
}"""


def has_leave_reader_button(page, *, labels=LEAVE_READER_BUTTONS):
    """開いているダイアログのボタンが「リーダーから出る」ものだけか。

    そういうダイアログは閉じようとしてはいけない。どのボタンを押しても
    リーダーから出てしまう (#76)。
    """
    try:
        return bool(page.evaluate(LEAVE_BUTTON_JS, [label.lower() for label in labels]))
    except Exception:  # noqa: BLE001 - 読めないなら普通のダイアログとして扱う
        return False


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
    """開いているダイアログを閉じる。閉じた数を返す。

    **「リーダーから出る」ボタンしか無いダイアログには触らない** (#76)。
    残しておけば capture_pages が読んで中断できる。
    """
    if has_leave_reader_button(page):
        return 0
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


def reader_error_text(page, *, markers=READER_ERROR_MARKERS):
    """リーダーが落ちていればその文言を返す。落ちていなければ空文字。

    「ページが変わらなくなった」の**理由**を確かめるために使う。理由を見ずに
    最終ページとみなしていたのが #76。
    """
    text = alert_text(page)
    lowered = text.lower()
    if not any(m in lowered for m in markers):
        return ""
    # unsupported_reason と同じ正規化。実機の文言は改行を含むので、
    # そのまま人間向けに出すと 1 行のはずの報告が 3 行に割れる
    return " ".join(text.split())[:REASON_MAX_CHARS]


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


# 画像が変わらなくなったとき、**読み手側の位置がまだ終わりに達していなければ**
# 粘る回数と待ち時間 (#79)。
#
# 実測で、`end_of_book` は本の途中でも出る。合本 4 冊の撮り直しでは
# 位置 1237/2999 (41%) と 14541/58503 (25%) で「最終ページ」と判定された。
# ところが**その本は壊れていない**。あとから同じ位置を開いて送ると普通に進み、
# 止まった地点の前後 16 ページはすべて別画像だった。つまり一過性の停滞で、
# 3 回の素早い再送では足りなかっただけ。
#
# 位置が総量に達していれば粘らない。本当の最終ページで毎回 30 秒待つと
# 342 冊で 3 時間増える。実測では最後まで撮れた本が position == book_total
# ちょうどで終わっている。
STALL_PATIENCE = 3
STALL_WAIT = 10.0


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
        reader_error    リーダーが落ちた。最終ページではないので完成扱いにしない
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
    patience = 0
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
            if current == prev and patience < STALL_PATIENCE:
                # **読み手側の位置がまだ終わりに達していないなら、もう少し粘る。**
                # 一過性の停滞を最終ページと呼ばないため (#79)
                position, book_total = read_position_pair(page)
                if (
                    position is not None
                    and book_total is not None
                    and position < book_total
                    and not reader_error_text(page)
                ):
                    patience += 1
                    emit(
                        "capture_stalled",
                        human=(
                            f"{total} ページで画像が変わらなくなりましたが、"
                            f"位置 {position}/{book_total} は終わりではありません。"
                            f"待って撮り直します（{patience}/{STALL_PATIENCE}）"
                        ),
                        page=total,
                        position=position,
                        book_total=book_total,
                    )
                    page.wait_for_timeout(int(STALL_WAIT * 1000))
                    shot, mode = page_shot(page)
                    current = digest(shot)

            if current == prev:
                # **「変わらない」の理由を確かめてから最終ページと呼ぶ。**
                # リーダーが落ちるとページ画像が消え、静止した画面を撮り続けるので
                # ダイジェストは当然一致する (#76)
                trouble = reader_error_text(page)
                if not trouble and expect_mode == SHOT_ELEMENT and mode != SHOT_ELEMENT:
                    # 文言が変わっていてもこれで拾える。最終ページに達しただけなら
                    # ページ画像の要素は残っている
                    trouble = "ページ画像の要素が消えました"
                reason = (
                    "reader_error" if trouble else ("end_of_book" if total > 1 else "no_change")
                )
                # **止まった位置を必ず残す。** これが唯一、あとから機械的に
                # 「途中で切れた本」を洗える手がかりになる。最終ページまで
                # 行った本は position が total の近くにあり、途中で止まった本は
                # 大きく手前にある。上の 2 つの検出はどちらも取り逃しうる
                # （viewport 撮影の本でダイアログが読めない場合など）ので、
                # 検出できなかったぶんはログから拾い直すしかない
                position, book_total = read_position_pair(page)
                emit(
                    "capture_stopped",
                    human=f"{total} ページで停止しました（{reason}、位置 {position}/{book_total}）",
                    page=total,
                    reason=reason,
                    position=position,
                    book_total=book_total,
                    message=trouble or "",
                )
                if trouble:
                    emit_error(
                        emit,
                        f"{total} ページでリーダーが応答しなくなりました: {trouble}",
                    )
                # 1 枚も進めていないなら最終ページではなく送りに失敗している
                return total, reason

        total += 1
        # 1 ページ進めたら粘りを戻す。上限は「立て続けに立ち直れなかった回数」で、
        # 本ぜんたいの回数ではない。長い本ほど一過性の停滞に当たりやすい
        patience = 0
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


# 目次パネル。合本はここからでないと巻の先頭を越えられない (#70)。
#
# aria-label はロケール決め打ちにしない。同じファイルの UNSUPPORTED_MARKERS に
# 「ロケールが ja-JP でもこの文言は英語で出た」という実測が残っている。
TOC_BUTTON_SELECTOR = 'ion-button[aria-label="目次"], ion-button[aria-label="Table of Contents"]'
TOC_ITEM_SELECTOR = "ion-menu.side-menu ion-item"
# 目次が無い本で 30 秒 x 342 冊を待たないための上限
TOC_CLICK_TIMEOUT_MS = 5000
# 目次項目が描画されるまでの待ち。実機の合本は 220 項目あるので、固定待ちで
# 1 回だけ数えると重い本を「目次なし」と取り違える
TOC_ITEM_TIMEOUT_MS = 8000
# キーの生存確認で押す回数。閉じた直後の 1 回目は飲まれる（実測）
KEY_PROBE_ATTEMPTS = 3
TOC_OPEN_JS = """(selector) => {
  const b = document.querySelector(selector);
  if (!b) return false;
  b.click();
  return true;
}"""


def _close_toc(page, *, page_wait=DEFAULT_REWIND_WAIT):
    """目次パネルを閉じ、キー入力を受け取れる状態に戻す。

    **順序が効く。** Escape で閉じてから描画領域をクリックする。
    """
    page.keyboard.press("Escape")
    page.wait_for_timeout(int(page_wait * 1000))
    size = page.viewport_size
    if size is None:
        # 寸法を推測して外れると、実ビューポートの外を叩いて何も起きない。
        # キーが死んだままかどうかは _keys_respond が確かめるので、
        # ここで当て推量しない
        return
    page.mouse.click(size["width"] // 2, size["height"] // 2)
    page.wait_for_timeout(int(page_wait * 1000))


def jump_to_start_via_toc(page, *, page_wait=DEFAULT_REWIND_WAIT, emit=null_emit):
    """目次の先頭項目へ飛ぶ。飛べたら True。

    合本（全 7 巻など）はページ送りキーで巻の境界を越えられず、キーだけでは
    本の先頭に戻せない（#70。位置 3216 付近で下がらなくなる）。目次からなら
    巻を越えて一度で飛べる（実測: 位置 2594 → 1）。

    **飛んだあとキー入力が効かなくなる。** 実測で 8 回押しても位置が動かず、
    一方「次のページ」ボタンは効いた（1→2→3→4→5）のでリーダー自体は生きている。
    パネルを Escape で閉じ、さらに**描画領域を実際にクリック**すると戻る。
    JS の focus() では戻らなかった（activeElement は #kr-renderer になるのに
    キーは死んだまま）。閉じずに返すと、後続のページ送りが全部飲まれて
    「動かなくなった」と誤判定される（#72 と同じ形）。

    **開いたら必ず閉じる。** 途中で失敗した出口から閉じずに抜けると、
    まさにその誤判定が起きる。分岐ごとに手で閉じると必ずどれかが抜けるので
    finally に寄せてある。
    """
    opened = False
    try:
        # **JS でクリックする。** この時点で hide_ui_css が効いていて、目次ボタンは
        # .top-chrome__button として display:none になっている。Playwright の
        # click() は要素が操作可能になるのを待つので、既定では 30 秒待って
        # 落ちるだけだった（実測。巻き戻しがキーだけで走り #70 が直らなかった）
        if not page.evaluate(TOC_OPEN_JS, TOC_BUTTON_SELECTOR):
            return False
        opened = True
        # 項目が出るまで待つ。出なければ目次を持たない本として諦める
        page.wait_for_selector(TOC_ITEM_SELECTOR, timeout=TOC_ITEM_TIMEOUT_MS)
        # 項目のほうは実クリック。パネルは UI_SELECTORS に入っていないので見えている
        page.locator(TOC_ITEM_SELECTOR).first.click(timeout=TOC_CLICK_TIMEOUT_MS)
        page.wait_for_timeout(int(max(page_wait, DEFAULT_PAGE_WAIT) * 1000))
    except Exception as exc:  # 目次が無い・構造が変わった等。キーでの巻き戻しに委ねる
        emit("toc_jump_failed", human=f"目次から先頭へ飛べませんでした: {exc}"[:REASON_MAX_CHARS])
        return False
    finally:
        if opened:
            _close_toc(page, page_wait=page_wait)
    return True


def _keys_respond(page, forward, *, page_wait):
    """ページ送りキーが効いているか。効けば True、効かなければ False。

    判断できなければ None（位置が読めない本）。確かめたら位置は元へ戻す。

    目次から飛んだあとに要る。飛んだ先が位置 2〜10 に着地し、かつパネルを
    閉じてもキーが戻っていなかった場合、巻き戻しのループは 3 回押して
    動かないので `stuck` から **at_start = True** とみなす。着地点は
    MAX_START_POSITION の枠内なので `ok` になり、**冒頭数ページが欠けた本が
    完成扱いで確定する**（batch は出力があるとスキップする）。

    この変更以前は、同じ故障が位置 1200 のような遠い場所で起きて必ず
    大声で落ちていた。目次ジャンプは着地点を枠の内側に持ち込むので、
    ここで「キーが生きている」ことを別途証明しないと、loud failure が
    silent partial book に化ける。

    「一度も下がらなかったら失敗」という判定は使えない。見開き表示の本は
    位置 2 で止まるので、それだと軒並み落ちる。前進させて確かめるのが、
    見開き本と死んだキーを区別できる唯一の形。

    **1 回で決めない。** パネルを閉じた直後の 1 回目は飲まれる。実測:

        jumped=True pos=1
          press1: 1   ← 飲まれた
          press2: 2
          press3: 3

    ダイアログを閉じた直後に入力が飲まれるのは #53 で既に分かっていた挙動で、
    目次パネルでも同じだった。1 回で決めると、生きているキーを死んだと誤判定して
    **全部の本が巻き戻せなくなる**（実機で踏んだ）。
    """
    before = read_position(page)
    if before is None:
        return None
    wait = max(page_wait, DEFAULT_PAGE_WAIT)
    forward_presses = 0
    moved = None
    for _ in range(KEY_PROBE_ATTEMPTS):
        page.keyboard.press(turn_key(forward))
        forward_presses += 1
        moved = _wait_for_position_change(page, before, page_wait=wait)
        if moved is not None:
            break
    if moved is None:
        return False
    # **押した回数だけ戻す。位置の数値が戻ったかでは判断しない。**
    # 位置はページより粗く、表紙と扉が同じ「位置 1」になる本がある。
    # 数値が戻ったことを条件にすると、見た目は戻ったのに 1 ページ進んだままになり、
    # **表紙が落ちる**。実機で踏んだ: 探りを入れた回の 1 ページ目が、入れなかった回の
    # 2 ページ目とバイト単位で一致した。
    # 先頭で余分に戻しても何も起きないので、飲まれる 1 回ぶん多めに押す。
    back = turn_key(reverse_of(forward))
    for _ in range(forward_presses + 1):
        page.keyboard.press(back)
        page.wait_for_timeout(int(wait * 1000))
    return True


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

    # まず目次から飛ぶ。合本はキーでは巻の境界を越えられない (#70)。
    # 普通の本でも、数百回のキー送り（再撮影の実測で中央値 178 回）が 1 回で済む。
    # 飛べたあとも下のループは回す。目次の先頭項目が本当の先頭より後ろにある本
    # （表紙や前付けを目次に持たない本）では、残りをキーで詰める必要がある。
    via_toc = False
    if before > MAX_START_POSITION and jump_to_start_via_toc(page, page_wait=page_wait, emit=emit):
        via_toc = True
        jumped, jumped_total = _settled_position_pair(
            page, page_wait=max(page_wait, DEFAULT_PAGE_WAIT)
        )
        # 読めなくても必ず出す。出さないと、目次を通った本とキーだけの本を
        # ログから切り分けられない（#69 以降、原因別の集計で運用している）
        emit(
            "toc_jump",
            human=f"目次から先頭へ飛びました: 位置 {before} → {jumped}",
            before=before,
            after=jumped,
        )
        # 読めたら必ず採用する。「下がったときだけ」にすると、飛んで動いたのに
        # before だけ古い値のまま残り、実際の位置と食い違う。飛んだ先が後ろでも、
        # 下のループが読み直して詰めるので嘘を持ち回るより良い
        if jumped is not None:
            before = jumped
            if jumped_total is not None:
                total = jumped_total

        # **キーが生きていることを証明してからループに入る。** 詳細は
        # _keys_respond。証明せずに進むと、着地点が MAX_START_POSITION の枠内
        # だったときに「押しても下がらない」＝先頭、と読み違えて部分本が
        # 完成扱いになる
        if _keys_respond(page, forward, page_wait=page_wait) is False:
            where = f"位置 {before}" + (f"/{total}" if total is not None else "")
            emit(
                "rewound",
                human=(
                    f"目次から飛んだあとページ送りが効きません（{where}）。"
                    "先頭に見えても途中の可能性があるため中止します"
                ),
                ok=False,
                presses=0,
                position=before,
                total=total,
                reason="keys_dead",
                via_toc=True,
            )
            return False, 0

    pressed = 0
    stuck = 0
    jumps = 0
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
            if closed and current is not None and current > before:
                # **閉じた拍子に位置が飛ぶ本がある**（Whispersync の
                # 「最後に読んでいたページへ移動しますか」に「はい」を押した形）。
                # before はそれまでの最小値であって今の位置ではないので、
                # 飛んだあとにこれと比べると 3 回で stuck が立ち、
                # 「位置 3 で先頭に着いた」と報告しながら実際は 1654 にいる、
                # という事故になる（この PR が防ごうとしているものそのもの）。
                # 飛んだら基準を取り直す。
                # **条件は「閉じた」ではなく「先頭から遠ざかった」。** 閉じただけで
                # 取り直すと stuck = 0 と continue でこの周回の停滞判定が消える。
                # 先頭かどうかは「押しても下がらなくなった」でしか判定できない
                # （見開きの本は位置 2 で止まる）ので、ラベルが読めない回に
                # ダイアログが閉じられ続ける見開き本は、先頭にいるのに
                # 永久に at_start にならない
                before = current
                stuck = 0
                # 読めた周回なので、読めなかった回数も戻す
                unreadable = 0
                if seen_total is not None:
                    total = seen_total
                # 取り直した先が先頭ということはない (current > before >= 2)
                if pressed % 25 == 0:
                    emit("status", human=f"先頭へ巻き戻し中... (位置 {before})")
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
        if current > before:
            # **先頭から遠ざかった。** 「押しても下がらない」とは別の事象。
            # ここを stuck に数えて before を据え置くと、3 回で「先頭に着いた」と
            # 結論しながら実際は遠くにいる、という**嘘の成功**になる
            # （実測の再現で ok=true position=5 と報告して実際は 1654）。
            # before はそれまでの最小値であって、今いる場所ではない (#72)。
            #
            # 一過性のラベルを拾っただけかもしれないので、落ち着いてから読み直す。
            # 巻き戻しの刻み (0.6 秒) は遷移中の値を掴みやすい (#53)。
            # **_settled_position_pair では落ち着かない**（読めた時点で即返る）ので
            # 同じ値を 2 回続けて読めるまで待つ。1 回の上振れで基準を取り直すと、
            # 先頭にいる本が失敗になる
            settled, settled_total = _stable_position_pair(
                page, page_wait=max(page_wait, DEFAULT_PAGE_WAIT)
            )
            if settled is not None:
                current = settled
                if settled_total is not None:
                    total = settled_total
            if current > before:
                # 本当に遠ざかっている。**入力を飲むダイアログが出ていても
                # 位置ラベルは読める**（text_content で読むので CSS で隠れていても
                # 返る）。実測: 目次パネルを閉じた直後、位置ラベルは 1 と読めるのに
                # 1 回目の押下だけ飲まれた。だから「読めない周回」だけで
                # ダイアログを閉じていたのでは間に合わない
                dismiss_dialogs(page)
                moved, moved_total = _stable_position_pair(
                    page, page_wait=max(page_wait, DEFAULT_PAGE_WAIT)
                )
                if moved is not None:
                    current = moved
                    if moved_total is not None:
                        total = moved_total
                emit(
                    "rewind_jumped",
                    human=f"巻き戻し中に位置が飛びました: {before} → {current}",
                    before=before,
                    after=current,
                )
                # 今いる場所を基準にする。嘘の位置を報告しない
                before = current
                stuck = 0
                jumps += 1
                # **飛び先が先頭付近でも黙って完成させない。** 飛んだあと入力が
                # 飲まれたままだと、押しても動かないので stuck が 3 たまり、
                # 飛び先が MAX_START_POSITION の枠内なら ok になる。
                # 目次ジャンプで踏んだ穴と同じ故障なので、同じ門番を置く
                if _keys_respond(page, forward, page_wait=page_wait) is False:
                    where = f"位置 {before}" + (f"/{total}" if total is not None else "")
                    emit(
                        "rewound",
                        human=(
                            f"巻き戻し中に位置が飛び、以後ページ送りが効きません（{where}）。"
                            "先頭に見えても途中の可能性があるため中止します"
                        ),
                        ok=False,
                        presses=pressed,
                        position=before,
                        total=total,
                        reason="keys_dead",
                        via_toc=via_toc,
                    )
                    return False, pressed
                continue
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
        # 目次を通った本とキーだけの本を、ログから切り分けるための主キー
        via_toc=via_toc,
        # 巻き戻し中に位置が飛んだ回数。立て直したあとの position からは
        # 飛んだ形跡が消えるので、これが唯一の目印になる (#72)
        jumps=jumps,
    )
    return ok, pressed


def _still_at_start(page, *, emit=null_emit):
    """巻き戻した後もまだ先頭にいるか。読めなければ先頭とみなす。

    ダイアログを閉じた拍子に位置が飛ぶ本があるため、巻き戻しの成功だけでは
    撮り始めてよいことにならない (#69)。

    **読めるまで待ってから判断する。** 1 回だけ読んで None なら通す形にすると、
    一番起きやすい失敗（ダイアログを閉じた直後にラベルが一瞬消える）が
    そのまま素通りする。ここに来る本は rewind_to_start の最初の読みを
    通っている＝ラベルを読める本なので、待てば読める。
    """
    position, total = _settled_position_pair(page, attempts=REWIND_REREAD_ATTEMPTS)
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
    rewind_info: dict = {}
    stopped_at: dict = {}

    def note(event, human=None, **fields):
        """manifest に残す情報を控えつつ、そのまま emit する。

        イベントを横取りするのは、rewind_to_start / capture_pages の戻り値を
        増やすと呼び出し側とテストに広く波及するため。出している情報は
        すでに揃っているので、拾って manifest に入れるだけでよい。
        """
        if event == "shot_mode_changed" and "page" in fields:
            shot_mode_changed.append(fields["page"])
        if event == "rewound":
            rewind_info.update(
                {
                    k: fields.get(k)
                    for k in ("ok", "presses", "position", "total", "reason", "via_toc", "jumps")
                }
            )
        if event == "capture_stopped":
            stopped_at.update(
                {k: fields.get(k) for k in ("page", "reason", "position", "book_total")}
            )
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
            rewind=rewind_info,
            stopped_at=stopped_at,
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

            # 開いた時点でリーダーが落ちている本。#76 の 4 冊を撮り直すと
            # まさにこの状態で開く。ここで見ないと、位置ラベルが読めないまま
            # detect_turn_key まで進んで「ページ送りの向きを判定できません
            # （--page-turn で明示してください）」になり、**原因も対処も
            # 間違った案内**になる。ログの原因別集計も汚れる
            broken = reader_error_text(page)
            if broken:
                emit(
                    "reader_error",
                    human=f"リーダーがエラーを出しています: {broken}",
                    asin=asin,
                    message=broken,
                )
                emit_error(
                    emit,
                    f"開いた時点でリーダーがエラーを出しています（{broken}）。"
                    "本の問題ではなくリーダー側の状態なので、時間をおいて撮り直してください",
                )
                stopped_reason = "reader_error"
                write_manifest()
                return EXIT_ERROR

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
                    # 巻き戻しの結果を一番残したいのがこの経路。書かずに返すと
                    # manifest 自体が無い (#72)
                    write_manifest()
                    return EXIT_ERROR
                forward = detected
                turn_source = "detected"
            else:
                forward = requested
                turn_source = "explicit"
            key = turn_key(forward)
            emit("status", human=f"ページ送りキー: {key}", message=f"ページ送りキー: {key}")

            if not no_rewind:
                rewound, _ = rewind_to_start(page, forward, max_rewind=max_rewind, emit=note)
                if not rewound:
                    emit_error(
                        emit,
                        "先頭ページまで戻せませんでした。途中から撮ると本の一部だけが"
                        "完成扱いになるため中止します（--no-rewind で無視できます）",
                    )
                    stopped_reason = "rewind_failed"
                    # 巻き戻しの結果を一番残したいのがこの経路。書かずに返すと
                    # manifest 自体が無い (#72)
                    write_manifest()
                    return EXIT_ERROR
                # ここで閉じるダイアログも位置を飛ばしうる（Whispersync の
                # 「最後に読んでいたページへ移動しますか」）。巻き戻しが
                # 成功したあとに飛ばされると、そのまま途中から撮り始める。
                # 閉じたかどうかに関わらず見直す。dismiss_dialogs は
                # evaluate が例外を投げると 0 を返すので、戻り値で門番すると
                # チェックごと飛ぶ
                dismiss_dialogs(page)
                if not _still_at_start(page, emit=emit):
                    # 「戻し切れなかった」と区別する。342 冊のログを原因別に
                    # 数えられるように
                    stopped_reason = "moved_after_rewind"
                    # 巻き戻しの結果を一番残したいのがこの経路。書かずに返すと
                    # manifest 自体が無い (#72)
                    write_manifest()
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
    if stopped_reason == "reader_error":
        # **完成扱いにしない。** 0 で返すと batch が出力を見てスキップし、
        # 途中までの本がそのまま確定する (#76)
        emit_error(
            emit,
            f"{total} ページでリーダーが落ちたため中断しました。"
            "最終ページではないので、この本は撮り直しが要ります",
        )
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
