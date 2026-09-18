"""Kindle アプリ（Microsoft Store 版）で本を開いて撮る (#74)

**Cloud Reader 非対応の本を撮る唯一の道。** 蔵書 405 冊のうち 56 冊は
`Kindle App Is Required` で Cloud Reader が開けず、1 ページも撮れていない。

実測で分かったこと（2026-09-18。ウィンドウ 1200x1390）:

- Store 版の実行ファイルは `Kindle.exe` で、`kindle` プロファイルがそのまま使える
- **本は「ライブラリの検索窓に題名を入れて 1 件目をクリック」でしか開けない。**
  `kindle://book?action=open&asin=...` は何も起きない（実測）
- **F11 の全画面では UI が一切消える**が、画面が横長だと 2 段組（見開き）になる。
  幅 1200 なら 1 段組＝ 1 ページになるので、こちらを使う（見開きを割るより素直）
- 読書 UI（上のバー・スライダー・← →）は本文をクリックすると出たり消えたりし、
  **ページにかぶさる**（ページは動かない）。出たまま撮ると上下が隠れるので、消してから撮る
- ページ送りは **→ が常に「次のページ」**（アプリのショートカット一覧）。縦書きでも
  変わらないので、Cloud Reader のような向きの判定（#111）が要らない
- ライブラリ画面は日によって上に 55px の帯が出たり消えたりする（2026-09-18 と翌日で
  「全て」の行の y が 243 と 188）。固定座標では検索窓のつもりが「Kindle ストア」を押す
  ので、位置は青く反転した「全て」の行からの相対で決める

**開いた本が違っていないかを、表紙の OCR で確かめてから撮る。**
検索で 2 冊以上に絞れなかった本は撮らずに飛ばす（別の本を蔵書に入れない）。

使い方:

    python scripts/capture_app_books.py --books unsupported.json --library <蔵書> \\
        --state <作業フォルダ>/app_capture.csv

    python scripts/capture_app_books.py ... --limit 1      # まず 1 冊試す
    python scripts/capture_app_books.py ... --max-pages 6  # 短く試す
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.book_format import IMAGE, SEARCHABLE, book_pdf_path, load_books  # noqa: E402
from core.console import setup_stdio  # noqa: E402

# ウィンドウの大きさ。幅 1200 で 1 段組（1400 以上だと 2 段組になる。実測）
WIDTH, HEIGHT = 1200, 1390
# ウィンドウを置く x（左のモニタ。主モニタのカーソル退避先と分ける）
WINDOW_LEFT = -1600
# ライブラリ画面の位置（上の大きさのとき）。**左の「全て」の行（青く反転）を基準にした相対**。
# 上の余白が日によって違う（実測: 「全て」の中心 y が 243 → 翌日 188）ので固定座標は使えない
LIBRARY_X = 125  # 「全て」の行と検索窓の中心 x
SEARCH_DY = -148  # 検索窓の中心 y（基準から）
COVER_DY = 60  # 検索結果の表紙の中心 y（基準から）
COVER_XS = (358, 599)  # 検索結果の 1 冊目・2 冊目の中心 x
# 本を閉じてライブラリに戻る矢印（タイトルバー、または読書 UI の左上）。UI が消えていれば
# ここは本文で、クリックは UI の出し入れになるだけ。_to_library は Esc・Ctrl+W のあとの最後の手段
BACK_ARROW = (32, 32)
# 読書 UI（上のバー・スライダー・← →）を出し入れするクリック位置。上の余白なので本文の
# リンクを踏まない。実測: 左端のクリックは前のページに戻る、下端は無反応
CHROME_TOGGLE = (600, 60)
# 読書 UI の「戻る」矢印が出る範囲（左上）。ここに暗い画素があれば UI が出ている
CHROME_BOX = (14, 8, 64, 44)
# 読書 UI のバーの帯の x 範囲（矢印の右から右側のアイコンの手前まで）。白地なので明るい
CHROME_BAND = (70, 980)

# 最低限削る余白。読書 UI を消して撮るので無し。UI はページに**かぶさる**（ページは動かない。
# 実測）ので、固定で削ると UI の無い状態では中身を切る。書名ヘッダー・ページ番号フッターも
# 読書 UI の側に出るもので、消せばページに残らない。右上のウィンドウ操作ボタンだけは残るが、
# ページ間で変わらない帯として ui_bands が見つける。
# アプリがタイトルバー（「← Kindle」。実測 48px）を出す表示のときは、その高さを本ごとに
# 測って削る（title_bar_height）
MIN_MARGINS = None
# 本を開いてから読めるようになるまで待つ秒数。未ダウンロードの本はここで落ちる
OPEN_WAIT = 25.0
# これ未満のページ数で「完了」にしない。撮れていない本が完成扱いで固定されるのを防ぐ
MIN_PAGES = 10
DONE = "完了"
COLUMNS = ["asin", "title", "status", "pages", "stopped_reason", "seconds", "detail"]
# PowerShell をコンソール窓なしで起動する（窓が出るとアプリから前面を奪う）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Store 版 Kindle の AUMID（Get-StartApps で取れる。末尾の英数字は発行元ごとの固定値）
APP_ID = "AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36!App"
# 起動してからライブラリが読めるまで待つ秒数。起動し直したアプリは遅れて最後の本を開き直し
# 最大化するので、それが済むまで待つ（8 秒では済んでいなかった。実測）
LAUNCH_WAIT = 20.0
# 検索結果が 1 冊に絞られるまで待つ上限（秒）
SEARCH_WAIT = 10
# タイトルバーの高さとして信じる範囲（実測 48px）。外れたら測れないものとして撮らない
TITLE_BAR_RANGE = (40, 60)
# 撮らずに飛ばしたときの画面を残す先（main が state のフォルダに設定する）。無人実行の
# 失敗は画面が残っていないと原因を追えない（実測: 「読書 UI を消せない」が続いても分からなかった）
SHOT_DIR: str | None = None


CURRENT_BOOK = ""  # 保存する画面のファイル名に使う（main が本ごとに設定する）


def _keep_shot(hwnd, name):
    """いまの画面を SHOT_DIR に残す（本ごとに上書き。増え続けない）。残せなくても処理は止めない。"""
    if not SHOT_DIR:
        return
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        _shot(hwnd).save(os.path.join(SHOT_DIR, f"{CURRENT_BOOK or 'book'}_{name}.png"))
    except Exception:  # noqa: BLE001 - 記録の失敗で本を落とさない
        pass


def _norm(text):
    """題名を突き合わせるための正規化（記号と空白を落とす）。"""
    import re
    import unicodedata

    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text or "")).lower()


def title_matches(want, seen, *, ratio=0.7, cap=24):
    """OCR で読んだ文字が、開きたかった本のものか。

    見るのは、**題名と読んだ文字の最長共通部分**が題名の ``ratio`` 以上あること
    （長い題名で無理を言わないよう ``cap`` 文字で頭打ち）。表紙のページ全体を読むので、
    相手には著者名・出版社名・帯の文句が混ざる。部分一致で見るのはそのため。

    **先頭 8 文字の一致では緩すぎる。** 対象は英語の数学書・技術書が多く、
    `Linear Algebra Done Right` と `Linear Algebra and Its Applications` のように
    先頭が同じ本が普通にある（実測でこの 2 つが同じ本と判定された）。
    """
    import re
    from difflib import SequenceMatcher

    # **括弧書き（シリーズ名・レーベル名）は照合に使わない。** 同じシリーズの別の本の表紙にも
    # 同じ文字列が印字されるので、`(Undergraduate Texts in Mathematics)` だけで cap を超えて
    # 「同じ本」と通ってしまう（レビューで実測: Linear Algebra Done Right と Analysis I）
    want = re.sub(r"[（(][^（()）]*[)）]", " ", want or "")
    a, b = _norm(want), _norm(seen)
    if not a or not b:
        return False
    need = min(int(len(a) * ratio), cap)
    if need <= 0:
        return False
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size >= need


def load_state(path):
    """撮り終えた本（status が 完了）の鍵。失敗した本は次にもう一度試す。"""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["asin"] or r["title"] for r in csv.DictReader(f) if r["status"] == "完了"}


def pending(books, done, library):
    """まだ撮れていない本。PDF が既にあるものは飛ばす。"""
    out = []
    for book in books:
        key = book.get("asin") or book.get("title", "")
        if key in done:
            continue
        if os.path.exists(book_pdf_path(library, book.get("title", ""))):
            continue
        out.append(book)
    return out


# --- ここから下は実機（画面）を触る ------------------------------------------


def _app():
    """Kindle アプリのウィンドウ。**プロセス名まで確かめる。**

    ``find_window`` の ``process_name`` は絞り込みではなく点数の加算なので、アプリが
    落ちていると「題名に kindle を含む別のウィンドウ」（エディタ・エクスプローラ・
    ターミナル）が返る。そこへ Ctrl+A / Ctrl+V や ← を何百回も送ると実害が出る。
    ``core/capture_runner.find_verified_window`` と同じ考え方で、一致しなければ落とす。
    """
    from core.win32_utils import activate_window, find_window, get_window_process_name

    hwnd = find_window("kindle", process_name="Kindle.exe")
    if not hwnd:
        raise RuntimeError("Kindle アプリのウィンドウが見つかりません")
    seen = (get_window_process_name(hwnd) or "").lower()
    if seen != "kindle.exe":
        raise RuntimeError(f"Kindle アプリではないウィンドウでした（{seen}）")
    activate_window(hwnd, click_position="none")
    time.sleep(0.8)
    return hwnd


def restart_app(*, timeout=60.0, emit=print):
    """Kindle アプリを終了して起動し直す。窓が出なければ RuntimeError。

    **一度おかしくなったアプリは操作で戻せない。** F11 全画面を経たあとは検索窓を
    クリックしても Tab で移っても文字が入らなくなり、何度試しても戻らなかった（実測）。
    起動し直せば入る。実行の最初と、本を開けなかったあとに呼ぶ。
    """
    from core.win32_utils import find_window, get_window_process_name

    subprocess.run(["taskkill.exe", "/IM", "Kindle.exe", "/F"], check=False, capture_output=True)
    time.sleep(3.0)
    subprocess.run(
        ["explorer.exe", rf"shell:AppsFolder\{APP_ID}"], check=False, capture_output=True
    )
    started = time.time()
    while time.time() - started < timeout:
        # find_window の process_name は加点であって絞り込みではない（_app と同じ）。
        # 題名に kindle を含む別の窓で「起動した」と見ないよう、プロセス名まで確かめる
        hwnd = find_window("kindle", process_name="Kindle.exe")
        if hwnd and (get_window_process_name(hwnd) or "").lower() == "kindle.exe":
            time.sleep(LAUNCH_WAIT)
            emit("  アプリを起動し直した")
            return
        time.sleep(1.0)
    raise RuntimeError("Kindle アプリを起動し直せない")


def _place(hwnd, width=WIDTH, height=HEIGHT, *, settle=2.0, tries=10):
    """撮影する大きさ・位置にウィンドウを置く（1 段組にするため）。置けなければ RuntimeError。

    **置いたあと、そのままでいることを確かめる。** 起動し直したアプリは、少し遅れて最後に
    読んでいた本を自動で開き、ウィンドウを最大化し直す（実測: 置いたあとに最大化されて、
    検索窓の位置が窓の外になり、以後の本が全部「開けない」になった）。2 回続けて
    置いた通りの矩形なら安定したと見る。
    """
    from core.win32_utils import get_window_rect

    want = _wanted_rect(width, height)
    stable = 0
    for _ in range(tries):
        _move_window(hwnd, width, height)
        time.sleep(settle)
        stable = stable + 1 if get_window_rect(hwnd) == want else 0
        if stable >= 2:
            return
    raise RuntimeError(f"ウィンドウを置けない（{get_window_rect(hwnd)}）")


def _move_window(hwnd, width, height):
    import ctypes

    # 最大化のままだと枠の分だけ描画がずれる（実測: 8px）。通常の状態に戻してから置く
    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    ctypes.windll.user32.SetWindowPos(hwnd, None, WINDOW_LEFT, 0, width, height, 0x0040)


def _wanted_rect(width=WIDTH, height=HEIGHT):
    return (WINDOW_LEFT, 0, WINDOW_LEFT + width, height)


def _is_placed(hwnd, width=WIDTH, height=HEIGHT):
    from core.win32_utils import get_window_rect

    return get_window_rect(hwnd) == _wanted_rect(width, height)


def _shot(hwnd, box=None):
    from PIL import ImageGrab

    from core.win32_utils import get_window_rect

    left, top, right, bottom = get_window_rect(hwnd)
    if box:
        left, top = left + box[0], top + box[1]
        right, bottom = left + (box[2] - box[0]), top + (box[3] - box[1])
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)


def _click(hwnd, x, y, wait=1.2):
    import pyautogui

    from core.win32_utils import get_window_rect

    left, top, _, _ = get_window_rect(hwnd)
    pyautogui.click(left + x, top + y)
    time.sleep(wait)


def _ocr(image):
    import shutil
    import tempfile

    from core.ocr_engine import process_folder_collect

    work = tempfile.mkdtemp(prefix="app_ocr_")
    try:
        image.save(os.path.join(work, "shot.png"))
        ok, results = process_folder_collect(work)
        return " ".join(text for _, text in results) if ok else ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def is_cover(image, *, threshold=6.0):
    """その場所に本の表紙があるか（背景は一様、表紙は色がばらつく）。

    開いた本が合っているかを、**検索結果がちょうど 1 冊か**で確かめるために使う。
    ページの中身で確かめる案は捨てた: 開いた直後の画面は本によって違い（表紙・前回の続き）、
    固定レイアウトの本には書名ヘッダーが無く、表紙が絵だけの本もある（実測）。
    """
    from PIL import ImageStat

    return max(ImageStat.Stat(image.convert("RGB")).stddev) > threshold


def _clipboard():
    """いまクリップボードに入っている文字。読めなければ空文字。"""
    # PowerShell の標準出力はコンソールのコードページ（cp932）で出る。UTF-8 で読むと日本語の
    # 題名が全部「入れられない」になる（実測: 本番 2 冊目で発覚）。出力を UTF-8 に固定する
    got = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw",
        ],
        check=False,
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    return got.stdout.decode("utf-8", errors="replace").strip()


def _to_clipboard(text):
    """題名をクリップボードに入れる（IME を通さずに検索窓へ貼るため）。

    PowerShell の -Command に題名をそのまま渡すと、括弧や空白を含む題名が
    コマンドとして解釈されて落ちる（実測: `Beginning in Algebraic Geometry
    (Undergraduate Texts in Mathematics)` で CommandNotFoundException）。
    UTF-8 のファイル経由なら題名の中身に左右されない。
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 '{path}').TrimEnd()",
            ],
            check=False,
            capture_output=True,
            creationflags=NO_WINDOW,
        )
    finally:
        os.unlink(path)
    # **貼れたことを確かめる。** Set-Clipboard は他のプロセスがクリップボードを
    # 掴んでいると失敗する。黙って失敗すると 1 冊前の題名が残り、前の本を開いて
    # **今の本の題名で蔵書に保存**してしまう
    return _norm(_clipboard()) == _norm(text)


