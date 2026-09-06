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

import datetime
import hashlib
import json
import os
import re

from core.pipeline import (
    EXIT_BAD_ARGS,
    EXIT_ERROR,
    EXIT_NO_IMAGES,
    EXIT_OK,
    MANIFEST_NAME,
    SHOT_ELEMENT,
    SHOT_VIEWPORT,
    clear_output_images,
    emit_error,
    null_emit,
)

BOOK_URL = "https://read.amazon.co.jp/?asin={asin}"
SIGNIN_MARKER = "/ap/signin"

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

# 読書位置の表示。実測で 2 形式ある:
#     "6/339ページ ● 1%"   (ページ表示)
#     "位置1/3495 ● 0%"    (位置表示)
# どちらも先頭の数値が前進で増えるので、そこだけ読めば向きを判定できる。
# スクラバー (#kr-scrubber-bar) の値は縦書きだと逆行するため使わない。
POSITION_SELECTOR = ".footer-label.position"
_POSITION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def read_position(page):
    """現在の読書位置を数値で返す。読めなければ None。"""
    try:
        locator = page.locator(POSITION_SELECTOR)
        if not locator.count():
            return None
        # text_content を使う。撮影前に UI を CSS で隠すため、
        # inner_text だと描画されていない要素から空文字が返ることがある
        matched = _POSITION_RE.search(locator.first.text_content() or "")
    except Exception:
        return None
    return int(matched.group(1)) if matched else None


def _settled_position(page, *, page_wait=DEFAULT_PAGE_WAIT, attempts=4):
    """読書位置が読めるまで数回待って返す。読めなければ None。

    読み込み直後はラベルの描画が間に合わず None になることがある。
    1 回で諦めると「動いていない」と誤判定して向きの判定に失敗する。
    """
    for i in range(attempts):
        position = read_position(page)
        if position is not None:
            return position
        if i < attempts - 1:
            page.wait_for_timeout(int(page_wait * 1000))
    return None


def detect_turn_key(page, *, page_wait=DEFAULT_PAGE_WAIT, emit=null_emit):
    """前進するページ送りキーを実測で判定する。判定できなければ None。

    縦書き（右→左）の本では right が前のページに戻る。数百冊には縦書きと
    横書きが混ざるため、決め打ちだと「正常終了したのに中身が逆順」の本が
    紛れ込む。読書位置の数値が増える方を前進とみなす。

    判定のために動かした分は元に戻す。戻さないと表紙を落とすため。
    （キャプチャ自体が本を読み進めるので、読書位置は結局末尾まで動く。
    ここで戻すのは読書位置の保全のためではない。）
    """
    before = _settled_position(page, page_wait=page_wait)
    if before is None:
        emit("status", human="読書位置を読めないため送りキーを判定できません")
        return None

    for candidate in ("left", "right"):
        # サインイン直後などに遅れて出るモーダルがキーを吸うため、
        # 押す前に毎回閉じる（閉じずに判定すると両方向とも動かず失敗する）
        dismiss_dialogs(page)
        page.keyboard.press(turn_key(candidate))
        page.wait_for_timeout(int(page_wait * 1000))
        after = _settled_position(page, page_wait=page_wait)
        if after is None or after == before:
            continue
        forward = candidate if after > before else reverse_of(candidate)
        # 判定で動かした分を戻す
        dismiss_dialogs(page)
        page.keyboard.press(turn_key(reverse_of(candidate)))
        page.wait_for_timeout(int(page_wait * 1000))
        emit(
            "page_turn_detected",
            human=f"ページ送りキーを判定しました: {forward}",
            page_turn=forward,
        )
        return forward
    emit("status", human="ページ送りキーを判定できませんでした")
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


def page_shot(page, *, selector=PAGE_IMAGE_SELECTOR):
    """ページ画像の要素だけを撮り、(バイト列, 撮影方式) を返す。

    要素が見つからない・撮れない本（画像レンダラでない本、レイアウト変更）は
    従来どおりビューポート全体にフォールバックする。無人実行なので、撮れなく
    なった瞬間に落とすより、方式を記録して撮り続けるほうが被害が小さい。
    """
    try:
        locator = page.locator(selector)
        if locator.count():
            return locator.first.screenshot(), SHOT_ELEMENT
    except Exception:  # noqa: BLE001 - 撮れない理由は問わずフォールバックする
        pass
    return page.screenshot(), SHOT_VIEWPORT


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

    expect_mode を渡すと、途中で撮影方式が変わったページを警告する。方式が
    変わるとページの寸法も変わるため、validate の size_mismatch でも拾えるが、
    どのページで切り替わったかはここでしか分からない。
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
            emit(
                "status",
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
    before = read_position(page)
    if before is None:
        emit("status", human="読書位置を読めないため巻き戻せません")
        return False, 0

    pressed = 0
    stuck = 0
    at_start = before <= 1
    while pressed < max_rewind and not at_start:
        page.keyboard.press(back)
        page.wait_for_timeout(int(page_wait * 1000))
        pressed += 1
        current = read_position(page)
        if current is None or current >= before:
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

    ok = at_start
    emit(
        "rewound",
        human=f"先頭へ巻き戻しました（{pressed} 回、位置 {before}）"
        if ok
        else f"先頭まで戻り切れませんでした（{pressed} 回、位置 {before}）",
        ok=ok,
        presses=pressed,
        position=before,
    )
    return ok, pressed


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

    save_dir = os.path.join(os.path.abspath(output_folder), title)
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
            dismiss_dialogs(page)
            page.add_style_tag(content=hide_ui_css())
            emit("status", human="ビューアの UI を隠しました", message="ビューアの UI を隠しました")

            # ページ画像の要素が撮れるなら UI も余白も最初から入らない。
            # 撮れない本のために従来のビューポート撮影も残してある。
            shot_mode = resolve_shot_mode(page)
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
                dismiss_dialogs(page)
            total, stopped_reason = capture_pages(
                page,
                save_dir,
                key=key,
                max_pages=max_pages,
                page_wait=page_wait,
                expect_mode=shot_mode,
                emit=emit,
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
