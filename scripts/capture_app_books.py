"""Kindle アプリ（Microsoft Store 版）で本を開いて撮る (#74)

**Cloud Reader 非対応の本を撮る唯一の道。** 蔵書 405 冊のうち 56 冊は
`Kindle App Is Required` で Cloud Reader が開けず、1 ページも撮れていない。

実測で分かったこと（2026-09-18。ウィンドウ 1200x1390）:

- Store 版の実行ファイルは `Kindle.exe` で、`kindle` プロファイルがそのまま使える
- **本は「ライブラリの検索窓に題名を入れて 1 件目をクリック」でしか開けない。**
  `kindle://book?action=open&asin=...` は何も起きない（実測）
- **F11 の全画面では UI が一切消える**が、画面が横長だと 2 段組（見開き）になる。
  幅 1200 なら 1 段組＝ 1 ページになるので、こちらを使う（見開きを割るより素直）
- ウィンドウ表示の UI（タイトルバー・書名ヘッダー・ページ番号フッター）は
  `--min-margins 0,0,115,60` で削れる
- ページ送りは **→ が常に「次のページ」**（アプリのショートカット一覧）。縦書きでも
  変わらないので、Cloud Reader のような向きの判定（#111）が要らない

**開いた本が違っていないかを、書名ヘッダーの OCR で確かめてから撮る。**
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

from core.book_format import book_pdf_path, load_books  # noqa: E402
from core.console import setup_stdio  # noqa: E402

# ウィンドウの大きさ。幅 1200 で 1 段組（1400 以上だと 2 段組になる。実測）
WIDTH, HEIGHT = 1200, 1390
# ライブラリ画面の位置（上の大きさのとき）。実測で決めた
SEARCH_BOX = (125, 96)
FIRST_COVER = (358, 305)
SECOND_COVER = (599, 305)
# ライブラリにいるかの目印。左の「全て」の行が青く反転している
LIBRARY_MARK = (125, 243)
# 本を閉じてライブラリに戻る矢印（本を開いているときだけ出る）
BACK_ARROW = (32, 32)

# 最低限削る余白。**アプリのタイトルバーだけ**（実測で高さ 62px）。
# 書名ヘッダーとページ番号フッターは本によって有無が違う（固定レイアウトの本には無い。
# 実測: Beginning in Algebraic Geometry）ので、ここで一律に削ると中身を切る。
# 残りはページ間の変化から UI 帯を見つける仕組み（run の ui_bands）に任せる
MIN_MARGINS = (0, 0, 65, 0)
COLUMNS = ["asin", "title", "status", "pages", "seconds", "detail"]


def _norm(text):
    """題名を突き合わせるための正規化（記号と空白を落とす）。"""
    import re
    import unicodedata

    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text or "")).lower()


def title_matches(want, seen, *, head=8):
    """OCR で読んだ文字が、開きたかった本のものか。

    見るのは 2 通り。**書名ヘッダー**（題名が途中で切れる）と、**表紙のページ全体**
    （表紙にはヘッダーが出ないので、題名が絵の中の文字として入っている）。
    どちらでも効くよう、正規化した先頭 head 文字が相手に含まれていれば同じ本とみなす。
    副題・出版社名・レーベル名の違いで落とさない。
    """
    a, b = _norm(want), _norm(seen)
    if not a or not b:
        return False
    return a[:head] in b or b[:head] in a


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
    from core.win32_utils import activate_window, find_window

    hwnd = find_window("kindle", process_name="Kindle.exe")
    if not hwnd:
        raise RuntimeError("Kindle アプリのウィンドウが見つかりません")
    activate_window(hwnd, click_position="none")
    time.sleep(0.8)
    return hwnd


def _place(hwnd, width=WIDTH, height=HEIGHT):
    """撮影する大きさ・位置にウィンドウを置く（1 段組にするため）。"""
    import ctypes

    ctypes.windll.user32.SetWindowPos(hwnd, None, -1600, 0, width, height, 0x0040)
    time.sleep(1.5)


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
        )
    finally:
        os.unlink(path)


def _cover_box(point, size=60):
    x, y = point
    return (x - size, y - size, x + size, y + size)


def is_library(image):
    """ライブラリ画面にいるか（左の「全て」の行が青い）。

    **画面がどこにいるかを確かめずに操作しない。** 本を開いたままだと、検索窓のつもりの
    クリックが本文に当たり、Ctrl+A が「読書補助機能」になってダイアログが開く（実測）。
    """
    from PIL import ImageStat

    r, g, b = ImageStat.Stat(image.convert("RGB")).mean
    return b > r + 40 and b > 120


def _to_library(hwnd, *, tries=3):
    """どの画面にいても、ライブラリに戻す。戻せなければ False。"""
    import pyautogui

    for _ in range(tries):
        for _ in range(2):
            pyautogui.press("esc")  # 開いたままのダイアログを閉じる
            time.sleep(0.4)
        if is_library(_shot(hwnd, _cover_box(LIBRARY_MARK, 12))):
            return True
        pyautogui.hotkey("ctrl", "w")
        time.sleep(2.0)
        if is_library(_shot(hwnd, _cover_box(LIBRARY_MARK, 12))):
            return True
        _click(hwnd, *BACK_ARROW, wait=2.0)
    return is_library(_shot(hwnd, _cover_box(LIBRARY_MARK, 12)))


def open_book(hwnd, title, *, emit=print):
    """ライブラリで題名を検索し、**1 件だけに絞れたとき**その本を開く。

    2 件以上に絞れなかった本は開かない（別の本を蔵書に入れない）。
    """
    import pyautogui

    if not _to_library(hwnd):
        emit("  ライブラリに戻れない")
        return False
    _to_clipboard(title)
    _click(hwnd, *SEARCH_BOX, wait=0.6)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.6)
    pyautogui.press("enter")
    time.sleep(3.0)
    if not is_cover(_shot(hwnd, _cover_box(FIRST_COVER))):
        emit("  検索に当たらない")
        return False
    if is_cover(_shot(hwnd, _cover_box(SECOND_COVER))):
        emit("  検索が 2 冊以上に当たる")
        return False
    _click(hwnd, *FIRST_COVER, wait=10.0)
    if is_library(_shot(hwnd, _cover_box(LIBRARY_MARK, 12))):
        emit("  本が開かない")
        return False
    return True


def _digest(image):
    import hashlib

    return hashlib.sha1(image.convert("L").resize((160, 180)).tobytes()).hexdigest()


def rewind_to_start(hwnd, *, max_presses=400, step=5, emit=print):
    """本の先頭まで戻す。戻り切ったら True。

    **画面キャプチャ経路には「先頭から撮る」処理が無い**（headless 経路の `max_rewind` は
    Cloud Reader を開くときの話）。アプリは前回の続きから開くので、戻さずに撮ると
    途中から始まる。実測: 8 ページ撮ったら xiii ページから始まった。
    """
    import pyautogui

    before = _digest(_shot(hwnd))
    for _pressed in range(0, max_presses, step):
        for _ in range(step):
            pyautogui.press("left")
            time.sleep(0.12)
        time.sleep(0.6)
        after = _digest(_shot(hwnd))
        if after == before:
            return True
        before = after
    emit(f"  先頭まで戻り切らない（{max_presses} 回）")
    return False


class _Watch:
    """run_book のイベントから、ページ数と**失敗の理由**を拾う。

    無人で何十冊も回すので、落ちた理由が一覧に残らないと原因を追えない。
    """

    def __init__(self):
        self.total = 0
        self.error = ""

    def __call__(self, event, **kw):
        if event == "capture_stopped":
            self.total = kw.get("total_pages", self.total)
        elif event == "result" and kw.get("total_pages"):
            self.total = kw["total_pages"]
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
    ap.add_argument("--format", dest="fmt", default=None, help="既定は books.json の format")
    ap.add_argument("--dry-run", action="store_true", help="撮らずに、対象の一覧だけ出す")
    args = ap.parse_args(argv)

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

    exists = os.path.exists(args.state)
    failures = 0
    with open(args.state, "a" if exists else "w", encoding="utf-8-sig", newline="") as state:
        writer = csv.DictWriter(state, fieldnames=COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for n, book in enumerate(todo, 1):
            title, asin = book.get("title", ""), book.get("asin", "")
            row = {"asin": asin, "title": title}
            started = time.time()
            print(f"[{n}/{len(todo)}] {title[:46]}", flush=True)
            try:
                hwnd = _app()
                _place(hwnd)
                if not open_book(hwnd, title):
                    row["status"] = "開けない"
                elif not rewind_to_start(hwnd):
                    row["status"] = "先頭に戻れない"
                else:
                    pages = _Watch()
                    # run_book は**終了コード**を返す（真偽値ではない）
                    code = run_book(
                        title=title,
                        output=args.library,
                        profile_key="kindle",
                        fmt=args.fmt or book.get("format", "searchable_pdf"),
                        headless=False,
                        min_margins=MIN_MARGINS,
                        max_pages=args.max_pages,
                        no_toc_bookmarks=True,  # 非対応の本なので Kindle の目次は取れない
                        emit=pages,
                    )
                    row["status"] = "完了" if code == EXIT_OK else "失敗"
                    row["pages"] = pages.total
                    row["detail"] = pages.error or (f"exit={code}" if code else "")
                    failures += code != EXIT_OK
            except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
                row["status"] = "失敗"
                row["detail"] = f"{type(exc).__name__}: {exc}"
                failures += 1
            row["seconds"] = round(time.time() - started, 1)
            writer.writerow(row)
            state.flush()
            print(
                f"  → {row['status']} {row.get('pages', '')} ページ "
                f"({row['seconds']} 秒) {row.get('detail', '')}",
                flush=True,
            )

    print(f"完了: {len(todo)} 冊中 {failures} 冊が失敗。一覧: {args.state}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