def _cover_box(point, size=60):
    x, y = point
    return (x - size, y - size, x + size, y + size)


def find_library_anchor(image, *, x=LIBRARY_X, span=(60, 500), height=(28, 44)):
    """ライブラリ画面の基準 y（左の「全て」の行の中心）。ライブラリでなければ None。

    左の列 ``x`` を上から見て、青く反転している一続きの行を探す。「全て」の行は
    高さ 36px・左の欄いっぱい（x = 20〜232）の青で、上下は白い余白（実測）。
    上の余白が変わっても、この形の行を探せば追える。

    **本のページの青い箱と区別する。** 高さと上下の白だけで見ていたとき、章見出しの
    青い箱（x = 115〜270、高さ 42px）を「全て」の行と取り違え、本を開いたまま
    ライブラリにいると判定した（実測）。行の左端・右端も青く、欄の外は白いことを要求する。

    **画面がどこにいるかを確かめずに操作しない。** 本を開いたままだと、検索窓のつもりの
    クリックが本文に当たり、Ctrl+A が「読書補助機能」になってダイアログが開く（実測）。
    """
    from PIL import ImageStat

    rgb = image.convert("RGB")

    def blue_at(px, y):
        r, _g, b = ImageStat.Stat(rgb.crop((px - 4, y, px + 4, y + 1))).mean
        return b > r + 40 and b > 120

    def white_at(px, y):
        return min(ImageStat.Stat(rgb.crop((px - 4, y, px + 4, y + 1))).mean) >= 240

    runs: list[list[int]] = []
    cur = None
    for y in range(span[0], min(span[1], rgb.height)):
        if blue_at(x, y):
            cur = [y, y] if cur is None else [cur[0], y]
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    for top, bottom in runs:
        if not (height[0] <= bottom - top + 1 <= height[1]):
            continue
        mid = (top + bottom) // 2
        if not (top - 10 >= 0 and bottom + 10 < rgb.height):
            continue
        # 行の左端と右端まで青く、欄の外（x=245）と上下は白い
        if not (blue_at(30, mid) and blue_at(225, mid)):
            continue
        if not (white_at(245, mid) and white_at(x, top - 10) and white_at(x, bottom + 10)):
            continue
        return mid
    return None


