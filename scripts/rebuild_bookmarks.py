"""蔵書の PDF のしおりを、Kindle の本が持つ目次で作り直す (#114)

OCR から推測したしおりは崩れやすい（`core/kindle_toc.py` の説明）。ここでは本を
Cloud Reader で開き、描画 API から目次と全ページの位置範囲を取って、しおりだけを
付け直す。**ページの画像とテキスト層には触らない。** 読書位置も動かさない。

既定は**試し実行**で、蔵書は書き換えない。本ごとの結果を一覧（CSV）に 1 冊ずつ追記する。
`--apply` を付けたときだけ書き換える。

- **要確認の印が付いた本は書き換えない。** 書き換えるには `--include-flagged` を付ける
- 書き換えは検証（ページ数・描画結果・テキストが変わっていないこと、書いたしおりを読み戻して
  一致すること）に通った本だけで、通らなければ元のファイルを残す

描画 API から取った内容は `--cache` に本ごとに保存し、2 回目以降は開き直さない。
本の終わりまで取れなかったもの（`complete` が偽）は保存しない。`--refresh` で取り直す。

Cloud Reader 非対応の本（Kindle アプリでしか開けない）は、しおりを作れないので読み飛ばす
（失敗には数えない）。1 冊でも失敗があれば終了コード 1。続けて `MAX_CONSECUTIVE_FAILURES` 冊失敗したら打ち切る
（サインイン切れなどで全冊が同じ理由で落ちるのを、ブラウザを何百回も起動して並べない）。

使い方:

    # 試し実行（一覧を出すだけ）
    python scripts/rebuild_bookmarks.py --books books_typed.json --library <蔵書> \\
        --cache <作業フォルダ>/toc_cache --report <作業フォルダ>/bookmarks.csv

    # 要確認の印が無い本を書き換える
    python scripts/rebuild_bookmarks.py ... --apply

    # 要確認の本を見たうえで、指定した本だけ書き換える
    python scripts/rebuild_bookmarks.py ... --apply --include-flagged --asin B0XXXXXXXX
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
    CONFIRMED,
    MOVED,
    UNCONFIRMED,
    UNSEARCHED,
    flatten_toc,
    map_to_pages,
    order_outliers,
    page_offset,
    write_outline,
)

# テキスト層があるとみなす、抜き取りページの合計文字数
TEXT_LAYER_MIN_CHARS = 50
# 並びから外れた項目を除いてよい上限。実測では外れるのは「目次」「Cover」の 1 項目だった。
# これを超えて外れる目次は、対応づけの前提（目次の順＝ページの順）が崩れている
MAX_ORDER_OUTLIERS = 2
MAX_ORDER_OUTLIER_RATIO = 0.1
# 続けて失敗したら打ち切る冊数
MAX_CONSECUTIVE_FAILURES = 5

# 一覧の「要確認」の理由
FLAG_NO_TOC = "目次なし"
FLAG_NO_PAGES = "ページ範囲が取れない"
FLAG_TOC_ORDER = "目次の位置の並びが大きく崩れている"
FLAG_COUNT = "ページ数の差が表紙で説明できない"
FLAG_UNRENDERABLE = "描画できない区間あり"
FLAG_INCOMPLETE = "本の終わりまで走査できていない"
FLAG_NO_TEXT = "テキスト層なし（位置だけで決める）"
# 表紙の分と違うずれ幅をテキストから選んだ区間がある。目視では多くが正しいが、選んだずれ幅で
# 見つかった項目も confirmed に数えるので、confirmed だけでは確かめたことにならない
FLAG_SHIFTED = "テキストからずれ幅を選んだ区間あり"

# これが立っている本はしおりを付けられない（--include-flagged でも書かない）
BLOCKING_FLAGS = {FLAG_NO_TOC, FLAG_NO_PAGES, FLAG_TOC_ORDER}

COLUMNS = [
    "asin",
    "title",
    "format",
    "pdf_pages",
    "render_pages",
    "shifts",
    "toc_entries",
    "dropped",
    "old_bookmarks",
    "confirmed",
    "moved",
    "unconfirmed",
    "unsearched",
    "flags",
    "status",
]


class UnsupportedBook(Exception):
    """Cloud Reader 非対応の本（Kindle アプリでしか開けない）。しおりは作れない。"""


def load_books(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def cached_structure(cache_dir, asin, *, profile_dir=None, refresh=False):
    """描画 API から取った目次とページ範囲。キャッシュが無ければ本を開いて取る。"""
    path = os.path.join(cache_dir, f"{asin}.json")
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            structure = json.load(f)
        if structure.get("complete"):
            return structure
    from core.headless_browser import open_reader
    from core.headless_capture import BOOK_URL, unsupported_reason
    from core.kindle_toc import fetch_book_structure

    with open_reader(BOOK_URL.format(asin=asin), headless=True, profile_dir=profile_dir) as page:
        if page is None:
            raise RuntimeError("本を開けませんでした")
        # 撮影済みでも、いま非対応になっている本がある（B071GN3JN2。#114 のコメントに実測）。
        # 描画 API の要求が出ないので、そのままだと「描画要求が出ませんでした」で失敗に数える
        reason = unsupported_reason(page)
        if reason:
            raise UnsupportedBook(reason)
        structure = fetch_book_structure(page)
    if structure.get("complete"):
        # 途中で落ちても壊れた JSON を残さない
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(structure, f, ensure_ascii=False)
        os.replace(tmp, path)
    return structure


def count_outline(reader):
    def walk(items):
        return sum(walk(i) if isinstance(i, list) else 1 for i in items)

    try:
        return walk(reader.outline)
    except Exception:  # noqa: BLE001 - 壊れたしおりは 0 として数える
        return 0


def shift_summary(entries):
    """区間ごとのずれ幅を、本の先頭からの並びで返す（例: ``2→0→2``）。"""
    out: list[str] = []
    for e in entries:
        if not out or out[-1] != str(e.shift):
            out.append(str(e.shift))
    return "→".join(out)


def plan_book(pdf_path, structure):
    """1 冊分の対応づけと、一覧の 1 行ぶんの数字を作る。

    Returns:
        (entries, row, flags)。flags に BLOCKING_FLAGS のどれかがあれば entries は書けない。
    """
    from pypdf import PdfReader

    from core.text_layer import _sample_indexes

    reader = PdfReader(pdf_path)
    pdf_pages = len(reader.pages)
    ranges = structure.get("pages") or []
    render_pages = len(ranges)
    flags = []

    entries = flatten_toc(structure.get("toc"))
    if not entries:
        flags.append(FLAG_NO_TOC)
    if not ranges:
        flags.append(FLAG_NO_PAGES)
    dropped = 0
    if entries:
        outliers = order_outliers(entries)
        dropped = len(outliers)
        if dropped > max(MAX_ORDER_OUTLIERS, len(entries) * MAX_ORDER_OUTLIER_RATIO):
            flags.append(FLAG_TOC_ORDER)
        else:
            # 並びから外れた項目（別の場所を指す「目次」「Cover」など）だけ外して付ける
            skip = set(outliers)
            entries = [e for i, e in enumerate(entries) if i not in skip]
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

    text = page_text if has_text else None
    mappable = not (BLOCKING_FLAGS & set(flags))
    if mappable:
        # ずれ幅は区間ごとにテキストで決まる（core.kindle_toc.map_to_pages）。表紙の分は既定値
        map_to_pages(entries, ranges, pdf_pages, text, offset or 0)
        if offset is not None and any(e.shift != offset for e in entries):
            flags.append(FLAG_SHIFTED)
    hows = Counter(e.how for e in entries) if mappable else Counter()
    row = {
        "pdf_pages": pdf_pages,
        "render_pages": render_pages,
        "shifts": shift_summary(entries) if mappable else "",
        "toc_entries": len(entries),
        "dropped": dropped,
        "old_bookmarks": count_outline(reader),
        "confirmed": hows[CONFIRMED],
        "moved": hows[MOVED],
        "unconfirmed": hows[UNCONFIRMED],
        "unsearched": hows[UNSEARCHED],
        "flags": " / ".join(flags),
    }
    return entries, row, flags


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--books", required=True, help="books.json（title / asin / format）")
    ap.add_argument("--library", required=True, help="蔵書フォルダ（撮影時の --out と同じ場所）")
    ap.add_argument("--cache", required=True, help="描画 API から取った内容を置くフォルダ")
    ap.add_argument("--report", required=True, help="一覧（CSV）の出力先")
    ap.add_argument("--apply", action="store_true", help="しおりを書き換える（既定は試し実行）")
    ap.add_argument(
        "--include-flagged",
        action="store_true",
        help="要確認の印が付いた本も書き換える（付けられない本は除く）",
    )
    ap.add_argument("--refresh", action="store_true", help="キャッシュを使わず取り直す")
    ap.add_argument("--asin", action="append", help="この ASIN の本だけ（複数指定可）")
    ap.add_argument("--profile-dir", help="ブラウザプロファイル（省略時は既定）")
    args = ap.parse_args(argv)

    books = load_books(args.books)
    if args.asin:
        wanted = set(args.asin)
        books = [b for b in books if b.get("asin") in wanted]

    os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
    statuses: Counter = Counter()
    failures = consecutive = flagged = 0
    with open(args.report, "w", encoding="utf-8-sig", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for n, book in enumerate(books, 1):
            title, asin = book.get("title", ""), book.get("asin", "")
            row = {"asin": asin, "title": title, "format": book.get("format", "")}
            pdf = book_pdf_path(args.library, title)
            failed = False
            if not os.path.exists(pdf):
                row["status"] = "PDF なし"
            else:
                try:
                    structure = cached_structure(
                        args.cache, asin, profile_dir=args.profile_dir, refresh=args.refresh
                    )
                    entries, numbers, flags = plan_book(pdf, structure)
                    row.update(numbers)
                    flagged += bool(flags)
                    os.makedirs(args.cache, exist_ok=True)
                    with open(
                        os.path.join(args.cache, f"{asin}.plan.json"), "w", encoding="utf-8"
                    ) as f:
                        json.dump([e.__dict__ for e in entries], f, ensure_ascii=False, indent=1)
                    if not args.apply:
                        row["status"] = "試し実行"
                    elif BLOCKING_FLAGS & set(flags):
                        row["status"] = "しおりを付けられないので未適用"
                    elif flags and not args.include_flagged:
                        row["status"] = "要確認のため未適用"
                    else:
                        result = write_outline(pdf, entries)
                        if result["ok"]:
                            row["status"] = "書き換えた"
                        else:
                            row["status"] = f"失敗: {result.get('error')}"
                            failed = True
                except UnsupportedBook as exc:
                    # 撮影のバッチと同じ扱い。直しようが無いので失敗に数えない
                    row["status"] = f"Cloud Reader 非対応: {exc}"
                except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
                    row["status"] = f"失敗: {type(exc).__name__}: {exc}"
                    failed = True
            writer.writerow(row)
            report.flush()
            statuses[row["status"].split(":")[0]] += 1
            failures += failed
            consecutive = consecutive + 1 if failed else 0
            print(
                f"[{n}/{len(books)}] {row['status']} {row.get('flags', '')} {title[:40]}",
                flush=True,
            )
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"{consecutive} 冊続けて失敗したので打ち切ります", flush=True)
                break

    print(f"完了: {dict(statuses)}、要確認 {flagged} 冊、失敗 {failures} 冊。一覧: {args.report}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
