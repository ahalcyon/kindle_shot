"""replacements.json の規則を、実際に出来上がった本に当てて点検する

辞書のテスト (tests/test_replacements_dictionary.py) は辞書を辞書自身に照らす
だけなので、**誤読形を部分文字列として含む正しい語**を壊す規則を見つけられない。

    プロード -> ブロード    アップロード を 165 箇所壊す
    バツカー -> ハッカー    ヒューレット・パッカード を壊す
    ナツツ   -> ナッツ      ナツツバキ(夏椿) を壊す

この種は、規則を足すときに見ていたのとは別の本で初めて出る。そこで手持ちの
本を全部読み、カタカナの連続ごとに「置換するとどうなるか」を並べて人が見る。

**「置換後の形がコーパスに一度も出てこない」ものを疑う。** ふつうの誤読直しは、
直した形が同じ本の別の場所に正しく出ている (クロープ(ホール) の隣に
クローブ(ホール) が 51 件)。出てこないなら、その形は日本語として存在しない
可能性がある。存在する語もあるので (固有名詞や複合語)、最後は人が見る。

使い方:
    python scripts\\audit_rules.py C:\\...\\library
    python scripts\\audit_rules.py library --min-count 5
    python scripts\\audit_rules.py library --rule プロード   # 1 規則の内訳を見る
"""

import argparse
import collections
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from mine_misreads import load_corpus  # noqa: E402

from core.text_replacements import load_replacer  # noqa: E402

KATAKANA_RUN = re.compile(r"[\u30a0-\u30ff\u31f0-\u31ff\u3099-\u309c]+")


def katakana_runs(text):
    """カタカナの連続と、その出現回数。"""
    return collections.Counter(KATAKANA_RUN.findall(text))


def suspicious(runs, replacer, *, min_count):
    """置換で「コーパスに一度も無い形」に変わる連続を、多い順に返す。

    Returns: [(count, before, after), ...]
    """
    present = set(runs)
    found = []
    for run, count in runs.items():
        if count < min_count:
            continue
        after = replacer.apply(run)
        if after == run or after in present:
            continue
        found.append((count, run, after))
    found.sort(reverse=True)
    return found


def rule_detail(runs, replacer, key):
    """左辺 key を含むカタカナ連続を、多い順に返す。"""
    rows = [(count, run, replacer.apply(run)) for run, count in runs.items() if key in run]
    rows.sort(reverse=True)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description="置換規則をコーパスに当てて点検する")
    p.add_argument("paths", nargs="+", help="PDF / テキスト / それらを含むディレクトリ")
    p.add_argument("--rules", help="置換辞書 (既定: リポジトリの replacements.json)")
    p.add_argument(
        "--min-count", type=int, default=3, help="この回数以上出るものだけ見る (既定: 3)"
    )
    p.add_argument("--rule", help="この左辺を含む連続の内訳だけを出す")
    p.add_argument("--json", dest="as_json", action="store_true", help="JSON で出す")
    args = p.parse_args(argv)

    replacer, err = load_replacer(args.rules)
    if err:
        print(err, file=sys.stderr)
        return 2
    if replacer.is_empty():
        print("置換規則がありません", file=sys.stderr)
        return 2

    text, files = load_corpus(args.paths)
    if not files:
        print("読めるファイルがありません", file=sys.stderr)
        return 2
    runs = katakana_runs(text)
    print(f"{files} ファイル / {len(text)} 文字 / カタカナ {len(runs)} 種", file=sys.stderr)

    if args.rule:
        rows = rule_detail(runs, replacer, args.rule)
        if args.as_json:
            json.dump(
                [{"count": c, "before": b, "after": a} for c, b, a in rows],
                sys.stdout,
                ensure_ascii=False,
                indent=2,
            )
            print()
        else:
            total = sum(c for c, _, _ in rows)
            print(f"{args.rule} を含む連続: {len(rows)} 種 / {total} 箇所")
            for c, b, a in rows:
                mark = "  " if a == b else "->"
                print(f"{c:6d}  {b} {mark} {a if a != b else ''}")
        return 0

    found = suspicious(runs, replacer, min_count=args.min_count)
    if args.as_json:
        json.dump(
            [{"count": c, "before": b, "after": a} for c, b, a in found],
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        print()
        return 0
    print(f"要確認 {len(found)} 件（置換後の形がコーパスに出てこないもの）")
    for c, b, a in found:
        print(f"{c:6d}  {b}  ->  {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