def is_library(image):
    """ライブラリ画面にいるか（ウィンドウ全体の画像で見る）。"""
    return find_library_anchor(image) is not None


def _to_library(hwnd, *, tries=3):
    """どの画面にいても、ライブラリに戻す。戻せなければ False。"""
    import pyautogui

    for _ in range(tries):
        for _ in range(2):
            pyautogui.press("esc")  # 開いたままのダイアログを閉じる
            time.sleep(0.4)
        if not _is_placed(hwnd):
            # 最大化されていると欄の幅が変わり、「全て」の行の検出が当てにならない。先に置き直す
            _place(hwnd)
        if is_library(_shot(hwnd)):
            return True
        pyautogui.hotkey("ctrl", "w")
        time.sleep(2.0)
        if is_library(_shot(hwnd)):
            return True
        _click(hwnd, *BACK_ARROW, wait=2.0)
    return is_library(_shot(hwnd))


def open_book(hwnd, title, *, emit=print):
    """ライブラリで題名を検索し、**1 件だけに絞れたとき**その本を開く。

    2 件以上に絞れなかった本は開かない（別の本を蔵書に入れない）。
    """
    import pyautogui

    from core.win32_utils import activate_window

    if not _to_library(hwnd):
        emit("  ライブラリに戻れない")
        return False
    if not _is_placed(hwnd):
        # 前の本を閉じる間に最大化し直されることがある（起動し直した直後）。置き直す
        _place(hwnd)
    anchor = find_library_anchor(_shot(hwnd))
    if anchor is None:
        emit("  ライブラリの「全て」の行が見つからない")
        return False
    if not _to_clipboard(title):
        emit("  クリップボードに題名を入れられない")
        return False
    # クリップボードに入れる PowerShell が前面を奪う。奪われたままだと、検索窓への
    # クリックはウィンドウを前面に戻すだけでフォーカスが移らない（実測: 貼り付けが空振り）
    activate_window(hwnd, click_position="none")
    # **題名が検索窓に入ったことを見てから Enter を押す。** 入っていないまま Enter を押すと
    # 絞り込まれず、蔵書全体の 1 冊目が「検索結果」に見える
    search_y = anchor + SEARCH_DY
    search_box = (20, search_y - 16, 240, search_y + 16)
    for attempt in range(4):
        if attempt % 2 == 0:
            # Esc で何も選ばれていない状態にしてから Tab を押すと検索窓に入る（実測）。
            # クリックは検索窓にフォーカスを移さないことがある（実測: F11 全画面から
            # 戻したあとは何度クリックしても入らなかった）ので、両方を交互に試す
            pyautogui.press("esc")
            time.sleep(0.3)
            pyautogui.press("tab")
            time.sleep(0.5)
        else:
            _click(hwnd, LIBRARY_X, search_y, wait=0.8)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
        time.sleep(0.4)
        # 見るのは暗い画素（文字）が増えたか。画像の同一性で見るとキャレットの点滅で「入った」になる
        empty = _dark_pixels(_shot(hwnd, search_box))
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.8)
        if _dark_pixels(_shot(hwnd, search_box)) > empty + 20:
            break
    else:
        emit("  検索窓に題名が入らない")
        return False
    pyautogui.press("enter")
    first, second = ((x, anchor + COVER_DY) for x in COVER_XS)
    # 結果が出るまで待つ（起動直後は 3 秒で出なかった。実測）。絞り込む前の並びでも
    # 1 冊目の枠には表紙があるので、「2 冊目の枠が空」になるまでを結果の確定と見る
    for _ in range(SEARCH_WAIT):
        time.sleep(1.0)
        if is_cover(_shot(hwnd, _cover_box(first))) and not is_cover(
            _shot(hwnd, _cover_box(second))
        ):
            break
    else:
        if not is_cover(_shot(hwnd, _cover_box(first))):
            emit("  検索に当たらない")
        else:
            emit("  検索が 2 冊以上に当たる")
        return False
    # 結果が出た直後のクリックは飲まれることがある（実測: 同じ状態でもう一度押すと開いた）。
    # 開いたことをライブラリ画面が消えたかで確かめ、8 秒反応が無ければもう一度押す
    time.sleep(1.0)
    for attempt in range(2):
        _click(hwnd, *first, wait=2.0)
        waited = 2.0
        while waited < OPEN_WAIT:
            if not is_library(_shot(hwnd)):
                return True
            time.sleep(1.0)
            waited += 1.0
            if attempt == 0 and waited >= 8.0:
                break
    emit("  本が開かない（未ダウンロードで時間がかかっている可能性）")
    return False


