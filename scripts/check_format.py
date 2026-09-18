"""撮影済みの蔵書から、形式の判定が当たっていたかを後から確かめる (#96)

`image_pdf` と判定した本は以後まったく実測されないので、判定が外れていても
気づく道が無かった。ここでは出来上がった PDF を描き直して OCR し、撮影時と同じ
数え方（本文行だけ）で 1 ページあたりの文字数を測って、形式と食い違う本を挙げる。

**蔵書は読むだけで、書き換えない。** 判定が外れていた本をどうするか（撮り直すか、
テキスト層を剥がすか）は人が決める。

1 冊およそ 30 秒（20 ページの描き直し + OCR）。`image_pdf` の 175 冊なら約 1.5 時間、
蔵書全部（349 冊）なら約 3 時間。結果は 1 冊ずつ CSV に追記するので、途中で止めても
続きから再開できる。**測れた本だけを済みとする**ので、失敗した本や PDF がまだ無い本は
次に流したときにもう一度測る。

使い方:

    python scripts/check_format.py --books books_typed.json --library <蔵書> \\
        --report <作業フォルダ>/format_check.csv

    # 疑いのある本だけ見たい
    python scripts/check_format.py ... --only image_pdf
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.book_format import IMAGE, SEARCHABLE, book_pdf_path, load_books  # noqa: E402
from core.console import setup_stdio  # noqa: E402
from core.format_check import (  # noqa: E402
    DPI,
    OK,
    SAMPLE_PAGES,
    THRESHOLD,
    UNKNOWN,
    measure_pdf,
    verdict,
)

COLUMNS = [
    "asin",
    "title",
    "format",
    "pdf_pages",
    "sampled",
    "chars",
    "cpp",
    "verdict",
    "seconds",
    # 条件を変えて追記したとき、混ざったことが分かるように残す
    "dpi",
    "threshold",
]
# 済みの印。**測れた本だけ**を飛ばす。失敗・PDF なしを済みにすると、PDF が後から出来ても
# 一過性の失敗でも二度と測られない（しかも 1 行も出ないので気づけない）
NOT_MEASURED = ("PDF なし", "失敗")


def main(argv=None):
    # 題名に cp932 で書けない字があっても、進捗の 1 行で一括処理を止めない
    setup_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--books", required=True, help="books.json（title / asin / format）")
    ap.add_argument("--library", required=True, help="蔵書フォルダ（撮影時の --out と同じ場所）")
    ap.add_argument("--report", required=True, help="一覧（CSV）の出力先。あれば続きから")
    ap.add_argument(
        "--only", choices=[IMAGE, SEARCHABLE], help="この形式の本だけ測る（打ち間違いを防ぐ）"
    )
    ap.add_argument("--sample", type=int, default=SAMPLE_PAGES, help="抜き取るページ数")
    ap.add_argument("--dpi", type=int, default=DPI, help="描き直す解像度（72 で撮影画像と等倍）")
    ap.add_argument("--threshold", type=float, default=THRESHOLD, help="文字数/ページの境目")
    args = ap.parse_args(argv)

    books = load_books(args.books)
    if args.only:
        books = [b for b in books if b.get("format") == args.only]

    # 同じ本を指す鍵。asin が無い一覧もあるので、無ければ題名で引く（book_pdf_path と同じ）
    def key(book):
        return book.get("asin") or book.get("title", "")

    done = set()
    exists = os.path.exists(args.report)
    if exists:
        with open(args.report, encoding="utf-8-sig", newline="") as f:
            done = {
                r["asin"] or r["title"]
                for r in csv.DictReader(f)
                if not r["verdict"].startswith(NOT_MEASURED)
            }
    os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
    counts: dict[str, int] = {}
    suspects = measured = failures = 0
    # 追記する（測れなかった本の行も残す。条件を変えて流したことが後から分かる）
    with open(args.report, "a" if exists else "w", encoding="utf-8-sig", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for n, book in enumerate(books, 1):
            title, asin = book.get("title", ""), book.get("asin", "")
            fmt = book.get("format", "")
            if key(book) in done:
                continue
            pdf = book_pdf_path(args.library, title)
            row = {
                "asin": asin,
                "title": title,
                "format": fmt,
                "dpi": args.dpi,
                "threshold": args.threshold,
            }
            if not os.path.exists(pdf):
                row["verdict"] = "PDF なし"
            else:
                started = time.time()
                try:
                    row.update(measure_pdf(pdf, sample=args.sample, dpi=args.dpi))
                    row["verdict"] = verdict(fmt, row["cpp"], threshold=args.threshold)
                    measured += 1
                    suspects += row["verdict"] not in (OK, UNKNOWN)
                except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
                    row["verdict"] = f"失敗: {type(exc).__name__}: {exc}"
                    failures += 1
                row["seconds"] = round(time.time() - started, 1)
            writer.writerow(row)
            report.flush()
            counts[row["verdict"].split(":")[0]] = counts.get(row["verdict"].split(":")[0], 0) + 1
            print(
                f"[{n}/{len(books)}] cpp={str(row.get('cpp', '-')):>7} {row['verdict']} "
                f"| {title[:32]}",
                flush=True,
            )

    print(
        f"完了: {measured} 冊を測り、{suspects} 冊が形式と食い違う。"
        f"内訳 {counts}。一覧: {args.report}"
    )
    # 失敗が残っているのに 0 で返すと、成功と見分けがつかない
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
