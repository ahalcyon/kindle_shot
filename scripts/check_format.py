"""撮影済みの蔵書から、形式の判定が当たっていたかを後から確かめる (#96)

`image_pdf` と判定した本は以後まったく実測されないので、判定が外れていても
気づく道が無かった。ここでは出来上がった PDF を描き直して OCR し、撮影時と同じ
数え方（本文行だけ）で 1 ページあたりの文字数を測って、形式と食い違う本を挙げる。

**蔵書は読むだけで、書き換えない。** 判定が外れていた本をどうするか（撮り直すか、
テキスト層を剥がすか）は人が決める。

1 冊およそ 50 秒（20 ページの描き直し + OCR）。蔵書 175 冊で約 2.4 時間。
結果は 1 冊ずつ CSV に追記するので、途中で止めても続きから再開できる。

使い方:

    python scripts/check_format.py --books books_typed.json --library <蔵書> \\
        --report <作業フォルダ>/format_check.csv

    # 疑いのある本だけ見たい
    python scripts/check_format.py ... --only image_pdf
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.book_format import DEFAULT_THRESHOLD, book_pdf_path  # noqa: E402
from core.console import setup_stdio  # noqa: E402
from core.format_check import OK, SAMPLE_PAGES, measure_pdf, verdict  # noqa: E402

COLUMNS = ["asin", "title", "format", "pdf_pages", "sampled", "chars", "cpp", "verdict", "seconds"]


def main(argv=None):
    # 題名に cp932 で書けない字があっても、進捗の 1 行で一括処理を止めない
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--books", required=True, help="books.json（title / asin / format）")
    ap.add_argument("--library", required=True, help="蔵書フォルダ（撮影時の --out と同じ場所）")
    ap.add_argument("--report", required=True, help="一覧（CSV）の出力先。あれば続きから")
    ap.add_argument("--only", help="この形式の本だけ測る（image_pdf / searchable_pdf）")
    ap.add_argument("--sample", type=int, default=SAMPLE_PAGES, help="抜き取るページ数")
    ap.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="文字数/ページの境目"
    )
    args = ap.parse_args(argv)

    with open(args.books, encoding="utf-8-sig") as f:
        books = json.load(f)
    if args.only:
        books = [b for b in books if b.get("format") == args.only]

    done = set()
    if os.path.exists(args.report):
        with open(args.report, encoding="utf-8-sig", newline="") as f:
            done = {r["asin"] for r in csv.DictReader(f)}
    os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
    suspects = measured = 0
    with open(args.report, "a" if done else "w", encoding="utf-8-sig", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=COLUMNS, extrasaction="ignore")
        if not done:
            writer.writeheader()
        for n, book in enumerate(books, 1):
            title, asin = book.get("title", ""), book.get("asin", "")
            fmt = book.get("format", "")
            if asin in done:
                continue
            pdf = book_pdf_path(args.library, title)
            row = {"asin": asin, "title": title, "format": fmt}
            if not os.path.exists(pdf):
                row["verdict"] = "PDF なし"
            else:
                started = time.time()
                try:
                    row.update(measure_pdf(pdf, sample=args.sample))
                    row["verdict"] = verdict(fmt, row["cpp"], threshold=args.threshold)
                    measured += 1
                    suspects += row["verdict"] != OK
                except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
                    row["verdict"] = f"失敗: {type(exc).__name__}: {exc}"
                row["seconds"] = round(time.time() - started, 1)
            writer.writerow(row)
            report.flush()
            print(
                f"[{n}/{len(books)}] {row['verdict'][:34]:<36} cpp={row.get('cpp', '-'):>7} "
                f"{title[:32]}",
                flush=True,
            )

    print(f"完了: {measured} 冊を測り、{suspects} 冊が形式と食い違う。一覧: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