def title_bar_height(image, *, x=(300, 900), limit=100):
    """アプリのタイトルバー（「← Kindle」の帯）の高さ。無ければ 0。

    アプリは、タイトルバーを出してその下にページを置く表示と、ページを上端まで広げる
    表示を行き来する（実測: 同じ日に両方あった。何で切り替わるかは分かっていない）。
    バーは薄い灰色（実測 247,249,250。高さ 48px）でページの白（255）と違うので、
    上端から続く灰色の行を数える。ページを上端まで広げているときは 0 行目が白か本文。
    """
    from PIL import ImageStat

    rgb = image.convert("RGB")

    def gray(y):
        mean = ImageStat.Stat(rgb.crop((x[0], y, x[1], y + 1))).mean
        return all(236 <= v <= 253 for v in mean)

    # 0 行目はウィンドウの枠線で暗いことがある（実測: 前面のとき 75）。2 行目までに
    # 灰色が始まらなければバーは無い
    start = next((y for y in range(3) if gray(y)), None)
    if start is None:
        return 0
    y = start
    while y < min(limit, rgb.height) and gray(y):
        y += 1
    return y


def reader_top(hwnd, *, emit=print):
    """いま開いている本の、ページが始まる y（タイトルバーの高さ）。測れなければ None。

    0（バー無し）か実測のレンジ（TITLE_BAR_RANGE）だけを信じる。範囲外の値は「バーではない
    薄い色の何か」（淡い表紙・表示テーマ・読書 UI のバー）なので、そのまま削ると全ページの
    上が欠けた本が完成扱いになる。撮らない（安全側）。
    """
    top = title_bar_height(_shot(hwnd))
    if top == 0 or TITLE_BAR_RANGE[0] <= top <= TITLE_BAR_RANGE[1]:
        return top
    emit(f"  タイトルバーの高さを測れない（{top}px）")
    return None


