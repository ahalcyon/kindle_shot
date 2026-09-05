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

from core.pipeline import (
    EXIT_BAD_ARGS,
    EXIT_ERROR,
    EXIT_NO_IMAGES,
    EXIT_OK,
    clear_output_images,
    emit_error,
    null_emit,
)

BOOK_URL = "https://read.amazon.co.jp/?asin={asin}"
SIGNIN_MARKER = "/ap/signin"

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
    page_wait=None,
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
        "page_wait": page_wait,
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


def capture_pages(
    page, save_dir, *, key="ArrowLeft", max_pages=None, page_wait=2.5, max_retries=3, emit=null_emit
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
    """
    prev = None
    total = 0
    while True:
        # 途中でセッションが切れると、サインイン画面を本文として保存してしまう
        if not is_signed_in(page.url):
            emit("signin_required", human="キャプチャ中にセッションが切れました")
            return total, "signin_required"

        shot = page.screenshot()
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
                shot = page.screenshot()
                current = digest(shot)
            if current == prev:
                # 1 枚も進めていないなら最終ページではなく送りに失敗している
                return total, "end_of_book" if total > 1 else "no_change"

        total += 1
        filename = f"{total:03d}.png"
        with open(os.path.join(save_dir, filename), "wb") as f:
            f.write(shot)
        emit("page", human=f"Page {total}: {filename}", page=total, file=filename)

        if max_pages and total >= max_pages:
            return total, "max_pages"

        prev = current
        page.keyboard.press(key)
        page.wait_for_timeout(int(page_wait * 1000))


def run_headless_capture(
    profile,
    title,
    output_folder,
    *,
    asin=None,
    url=None,
    profile_key=None,
    max_pages=None,
    page_wait=2.5,
    page_turn=None,
    overwrite=False,
    load_wait=12,
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
    key = turn_key(page_turn or getattr(profile, "page_turn_key", None))
    total = 0
    stopped_reason = "error"

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
            page_wait=page_wait,
        )
        path = os.path.join(save_dir, "manifest.json")
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
            emit("status", human=f"ページ送りキー: {key}", message=f"ページ送りキー: {key}")
            total, stopped_reason = capture_pages(
                page, save_dir, key=key, max_pages=max_pages, page_wait=page_wait, emit=emit
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
