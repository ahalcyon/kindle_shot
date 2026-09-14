"""検索可能 PDF から OCR テキスト層を剥がして image_pdf 相当に戻す

漫画を誤って searchable_pdf で作ってしまったときに使う。**撮り直さなくてよい。**
`image_pdf` と `searchable_pdf` は同じ画像を同じ座標に描いていて、違いは
不可視テキストとフォントが載るかどうかだけだから（`core/text_layer.py` の説明）。

1 冊 15 秒ほど。蔵書を直接書き換えるので、**検証に通った本だけ差し替える**
（ページ数・抜き取りページの描画結果・抽出文字数を見る）。通らなければ元の
ファイルをそのまま残し、失敗として数える。

使い方:

    # 判定済みの books.json で image_pdf の本だけ剥がす
    python scripts/strip_text_layer.py --folder <蔵書> --books books_typed.json

    # 実測ログの文字数/ページで決める
    python scripts/strip_text_layer.py --folder <蔵書> --measured batch.jsonl

    # 何をするかだけ見る
    python scripts/strip_text_layer.py --folder <蔵書> --books books_typed.json --dry-run
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.book_format import DEFAULT_THRESHOLD, IMAGE, measured_labels  # noqa: E402
from core.text_layer import strip_file  # noqa: E402


def titles_from_books(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict) and "books" in data:
        data = data["books"]
    return {b["title"] for b in data if b.get("format") == IMAGE}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--folder", required=True, help="PDF の入っているフォルダ")
    p.add_argument("--books", help="format 付きの books.json（image_pdf の本を剥がす）")
    p.add_argument("--measured", action="append", default=[], help="バッチログ（複数可）")
    p.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="文字数/ページの閾値"
    )
    p.add_argument("--dry-run", action="store_true", help="対象を出すだけで書き換えない")
    p.add_argument("--no-verify", action="store_true", help="検証を省く（推奨しない）")
    p.add_argument("--json", action="store_true", help="1 冊 1 行の JSON で出す")
    args = p.parse_args(argv)

    targets = set()
    if args.books:
        targets |= titles_from_books(args.books)
    if args.measured:
        labels, _ = measured_labels(args.measured, args.threshold)
        targets |= {t for t, fmt in labels.items() if fmt == IMAGE}
    if not targets:
        print("対象がありません（--books か --measured を指定してください）", file=sys.stderr)
        return 2

    present = {
        n[:-4]: os.path.join(args.folder, n)
        for n in os.listdir(args.folder)
        if n.lower().endswith(".pdf")
    }
    todo = sorted(t for t in targets if t in present)
    print(f"対象 {len(targets)} 冊のうち {len(todo)} 冊がフォルダにあります", file=sys.stderr)
    if args.dry_run:
        for t in todo:
            print(t)
        return 0

    ok = failed = 0
    saved = 0
    for i, title in enumerate(todo, 1):
        result = strip_file(present[title], verify=not args.no_verify)
        result["title"] = title
        if result["ok"]:
            ok += 1
            saved += result["size_before"] - result["size_after"]
        else:
            failed += 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        else:
            mark = "OK" if result["ok"] else "NG"
            extra = result.get("error") or f"{result.get('removed', 0)} ブロック除去"
            print(f"[{mark}] {i}/{len(todo)} {title[:44]} — {extra}", flush=True)

    print(f"成功 {ok} / 失敗 {failed} / 削減 {saved / 1024 / 1024:.1f} MiB", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