def _dark_pixels(image, *, dark=150):
    return sum(1 for p in image.convert("L").tobytes() if p < dark)


def has_slider(image, *, y_from_bottom=64, x=(300, 900), contrast=40, flat=8, share=0.9):
    """読書 UI のスライダー（下端から 64px の横線）が写っているか。

    UI の有無を色の多寡で見る方法は表紙で誤った（黒い表紙、左右が黒い表紙、リフロー本が
    ページ自体に描く「ページ 9/253」。実測）。スライダーは**決まった y に引かれる一様な横線**で、
    その上下 6px はページ（白か絵）なので、「行がほぼ一様で、上下の行と明るさが違う」で見る。
    本文にこの位置・この長さの罫線が来ることはまず無く、来ても切り替え前後の比較で弾く。

    「一様」は中央値 ±``flat`` に入る画素の割合が ``share`` 以上。標準偏差で見ると、読書位置の
    つまみ（白い縁付きの暗い円、幅 18px）が範囲に入る本（進捗 18〜78% で開いた本）で
    一様でなくなり、UI があっても見逃した（レビューで実測）。
    """
    from PIL import ImageStat

    gray = image.convert("L")
    y = image.height - y_from_bottom
    values = list(gray.crop((x[0], y, x[1], y + 1)).tobytes())
    median = sorted(values)[len(values) // 2]
    if sum(1 for v in values if abs(v - median) <= flat) < share * len(values):
        return False

    def mean(dy):
        return ImageStat.Stat(gray.crop((x[0], y + dy, x[1], y + dy + 1))).mean[0]

    return abs(median - mean(-6)) > contrast and abs(median - mean(6)) > contrast


def _toggle_chrome(hwnd, *, top=0):
    """読書 UI を切り替え、(切り替え前, 切り替え後) の画面を返す。"""
    before = _shot(hwnd)
    _click(hwnd, CHROME_TOGGLE[0], CHROME_TOGGLE[1] + top, wait=1.2)
    return before, _shot(hwnd)


def chrome_hidden(hwnd, *, top=0):
    """読書 UI が消えているか。**切り替えて比べ、元に戻す。** 分からなければ None。

    出す→消すの 2 回切り替えて、(a) 2 回目で元の画面に戻ること、(b) 切り替え後にだけ
    スライダーが写ること、を確かめる。(a)(b) なら元は「消えていた」。逆に元にだけ写れば
    「出ていた」。どちらでもなければ（クリックが効いていない等）None。
    """
    before, shown = _toggle_chrome(hwnd, top=top)
    _click(hwnd, CHROME_TOGGLE[0], CHROME_TOGGLE[1] + top, wait=1.2)
    restored = _shot(hwnd)
    if _digest(restored) != _digest(before):
        return None
    was, now = has_slider(before), has_slider(shown)
    if now and not was:
        return True
    if was and not now:
        return False
    return None


def hide_reader_chrome(hwnd, *, top=0, emit=print):
    """読書 UI を消す。消せなければ False。

    UI はページに**かぶさる**（出しても消してもページは動かない。実測）ので、出たまま
    撮ると上下が UI の分だけ隠れる。本を開いた直後にどちらの状態かは決まっていない。
    1 回切り替えてスライダーが消えれば消えた。現れたなら出したので、もう 1 回切り替えて
    消えることを確かめる。
    """
    before, after = _toggle_chrome(hwnd, top=top)
    was, now = has_slider(before), has_slider(after)
    if was and not now:
        return True
    if now and not was:
        shown, hidden = _toggle_chrome(hwnd, top=top)
        if has_slider(shown) and not has_slider(hidden):
            return True
        emit("  読書 UI を消せない（出したあと消えない）")
    else:
        emit("  読書 UI を消せない（切り替えても変わらない）")
    _keep_shot(hwnd, "chrome")
    return False


def verify_title(hwnd, title, *, top=0, emit=print):
    """いま開いているページの文字が、撮りたい本のものか。

    **先頭（表紙）まで戻してから呼ぶこと。** 表紙には題名が大きく入っているので、
    ページ全体を OCR すれば照合できる。本文ページの書名ヘッダーは題名が途中で切れる。

    検索が 1 件でも、部分一致で別の本（巻数違い・シリーズの別冊）が当たりうる。
    ここを飛ばすと、**別の本の中身が撮りたかった本の題名で蔵書に入る**。
    読めなかったときも撮らない（安全側。あとで手で確かめる）。
    """
    seen = _ocr(_shot(hwnd))
    if title_matches(title, seen):
        return True
    # 表紙の題名が飾り文字で読めない本がある（実測: 「学びを結果に変えるアウトプット大全」は
    # OUTPUT の英字しか読めなかった）。読書 UI を出すと上のバーに**アプリが持つ題名**が出るので、
    # そちらも読む。読んだら UI を消し直す（消せなければ撮らない）
    before, shown = _toggle_chrome(hwnd, top=top)
    if has_slider(before) or not has_slider(shown):
        # 出したつもりの UI が見えないなら、バーを読んだことにも UI の状態にも確信が持てない。
        # ここで通すと、UI が写った本が完成扱いになる（レビュー指摘）
        emit("  読書 UI が出ない（題名のバーを読めない）")
        _keep_shot(hwnd, "title")
        return False
    bar = shown.crop((CHROME_BAND[0], top, CHROME_BAND[1], top + CHROME_BOX[3] + 8))
    seen_bar = _ocr(bar)
    if not hide_reader_chrome(hwnd, top=top, emit=emit):
        return False
    if bar_title_matches(title, seen_bar):
        return True
    emit(f"  開いた本を確かめられない（読めた文字: {seen[:60]!r} / バー: {seen_bar[:40]!r}）")
    _keep_shot(hwnd, "title")
    return False


def bar_title_matches(want, bar, *, min_chars=8):
    """読書 UI のバーの題名が、撮りたい本のものか。表紙より厳しく**前方一致**で見る。

    バーはアプリが持つ書誌の題名そのもの（ノイズ無し）で、長いと末尾が「…」で省略される。
    表紙と同じ最長共通部分の照合だと、巻数・版・号だけ違う本（❶ と ❷、third と fourth
    edition）が先頭の共通部分で通る（レビューで実測）。省略前の文字列が題名の先頭と
    一致することを要求すれば、省略される前に見えている巻数の違いで弾ける。
    OCR の揺れで落ちるのは撮らない側なので安全。
    """
    seen = _norm(bar.replace("…", " ").replace("...", " "))
    full = _norm(want)
    if len(seen) < min_chars or not full:
        return False
    return full.startswith(seen) or seen.startswith(full)


def _digest(image):
    import hashlib

    return hashlib.sha1(image.convert("L").resize((160, 180)).tobytes()).hexdigest()


def rewind_to_start(hwnd, *, max_presses=1500, step=10, interval=0.05, emit=print):
    """本の先頭まで戻す。戻り切ったら True。

    **画面キャプチャ経路には「先頭から撮る」処理が無い**（headless 経路の `max_rewind` は
    Cloud Reader を開くときの話）。アプリは前回の続きから開くので、戻さずに撮ると
    途中から始まる。実測: 8 ページ撮ったら xiii ページから始まった。

    **先に「キーが届くこと」を確かめる。** 画面が変わらないことだけを見ると、
    「先頭にいる」と「キーが届いていない」（フォーカスを奪われた・別のウィンドウを
    掴んでいる）が区別できず、本の途中から撮った部分本が完成扱いになる。
    """
    import pyautogui

    # **どちらの向きでも動かないときだけ「効かない」と見なす。**
    # 最終ページでは → が、先頭では ← が効かない（実測: 前回の続きが最終ページだった本を
    # → だけで見て「効かない」と誤判定した）
    start = _digest(_shot(hwnd))
    for key in ("right", "left"):
        pyautogui.press(key)
        time.sleep(1.0)
        if _digest(_shot(hwnd)) != start:
            break
    else:
        emit("  ページ送りが効かない")
        return False
    before = _digest(_shot(hwnd))
    for _pressed in range(0, max_presses, step):
        pyautogui.press("left", presses=step, interval=interval)
        time.sleep(0.6)
        after = _digest(_shot(hwnd))
        if after == before:
            return True
        before = after
    emit(f"  先頭まで戻り切らない（← を {max_presses} 回送っても変わり続ける）")
    return False


def quarantine(library, title, work_dir):
    """完了でない本の PDF を蔵書から作業フォルダへ退避する。退避したら行き先、無ければ None。

    このスクリプトが撮る本は、撮る前に pending() が「PDF が無い」と確かめたものなので、
    ここにある PDF はこの実行が作ったもの。他の経路で作った完成本を動かすことはない。
    """
    src = book_pdf_path(library, title)
    if not os.path.exists(src):
        return None
    folder = os.path.join(work_dir or ".", "failed_pdfs")
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, f"{int(time.time())}_{os.path.basename(src)}")
    os.replace(src, dst)
    return dst


