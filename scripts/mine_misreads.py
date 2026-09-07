"""OCR が小書きのカナを大書きに誤読した箇所を、出力から機械的に洗い出す

NDLOCR-Lite は小書きのカナ (ッ ャ ュ ョ ィ ...) を大書きに読み違えることが
多い。実測では『みんなのフィードバック大全』1 冊で 727 件あった。

**一般規則では直せない。** 「ツ の後ろが無声子音なら促音」のような規則は、
実データで次のように誤爆する:

    キャベツステーキ -> キャベッステーキ    カツカレー -> カッカレー
    テイクアウト     -> ティクアウト        ウイスキー -> ウィスキー
    ヒヨドリ         -> ヒョドリ            パティシエ -> パティシェ

そこで、**同じコーパスの中で「小書きに直した形のほうが多く出てくる」語だけ**
を誤読とみなして拾う。直した形が 1 度も出てこない語は（本当は誤読でも）
拾わない。取りこぼしても壊さないほうを選ぶ。

拾った結果は replacements.json の ``literal`` にそのまま貼れる形で出る。

使い方:
    python scripts\\mine_misreads.py out\\*.pdf --out rules.json
    python scripts\\mine_misreads.py out --min-ratio 3
"""

import argparse
import collections
import glob
import json
import os
import re
import sys

# 大書き -> 小書き。ワ ヵ ヶ は現代の表記でまず出ないので入れない
BIG_TO_SMALL = {
    "ツ": "ッ",
    "ヤ": "ャ",
    "ユ": "ュ",
    "ヨ": "ョ",
    "ア": "ァ",
    "イ": "ィ",
    "ウ": "ゥ",
    "エ": "ェ",
    "オ": "ォ",
}

# 語として数えるカタカナの連なり。2 文字だと部分語を拾いすぎる
KATAKANA_WORD = re.compile(r"[ァ-ヴー]{3,}")

TEXT_EXTENSIONS = (".txt", ".md")
PDF_EXTENSIONS = (".pdf",)


def read_pdf(path):
    """PDF のテキスト層を読む。pypdfium2 が無ければ None。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    doc = pdfium.PdfDocument(path)
    return "\n".join(doc[i].get_textpage().get_text_bounded() for i in range(len(doc)))


def iter_files(paths):
    """引数のパス（ファイル / ディレクトリ / glob）を平らにして返す。"""
    for raw in paths:
        expanded = glob.glob(raw) or [raw]
        for path in expanded:
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for name in sorted(files):
                        if name.lower().endswith(TEXT_EXTENSIONS + PDF_EXTENSIONS):
                            yield os.path.join(root, name)
            else:
                yield path


def load_corpus(paths):
    """対象ファイルを読んで 1 本のテキストにする。(text, 読めたファイル数)。"""
    chunks = []
    count = 0
    for path in iter_files(paths):
        lower = path.lower()
        if lower.endswith(PDF_EXTENSIONS):
            try:
                text = read_pdf(path)
            except Exception as e:  # noqa: BLE001 - 壊れた PDF 1 冊で止めない
                print(f"読めません: {path} ({e})", file=sys.stderr)
                continue
            if text is None:
                print(f"pypdfium2 が無いため PDF を読めません: {path}", file=sys.stderr)
                continue
        elif lower.endswith(TEXT_EXTENSIONS):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                print(f"読めません: {path} ({e})", file=sys.stderr)
                continue
        else:
            continue
        chunks.append(text)
        count += 1
    # 抽出器が字の隙間に入れる空白を落とす。**改行は残す** — 落とすと隣り合う
    # カタカナ語がつながって 1 語になり、語として数えられなくなる
    return re.sub(r"[^\S\n]+", "", "".join(chunks)), count


def count_words(text):
    """カタカナ語の出現数を数える。"""
    return collections.Counter(KATAKANA_WORD.findall(text))


# 1 語あたりに試す直し方の上限。大書きが多い語で組み合わせが爆発しないように
MAX_COMBINATIONS = 1 << 8


def corrected(word):
    """語の中の大書きカナを小書きにした候補を、組み合わせすべて返す。

    1 文字ずつではなく総当たりにするのは、``ギャツプフイードバツク`` のように
    **1 語に複数の誤読が入る**ため。1 文字ずつだと ``ギャツプフィードバツク``
    のような「まだ誤読が残った形」が正解として採られてしまう。置換辞書は
    ルールを連鎖させないので、それでは直りきらない。

    先頭の 1 文字は対象外。小書きのカナが語頭に来ることは無い。
    """
    positions = [i for i in range(1, len(word)) if word[i] in BIG_TO_SMALL]
    if not positions or (1 << len(positions)) > MAX_COMBINATIONS:
        return
    for mask in range(1, 1 << len(positions)):
        chars = list(word)
        for bit, i in enumerate(positions):
            if mask >> bit & 1:
                chars[i] = BIG_TO_SMALL[word[i]]
        yield "".join(chars)


def mine(words, *, min_ratio=2.0, min_count=2):
    """誤読とみなせる語を [(誤, 正, 誤の回数, 正の回数), ...] で返す。

    「小書きに直した形が min_ratio 倍以上、かつ min_count 回以上出てくる」
    ものだけを拾う。同じ語に複数の直し方があるときは、正の回数が多いほうを採る。
    """
    found = []
    for word, wrong_count in words.items():
        best = None
        for candidate in corrected(word):
            right_count = words.get(candidate, 0)
            if right_count < min_count or right_count < wrong_count * min_ratio:
                continue
            if best is None or right_count > best[1]:
                best = (candidate, right_count)
        if best is not None:
            found.append((word, best[0], wrong_count, best[1]))
    # 効き目の大きい順（誤読の回数）に並べる
    found.sort(key=lambda item: (-item[2], item[0]))
    return resolve_chains(found)


def resolve_chains(found):
    """直した先がさらに誤読なら、たどって最終形に付け替える。

    ``A -> B`` と ``B -> C`` が両方出たとき、置換辞書はルールを 1 回ずつしか
    適用しないので ``A`` は ``B`` 止まりになる。``A -> C`` に畳んでおく。
    """
    target = {wrong: right for wrong, right, _w, _r in found}
    resolved = []
    for wrong, right, wrong_count, right_count in found:
        seen = {wrong}
        while right in target and right not in seen:
            seen.add(right)
            right = target[right]
        if right != wrong:
            resolved.append((wrong, right, wrong_count, right_count))
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="PDF / テキスト / それらを含むフォルダ")
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=2.0,
        help="正しい形が誤読の何倍出てくれば採用するか（既定: 2.0）",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="正しい形の最低出現回数（既定: 2）",
    )
    parser.add_argument("--out", help="replacements.json に貼れる JSON の書き出し先")
    args = parser.parse_args(argv)

    text, files = load_corpus(args.paths)
    if not files:
        print("読めるファイルがありません", file=sys.stderr)
        return 1
    words = count_words(text)
    found = mine(words, min_ratio=args.min_ratio, min_count=args.min_count)

    print(f"{files} ファイル / カタカナ語 {len(words)} 種 {sum(words.values())} 件")
    print(f"誤読とみなせる語 {len(found)} 種 {sum(item[2] for item in found)} 件\n")
    print(f"{'誤(回)':>7} {'正(回)':>7}  誤 -> 正")
    for wrong, right, wrong_count, right_count in found:
        print(f"{wrong_count:7d} {right_count:7d}  {wrong} -> {right}")

    if args.out:
        rules = {"literal": {wrong: right for wrong, right, _w, _r in found}}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"\n書き出しました: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
