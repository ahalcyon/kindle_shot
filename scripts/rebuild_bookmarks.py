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

Cloud Reader 非対応の本（Kindle アプリでしか開けない）は、しおりを作れないので読み飛ばす。
直しようが無いので失敗には数えない（終了コードには出ない）。どの本を読み飛ばしたかは一覧の
status 列と完了行の内訳で分かる。

1 冊でも失敗があれば終了コード 1。続けて `MAX_CONSECUTIVE_FAILURES` 冊、取れなかった本
（失敗・非対応）が並んだら打ち切る（サインイン切れなどで全冊が同じ理由で落ちるのを、
ブラウザを何百回も起動して並べない）。**非対応でもブラウザは開いている**ので、続くなら
打ち切る理由は同じ。しかもこのツールが見るのは PDF がある本＝かつて撮れた本なので、
非対応が続くこと自体がおかしい。

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
from core.bookmark_rebuild import (  # noqa: E402
    UnsupportedBook,
    cached_structure,
    plan_book,
    rebuild_book,
)

# 続けて失敗したら打ち切る冊数
MAX_CONSECUTIVE_FAILURES = 5
# rebuild_book が書かなかった理由 → 一覧の status
APPLY_STATUS = {
    "しおりを付けられない": "しおりを付けられないので未適用",
    "要確認": "要確認のため未適用",
}

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
    # 列を足すときは末尾に（過去の一覧と列の位置で突き合わせる手作業を壊さない）
    "running_head",
]


def load_books(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


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
            failed = stalled = False
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
                    else:
                        # 判定は core と同じものを使う（2 か所に書くと片方だけ直して食い違う）
                        written = rebuild_book(pdf, structure, include_flagged=args.include_flagged)
                        if written["written"]:
                            row["status"] = "書き換えた"
                        elif written["reason"] in APPLY_STATUS:
                            row["status"] = APPLY_STATUS[written["reason"]]
                        else:
                            row["status"] = f"失敗: {written['reason']}"
                            failed = stalled = True
                except UnsupportedBook as exc:
                    # 撮影のバッチと同じ扱い。直しようが無いので失敗に数えない。
                    # ただし打ち切りの判定では「取れなかった本」として数える
                    row["status"] = f"Cloud Reader 非対応: {exc}"
                    stalled = True
                except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
                    row["status"] = f"失敗: {type(exc).__name__}: {exc}"
                    failed = stalled = True
            writer.writerow(row)
            report.flush()
            statuses[row["status"].split(":")[0]] += 1
            failures += failed
            consecutive = consecutive + 1 if stalled else 0
            print(
                f"[{n}/{len(books)}] {row['status']} {row.get('flags', '')} {title[:40]}",
                flush=True,
            )
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"{consecutive} 冊続けて取れなかったので打ち切ります", flush=True)
                break

    print(f"完了: {dict(statuses)}、要確認 {flagged} 冊、失敗 {failures} 冊。一覧: {args.report}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
