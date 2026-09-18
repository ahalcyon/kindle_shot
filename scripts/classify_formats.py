"""本ごとに形式を決めて books.json を書く

判定そのものは `core/book_format.py` にある（なぜ実測の文字数/ページで切れるか、
なぜ迷ったら searchable_pdf に倒すかも、そちらに書いてある）。
このスクリプトはそれを蔵書に当てて books.json を作るだけ。

使い方:

    python scripts/classify_formats.py --books books_all.json \
        --measured batch.jsonl --library <旧蔵書フォルダ> \
        --buckets buckets.json --out books_typed.json

`--measured` はバッチの JSON Lines ログ（`ocr_validation` を含むもの）。
複数回指定できる。`--only-missing` に出力先を渡すと、既に PDF がある本を省く。
"""

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.book_format import (  # noqa: E402
    DEFAULT_THRESHOLD,
    IMAGE,
    SEARCHABLE,
    book_pdf_path,
    decide,
    genre_labels,
    library_labels,
    load_books,
    measured_labels,
    series_labels,
)
from core.console import setup_stdio  # noqa: E402


def main(argv=None):
    # 題名に cp932 で書けない字があっても、進捗の 1 行で一括処理を止めない
    setup_stdio()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--books", required=True, help="入力の books.json")
    p.add_argument("--out", required=True, help="format を入れた books.json の出力先")
    p.add_argument(
        "--measured",
        action="append",
        default=[],
        help="ocr_validation を含むバッチログ（複数可）",
    )
    p.add_argument("--library", help="旧蔵書フォルダ（テキスト層の有無を見る）")
    p.add_argument("--buckets", help="ジャンル分けの JSON")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="文字数/ページの閾値")
    p.add_argument("--only-missing", help="このフォルダに PDF がある本は出力から除く")
    args = p.parse_args(argv)

    books = load_books(args.books)
    measured, _ = measured_labels(args.measured, args.threshold)
    series = series_labels(measured)
    genre = genre_labels(args.buckets) if args.buckets else {}
    titles = [b.get("title") for b in books if b.get("title")]
    library = library_labels(args.library, titles) if args.library else {}

    # **完成済みの判定も正引きで行う (#95)。** ファイル名から逆引きすると、
    # 名前を切り詰められた本を「未処理」と見なして 1 冊 10 分かけて撮り直す
    done = set()
    if args.only_missing and os.path.isdir(args.only_missing):
        for title in titles:
            if os.path.isfile(book_pdf_path(args.only_missing, title)):
                done.add(title)

    out = []
    why: collections.Counter = collections.Counter()
    for book in books:
        title = book.get("title")
        if title in done:
            continue
        fmt, reason = decide(title, measured=measured, series=series, genre=genre, library=library)
        why[(reason, fmt)] += 1
        # **本ごとの設定を落とさない。** make_books.py は page_turn / split_words /
        # max_pages などを本ごとに書く（selection.example.json の「横書きは
        # page_turn: right」がまさにそれ）。拾う鍵を並べる形にすると、
        # ここを通した books.json から黙って消える。入力は元々
        # load_batch_file の検証を通せる形なので、そのまま持ち回って format だけ上書きする
        entry = {**book, "format": fmt}
        out.append(entry)

    import json

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(
        f"判定材料: 実測 {len(measured)} / 系列 {len(series)} / "
        f"ジャンル {len(genre)} / 旧蔵書 {len(library)}"
    )
    print(f"出力 {len(out)} 冊 -> {args.out}")
    for (reason, fmt), n in sorted(why.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {fmt:15s} {reason}")
    counts = collections.Counter(b["format"] for b in out)
    print(f"内訳: image_pdf {counts[IMAGE]} / searchable_pdf {counts[SEARCHABLE]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