def _config():
    """run_book に渡す設定。**`kindle` プロファイルのクリックを止める。**

    ビルトインの `kindle` は前面化のときウィンドウの左上（60, 10）をクリックする。
    読書 UI を消した状態ではそこが本文なので、クリックで UI が出たり表示が変わったりして、
    撮れたページが拡大された断片になった（実測: 2448x1360）。前面化だけさせる。
    """
    from core.config import load_config

    cfg = load_config()
    profiles = cfg.setdefault("capture", {}).setdefault("profiles", {})
    profiles.setdefault("kindle", {})["click_position"] = "none"
    return cfg


class _Watch:
    """run_book のイベントから、ページ数・止まった理由・**失敗の理由**を拾う。

    無人で何十冊も回すので、落ちた理由が一覧に残らないと原因を追えない。
    画面キャプチャ経路がページ数を出すのは ``result``（``core/capture_runner.py`` の
    manifest と同じ内容）。``capture_stopped`` は headless 経路のイベントで、ここには来ない。
    """

    def __init__(self):
        self.total = 0
        self.stopped = ""
        self.error = ""

    def __call__(self, event, **kw):
        if event == "result" and kw.get("total_pages"):
            self.total = kw["total_pages"]
            self.stopped = str(kw.get("stopped_reason") or "")
        elif event == "error":
            self.error = str(kw.get("message") or kw)[:200]


