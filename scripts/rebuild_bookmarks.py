"""蔵書の PDF のしおりを、Kindle の本が持つ目次で作り直す (#114)

OCR から推測したしおりは崩れやすい（`core/kindle_toc.py` の説明）。ここでは本を
Cloud Reader で開き、描画 API から目次と全ページの位置範囲を取って、しおりだけを
付け直す。**ページの画像とテキスト層には触らない。** 読書位置も動かさない。

既定は**試し実行**で、蔵書は書き換えない。本ごとの結果を一覧（CSV）に出す。
`--apply` を付けたときだけ書き換える。書き換えは検証（ページ数・描画結果・
テキストが変わっていないこと）に通った本だけで、通らなければ元のファイルを残す。

描画 API から取った内容は `--cache` に本ごとに保存し、2 回目以降は開き直さない。

使い方:

    # 試し実行（一覧を出すだけ）
    python scripts/rebuild_bookmarks.py --books books_typed.json --library <蔵書> \\
        --cache <作業フォルダ>/toc_cache --report <作業フォルダ>/bookmarks.csv

    # 指定した本だけ書き換える
    python scripts/rebuild_bookmarks.py ... --apply --asin B0XXXXXXXX --asin B0YYYYYYYY
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.book_format import book_pdf_path  # noqa: E402
from core.kindle_toc import (  # noqa: E402
    EXACT,
    MOVED,
    UNCONFIRMED,
    flatten_toc,
    map_to_pages,
    page_offset,
    write_outline,
)

# テキスト層があるとみなす、抜き取りページの合計文字数
TEXT_LAYER_MIN_CHARS = 50

# 一覧の「要確認」の理由
FLAG_NO_TOC = "目次なし"
FLAG_COUNT = "ページ数の差が表紙で説明できない"
FLAG_UNRENDERABLE = "描画できない区間あり"
FLAG_INCOMPLETE = "本の終わりまで走査できていない"
FLAG_NO_TEXT = "テキスト層なし（位置だけで決める）"

COLUMNS = [
    "asin",
    "title",
    "format",
    "pdf_pages",
    "render_pages",
    "offset",
    "toc_entries",
    "old_bookmarks",
    "exact",
    "moved",
    "unconfirmed",
    "flags",
    "status",
]


def load_books(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def cached_structure(cache_dir, asin, *, profile_dir=None):
    """描画 API から取った目次とページ範囲。キャッシュが無ければ本を開いて取る。"""
    path = os.path.join(cache_dir, f"{asin}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    from core.headless_browser import open_reader
    from core.headless_capture import BOOK_URL
    from core.kindle_toc import fetch_book_structure

    with open_reader(BOOK_URL.format(asin=asin), headless=True, profile_dir=profile_dir) as page:
        if page is None:
            raise RuntimeError("本を開けませんでした")
        structure = fetch_book_structure(page)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(structure, f, ensure_ascii=False)
    return structure


def count_outline(reader):
    def walk(items):
        return sum(walk(i) if isinstance(i, list) else 1 for i in items)

    try:
        return walk(reader.outline)
    except Exception:  # noqa: BLE001 - 壊れたしおりは 0 として数える
        return 0


# ずらし幅を試す上限。差し込んだページの数ぶん。大きい差は別の原因なので試さない
MAX_OFFSET_TRIAL = 3


def choose_offset(entries, starts, pdf_pages, page_text, candidates):
    """テキストで章名を確かめられた項目が最も多いずらし幅を返す。テキストが無ければ 0。"""
    import copy

    if page_text is None or not entries or not starts:
        return 0
    best, best_score = 0, -1
    for offset in candidates:
        trial = map_to_pages(copy.deepcopy(entries), starts, pdf_pages, page_text, offset)
        score = sum(1 for e in trial if e.how == EXACT)
        if score > best_score:
            best, best_score = offset, score
    return best


def plan_book(pdf_path, structure):
    """1 冊分の対応づけと、一覧の 1 行ぶんの数字を作る。"""
    from pypdf import PdfReader

    from core.text_layer import _sample_indexes

    reader = PdfReader(pdf_path)
    pdf_pages = len(reader.pages)
    render_pages = len(structure.get("pages") or [])
    flags = []

    entries = flatten_toc(structure.get("toc"))
    if not entries:
        flags.append(FLAG_NO_TOC)
    offset = page_offset(pdf_pages, render_pages)
    if offset is None:
        flags.append(FLAG_COUNT)
    if structure.get("unrenderable"):
        flags.append(FLAG_UNRENDERABLE)
    if not structure.get("complete"):
        flags.append(FLAG_INCOMPLETE)

    sample = _sample_indexes(pdf_pages, size=6)
    chars = sum(len((reader.pages[i].extract_text() or "").strip()) for i in sample)
    has_text = chars >= TEXT_LAYER_MIN_CHARS
    if not has_text:
        flags.append(FLAG_NO_TEXT)

    cache: dict[int, str] = {}

    def page_text(i):
        if i not in cache:
            cache[i] = reader.pages[i].extract_text() or ""
        return cache[i]

    starts = [p[0] for p in structure.get("pages") or []]
    if offset is None:
        # 差が表紙で説明できない本（途中に画像を差し込んだ本など）。ずらし幅の候補を
        # 全部試し、テキストで章名を確かめられた項目が最も多いものを採る
        candidates = range(0, min(max(pdf_pages - render_pages, 0), MAX_OFFSET_TRIAL) + 1)
        offset = choose_offset(
            entries, starts, pdf_pages, page_text if has_text else None, candidates
        )
    if entries and starts:
        map_to_pages(entries, starts, pdf_pages, page_text if has_text else None, offset)
    hows = Counter(e.how for e in entries)
    row = {
        "pdf_pages": pdf_pages,
        "render_pages": render_pages,
        "offset": offset,
        "toc_entries": len(entries),
        "old_bookmarks": count_outline(reader),
        "exact": len(entries) - hows[MOVED] - hows[UNCONFIRMED],
        "moved": hows[MOVED],
        "unconfirmed": hows[UNCONFIRMED],
        "flags": " / ".join(flags),
    }
    return entries, row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--books", required=True, help="books.json（title / asin / format）")
    ap.add_argument("--library", required=True, help="蔵書フォルダ（撮影時の --out と同じ場所）")
    ap.add_argument("--cache", required=True, help="描画 API から取った内容を置くフォルダ")
    ap.add_argument("--report", required=True, help="一覧（CSV）の出力先")
    ap.add_argument("--apply", action="store_true", help="しおりを書き換える（既定は試し実行）")
    ap.add_argument("--asin", action="append", help="この ASIN の本だけ（複数指定可）")
    ap.add_argument("--profile-dir", help="ブラウザプロファイル（省略時は既定）")
    args = ap.parse_args(argv)

    books = load_books(args.books)
    if args.asin:
        wanted = set(args.asin)
        books = [b for b in books if b.get("asin") in wanted]

    rows = []
    for n, book in enumerate(books, 1):
        title, asin = book.get("title", ""), book.get("asin", "")
        row = {"asin": asin, "title": title, "format": book.get("format", "")}
        pdf = book_pdf_path(args.library, title)
        if not os.path.exists(pdf):
            row["status"] = "PDF なし"
            rows.append(row)
            continue
        try:
            structure = cached_structure(args.cache, asin, profile_dir=args.profile_dir)
            entries, numbers = plan_book(pdf, structure)
            row.update(numbers)
            with open(os.path.join(args.cache, f"{asin}.plan.json"), "w", encoding="utf-8") as f:
                json.dump([e.__dict__ for e in entries], f, ensure_ascii=False, indent=1)
            if args.apply and entries:
                result = write_outline(pdf, entries)
                row["status"] = "書き換えた" if result["ok"] else f"失敗: {result.get('error')}"
            else:
                row["status"] = "試し実行"
        except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
            row["status"] = f"失敗: {type(exc).__name__}: {exc}"
        rows.append(row)
        print(f"[{n}/{len(books)}] {row['status']} {row.get('flags', '')} {title[:40]}", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
    with open(args.report, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    statuses = Counter(r["status"].split(":")[0] for r in rows)
    flagged = sum(1 for r in rows if r.get("flags"))
    print(f"完了: {dict(statuses)}、要確認 {flagged} 冊。一覧: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
