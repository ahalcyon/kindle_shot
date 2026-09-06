"""Kindle Cloud Reader のライブラリから蔵書一覧を取得する

`cli.py batch` は ASIN とタイトルのリスト (books.json) を必要とするが、
その ASIN を集める手段が無かった。数百冊を 1 冊ずつ商品ページで調べるのは
現実的でないため、ログイン済みの headless ブラウザでライブラリページから
そのまま吸い出す。

DOM の構造 (実測):
    div[id="coverContainer-<ASIN>"]  innerText がタイトル
    img[id="cover-<ASIN>"]

初期表示は 50 件で、`main#library` を末尾までスクロールすると追加読み込み
される。ウィンドウのスクロールでは増えない。
"""

import json
import os
import re

from core.pipeline import EXIT_ERROR, EXIT_OK, emit_error, null_emit

LIBRARY_URL = "https://read.amazon.co.jp/kindle-library"

# 蔵書の各項目。id から ASIN を、innerText からタイトルを取る。
ITEMS_JS = """() => Array.from(document.querySelectorAll('[id^="coverContainer-"]')).map(el => ({
    asin: el.id.replace('coverContainer-', ''),
    title: (el.innerText || '').trim(),
}))"""

# ライブラリのスクロール要素。ウィンドウではなくここを送らないと追加読み込みされない。
SCROLL_JS = """() => {
    const el = document.querySelector('main#library');
    if (!el) return false;
    el.scrollTop = el.scrollHeight;
    return true;
}"""

# 保存フォルダ名・ファイル名になるので、付いていても意味のない版表記は落とす
_TITLE_NOISE = re.compile(r"\s*\((?:Japanese Edition|Japanese|English Edition)\)\s*$", re.I)


def clean_title(title):
    """タイトルから版表記を落とす。"""
    return _TITLE_NOISE.sub("", (title or "").strip()).strip()


def dedupe(items):
    """ASIN で一意化する。同じ本の複数版が並ぶことがある。"""
    seen = set()
    result = []
    for item in items:
        asin = item.get("asin")
        if not asin or asin in seen:
            continue
        seen.add(asin)
        result.append(item)
    return result


def collect_items(page, *, scroll_wait=1200, max_scrolls=200, emit=null_emit):
    """ライブラリを末尾までスクロールして全件を集める。

    件数が増えなくなったら終わり。max_scrolls は暴走防止の上限。
    """
    previous = -1
    items = []
    for _ in range(max_scrolls):
        items = page.evaluate(ITEMS_JS)
        if len(items) == previous:
            break
        previous = len(items)
        emit("library_progress", human=f"読み込み中... {len(items)} 冊", count=len(items))
        if not page.evaluate(SCROLL_JS):
            emit("status", human="ライブラリのスクロール要素が見つかりません")
            break
        page.wait_for_timeout(scroll_wait)
    return items


def to_books(items, *, keep_edition=False):
    """batch がそのまま読める形にする。"""
    books = []
    for item in dedupe(items):
        title = item["title"] if keep_edition else clean_title(item["title"])
        if not title:
            continue
        books.append({"title": title, "asin": item["asin"]})
    return books


def run_library_dump(
    output,
    *,
    url=None,
    keep_edition=False,
    profile_dir=None,
    headless=True,
    load_wait=14,
    emit=null_emit,
):
    """ライブラリを走査して books.json を書き出す。

    Returns:
        終了コード
    """
    from core.headless_browser import open_reader

    target = url or LIBRARY_URL
    with open_reader(target, profile_dir=profile_dir, headless=headless, emit=emit) as page:
        if page is None:
            return EXIT_ERROR
        page.wait_for_timeout(int(load_wait * 1000))
        items = collect_items(page, emit=emit)

    books = to_books(items, keep_edition=keep_edition)
    if not books:
        emit_error(emit, "蔵書を 1 冊も取得できませんでした")
        return EXIT_ERROR

    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(books, f, indent=2, ensure_ascii=False)

    emit(
        "result",
        human=f"蔵書 {len(books)} 冊を書き出しました: {output}",
        ok=True,
        count=len(books),
        output=output,
    )
    return EXIT_OK