def main(argv=None):
    # 題名に cp932 で書けない字があっても、進捗の 1 行で一括処理を止めない
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--books", required=True, help="books.json（title / asin / format）")
    ap.add_argument("--library", required=True, help="蔵書フォルダ（撮影時の --out）")
    ap.add_argument("--state", required=True, help="進捗の CSV。あれば続きから")
    ap.add_argument("--limit", type=int, help="この冊数だけ撮る")
    ap.add_argument("--max-pages", type=int, help="1 冊あたりのページ数の上限（試し撮り用）")
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="--max-pages を付けたまま蔵書へ書くことを許す（ふつうは使わない）",
    )
    ap.add_argument(
        "--format",
        dest="fmt",
        default=None,
        choices=[IMAGE, SEARCHABLE],
        help="既定は一覧の format",
    )
    ap.add_argument("--dry-run", action="store_true", help="撮らずに、対象の一覧だけ出す")
    args = ap.parse_args(argv)
    if args.max_pages and not args.allow_partial:
        # 抜き取りの出力を蔵書に混ぜない（AGENTS.md）。PDF があると次から飛ばされるので、
        # 途中までの本が完成扱いで固定される
        ap.error(
            "--max-pages を使うときは --library に作業フォルダを指定し --allow-partial を付ける"
        )

    books = load_books(args.books)
    todo = pending(books, load_state(args.state), args.library)
    if args.limit:
        todo = todo[: args.limit]
    print(f"対象 {len(todo)} 冊 / 一覧 {len(books)} 冊", flush=True)
    if args.dry_run:
        for book in todo:
            print(f"  {book.get('asin', '')} {book.get('title', '')[:50]}", flush=True)
        return 0

    from core.pipeline import EXIT_OK, run_book
    from core.win32_utils import get_window_rect

    global SHOT_DIR
    SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(args.state)), "failed_shots")
    exists = os.path.exists(args.state)
    counts: dict[str, int] = {}
    # 前の実行で崩れた状態（全画面・入力が効かない）を引きずらない
    need_restart = True
    with open(args.state, "a" if exists else "w", encoding="utf-8-sig", newline="") as state:
        writer = csv.DictWriter(state, fieldnames=COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for n, book in enumerate(todo, 1):
            title, asin = book.get("title", ""), book.get("asin", "")
            row = {"asin": asin, "title": title}
            global CURRENT_BOOK
            CURRENT_BOOK = asin or _norm(title)[:20]
            started = time.time()
            print(f"[{n}/{len(todo)}] {title[:46]}", flush=True)
            if need_restart:
                # 起動し直せないなら一括処理ごと止める（1 冊ずつ 60 秒待って失敗を積むより分かりやすい）
                restart_app()
                need_restart = False
            try:
                hwnd = _app()
                _place(hwnd)
                top: int | None = 0
                if not open_book(hwnd, title):
                    row["status"] = "開けない"
                    need_restart = (
                        True  # 入力が効かなくなっている可能性がある。次の本の前に起動し直す
                    )
                elif (top := reader_top(hwnd)) is None:
                    row["status"] = "タイトルバーを測れない"
                elif not hide_reader_chrome(hwnd, top=top):
                    row["status"] = "読書 UI を消せない"
                elif reader_top(hwnd) != top:
                    # 読書 UI が出ていた状態で測った高さは信用しない（UI のバーを測っていたかもしれない）
                    row["status"] = "タイトルバーを測れない"
                elif not rewind_to_start(hwnd):
                    row["status"] = "先頭に戻れない"
                elif not verify_title(hwnd, title, top=top):
                    row["status"] = "本を確かめられない"
                else:
                    pages = _Watch()
                    placed = get_window_rect(hwnd)
                    # run_book は**終了コード**を返す（真偽値ではない）
                    code = run_book(
                        title=title,
                        output=args.library,
                        profile_key="kindle",
                        config=_config(),
                        # **asin を渡さない。** 渡すと run_book が Cloud Reader 用の「URL で開いて
                        # F11」を前面のウィンドウ（＝アプリ）に対して行い、全画面になって
                        # 2560x1440 の断片が撮れる（実測）。本はこのスクリプトが開いている。
                        # 表紙は先頭まで戻して撮った 1 ページ目がそのまま表紙なので取りに行かない
                        asin=None,
                        no_cover=True,
                        fmt=args.fmt or book.get("format", "searchable_pdf"),
                        headless=False,
                        page_turn="right",  # アプリは縦書きでも → が次ページ
                        # タイトルバーが出ている表示のときだけ、その高さを削る
                        min_margins=(0, 0, top + 2, 0) if top else MIN_MARGINS,
                        max_pages=args.max_pages,
                        # 失敗した本は次にやり直す。途中で終わった残骸画像は消して撮り直す
                        # （完成した本は pending() が先に飛ばしている）
                        overwrite=True,
                        no_toc_bookmarks=True,  # 非対応の本なので Kindle の目次は取れない
                        emit=pages,
                    )
                    row["pages"] = pages.total
                    row["stopped_reason"] = pages.stopped
                    row["detail"] = pages.error or (f"exit={code}" if code else "")
                    if code != EXIT_OK:
                        row["status"] = "失敗"
                    elif (moved := get_window_rect(hwnd)) != placed:
                        # 撮っている間にウィンドウが最大化・移動されると、拡大された断片が
                        # ページとして入る（実測: 2448x1360 のページになった）。完成扱いにしない
                        row["status"] = "失敗"
                        row["detail"] = f"撮影中にウィンドウが動いた {placed} → {moved}"
                    elif (
                        title_bar_height(_shot(hwnd)) != top
                        or chrome_hidden(hwnd, top=top) is not True
                    ):
                        # 表示の切り替わりの条件が分かっていないので、撮り終えたあとに測り直す。
                        # 途中で変わっていれば、上が欠けた／UI の写ったページが混ざっている
                        row["status"] = "失敗"
                        row["detail"] = "撮影中に表示が変わった（タイトルバーか読書 UI）"
                    elif pages.total < MIN_PAGES and not args.allow_partial:
                        # 撮れていない本を完成扱いにしない（次からずっと飛ばされる）
                        row["status"] = "ページが少なすぎる"
                        row["detail"] = f"{pages.total} ページ"
                    elif args.max_pages:
                        # 試し撮りを完了として記録しない（本番の state に混ざると永久に飛ばされる）
                        row["status"] = "試し撮り"
                    else:
                        row["status"] = DONE
            except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
                row["status"] = "失敗"
                row["detail"] = f"{type(exc).__name__}: {exc}"
                # 置けない・窓が見つからない等は、起動し直しが唯一の復旧手段
                need_restart = True
            if row["status"] != DONE:
                # **完了でない本の PDF を蔵書に残さない。** run_book は撮れた分の PDF を蔵書に
                # 書いてから返るので、そのままだと pending() が次から飛ばし、断片・少ページの本が
                # 蔵書に固定される。作業フォルダに退避する（消さない）
                quarantined = quarantine(args.library, title, os.path.dirname(args.state))
                if quarantined:
                    row["detail"] = f"{row.get('detail', '')} PDF を {quarantined} へ退避".strip()
            row["seconds"] = round(time.time() - started, 1)
            writer.writerow(row)
            state.flush()
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            print(
                f"  → {row['status']} {row.get('pages', '')} ページ "
                f"({row['seconds']} 秒) {row.get('detail', '')}",
                flush=True,
            )

    done = counts.get(DONE, 0)
    print(
        f"完了: {len(todo)} 冊中 {done} 冊を撮った。内訳 {counts}。一覧: {args.state}", flush=True
    )
    # 撮れなかった本があるのに 0 で返すと、無人実行で異常に気づけない
    return 0 if done == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
