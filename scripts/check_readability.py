"""完成した PDF を読み直し、文章として通っているかを機械的に判定する

342 冊を人が目で見るのは無理で、LLM に読ませるのも量的に現実的でない。
テキスト層だけから計算できる指標で、どの本のコピペを信用してよいかを出す (#65)。

``core/ocr_validator.py`` は変換中の PageLayout（行ごとの信頼度・座標つき）を
前提にしているので、完成した PDF には使えない。しかも信頼度は品質の代理として
弱い（洋書では conf=0.71 の行が正しく読めていた一方、conf=0.92 の行が装飾の
リーダー点だった）。

主な指標は「**直せる未知語**の割合」。頻度表に無いカタカナ語のうち、小書きカナや
濁点・半濁点を入れ替えると頻度の高い語になるものを誤読とみなす。「未知語」を
そのまま数えると専門用語や固有名詞を巻き込む（実測で中央値 22%、洋書は 100%、
数学書は 71% になった）。

使い方:
    python3 scripts/check_readability.py <PDF かフォルダ>
    python3 scripts/check_readability.py library --json
"""

import argparse
import collections
import glob
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from mine_misreads import (  # noqa: E402
    build_fragment_index,
    frequency_candidates,
    load_frequencies,
    zipf_of,
)

from core.ocr_validator import unbalanced_brackets  # noqa: E402

# 3 文字未満のカタカナ語は変種が偶然の別語になりやすい
KATAKANA = re.compile(r"[ァ-ヴー]{3,}")
JAPANESE = re.compile(r"[ぁ-ゖァ-ヴ一-龥]")
HIRAGANA = re.compile(r"[ぁ-ゖ]")
# 同じ文字が 4 つ以上続く（罫線・リーダー点を文字として読んだもの）
RUN = re.compile(r"(.)\1{3,}")

# 直した先がこれ以上の頻度を持つときだけ誤読とみなす
MIN_CORRECTED_ZIPF = 3.0
# 元がこれ以上の頻度なら、そもそも誤読ではない
MAX_ORIGINAL_ZIPF = 1.0

# 日本語の本とみなす下限。洋書はカタカナ語がほとんど無く、割合が当てにならない
MIN_JAPANESE_RATIO = 0.2
# 割合を信用する下限。カタカナ語が数語しかない本は 1 語で割合が跳ねる
MIN_KATAKANA_WORDS = 30
# 割合が高くても、実際の箇所がこれ未満なら格上げしない。
# 実測で「2 箇所しかないのに 6.1%」という本が出た（カタカナ語が 33 語しかない）
MIN_HITS_TO_FLAG = 10

# 判定の境目。用途が「コピペしてメモ」なので、1000 文字あたり何語壊れているかで
# 見る。カタカナ語あたりの割合だと、カタカナ密度の違いで本ごとにぶれる。
# 値は 342 冊の実測分布から決める（断片を除いたあとの分布で再校正する）
SUSPECT_PER_1000 = 1.0
BAD_PER_1000 = 2.0

# 同じ文字の連なりがこれ以上なら、装飾やリーダー点を文字として読んでいる
MAX_RUN_RATIO = 0.05

# 文字が取れたページがこれ未満なら、テキスト層が無いのと変わらない
MAX_EMPTY_PAGE_RATIO = 0.5

# テキスト層があるとみなす下限
MIN_TEXT_CHARS = 500


def read_pdf(path):
    """PDF のテキスト層をページごとに読む。

    抽出器が字の隙間に入れる空白は落とす（改行は残す）。mine_misreads の
    load_corpus と同じ正規化。揃えないと、規則を掘るときと測るときで
    語の切れ方が変わる。
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    try:
        return [
            re.sub(r"[^\S\n]+", "", doc[i].get_textpage().get_text_bounded())
            for i in range(len(doc))
        ]
    finally:
        doc.close()


def correction_for(word, frequencies, cache, fragments=frozenset()):
    """小書きカナ・濁点を入れ替えると頻度の高い語になるなら、その語を返す。

    行で切れた断片は除く。PDF のテキスト層は行単位なので、行をまたぐ語は
    断片として現れる（テーブル -> テーブ + ル）。断片は辞書に無いので必ず
    「珍しい語」になり、頻度差だけで見ると別の語への誤読に化ける
    （テーブ -> テープ / ハイク -> バイク / ウエイ -> ウェイ）。

    断片の数は段組や判型に比例するので、除かないと指標に「OCR の質」ではなく
    「レイアウト」が混じる。mine_misreads が実データ 62 冊で調整した
    build_fragment_index をそのまま使う。
    """
    if word in cache:
        return cache[word]
    found = None
    if word not in fragments and zipf_of(frequencies, word) < MAX_ORIGINAL_ZIPF:
        for candidate in frequency_candidates(word):
            if zipf_of(frequencies, candidate) >= MIN_CORRECTED_ZIPF:
                found = candidate
                break
    cache[word] = found
    return found


def measure(pages, frequencies, cache, fragments=frozenset()):
    """1 冊分の指標を返す。pages はページごとのテキスト。

    ページ単位で見るのは、本全体の合計だと「3 割のページが 1 文字も取れて
    いない本」が平然と通るため。
    """
    text = "\n".join(pages)
    words = KATAKANA.findall(text)
    counts = collections.Counter(words)
    corrections = {}
    for word in counts:
        fixed = correction_for(word, frequencies, cache, fragments)
        if fixed:
            corrections[word] = fixed
    hits = sum(counts[w] for w in corrections)
    lines = [line for line in text.splitlines() if line.strip()]
    body_pages = [p for p in pages if p.strip()]
    return {
        "chars": len(text),
        "pages": len(pages),
        "empty_page_ratio": 1 - len(body_pages) / max(1, len(pages)),
        "japanese_ratio": len(JAPANESE.findall(text)) / max(1, len(text)),
        "hiragana_ratio": len(HIRAGANA.findall(text)) / max(1, len(text)),
        "katakana_words": len(words),
        "misread_kinds": len(corrections),
        "misread_hits": hits,
        "misread_rate": hits / max(1, len(words)),
        # 用途は「コピペしてメモ」なので、1000 文字あたり何語壊れているかが
        # 利用者の単位。カタカナ密度の違いにも左右されない
        # max(1, len/1000) にすると 1000 文字未満で割る数が 1 に固定され、
        # 壊れた短い本が安全側に見える。文字数そのもので割る
        "misreads_per_1000": hits * 1000 / max(1, len(text)),
        "run_ratio": sum(len(m.group(0)) for m in RUN.finditer(text)) / max(1, len(text)),
        "short_line_ratio": sum(1 for x in lines if len(x.strip()) <= 3) / max(1, len(lines)),
        "bracket_gap": sum(abs(d) for *_x, d in unbalanced_brackets(text))
        * 1000
        / max(1, len(text)),
        "top_misreads": [
            f"{w}->{corrections[w]}" for w in sorted(corrections, key=lambda w: (-counts[w], w))[:5]
        ],
    }


def verdict(m):
    """判定と理由。割合が当てにならない本は、そう言う。

    「情報が足りない」を「問題が無い」と言い換えない。既定の出力は OK を
    隠すので、言い換えると最悪の本が最も静かに消える。
    """
    if m["chars"] < MIN_TEXT_CHARS or m["empty_page_ratio"] > MAX_EMPTY_PAGE_RATIO:
        return "テキスト層なし", (
            f"文字が取れたページが {(1 - m['empty_page_ratio']) * 100:.0f}% しかない"
        )
    if m["japanese_ratio"] < MIN_JAPANESE_RATIO:
        # ひらがなが皆無なら洋書。中途半端に日本語が混じる本は OCR の総崩れを疑う
        if m["hiragana_ratio"] < 0.01:
            return "対象外", "日本語の本ではない（カタカナ語での判定が当てにならない）"
        return "要確認", f"日本語の文字が {m['japanese_ratio'] * 100:.0f}% しかない"
    if m["katakana_words"] < MIN_KATAKANA_WORDS or m["misread_hits"] < MIN_HITS_TO_FLAG:
        return "判定不能", (
            f"カタカナ語 {m['katakana_words']} 語 / 直せる未知語 {m['misread_hits']} 箇所"
            "（少なすぎて割合が当てにならない）"
        )
    rate = f"1000 文字あたり {m['misreads_per_1000']:.1f} 語（カタカナ語の {m['misread_rate'] * 100:.1f}%）"
    # 同じ文字の連なりは単独では判断材料にならない。マンガの擬音・効果音でも
    # 高く出る（実測で 東京大学物語 7〜11% / 川尻こだま 22%）。
    # 誤読が出ている本の追加材料として添える
    if m["run_ratio"] >= MAX_RUN_RATIO:
        rate += f" / 同じ文字の連なりが {m['run_ratio'] * 100:.0f}%"
    if m["misreads_per_1000"] >= BAD_PER_1000:
        return "怪しい", rate
    if m["misreads_per_1000"] >= SUSPECT_PER_1000 or m["run_ratio"] >= MAX_RUN_RATIO:
        return "要確認", rate
    return "OK", rate


def iter_pdfs(paths):
    for path in paths:
        if os.path.isdir(path):
            yield from sorted(glob.glob(os.path.join(path, "*.pdf")))
        else:
            yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description="完成した PDF が文章として通っているか判定する")
    parser.add_argument("paths", nargs="+", help="PDF かフォルダ")
    parser.add_argument("--json", action="store_true", help="JSON Lines で出す")
    parser.add_argument("--all", action="store_true", help="OK の本も出す（既定は要確認以上だけ）")
    args = parser.parse_args(argv)

    try:
        frequencies = load_frequencies()
    except ImportError as e:
        print(
            f'頻度表を読み込めません（{e}）。pip install "wordfreq>=3.1,<4" で入ります',
            file=sys.stderr,
        )
        return 2
    fragments = build_fragment_index(frequencies)
    cache: dict = {}
    rows = []
    unreadable = 0
    for path in iter_pdfs(args.paths):
        try:
            pages = read_pdf(path)
        except Exception as e:  # noqa: BLE001 - 1 冊で止めない
            print(f"読めません: {path} ({e})", file=sys.stderr)
            unreadable += 1
            continue
        m = measure(pages, frequencies, cache, fragments)
        m["file"] = os.path.basename(path)
        m["verdict"], m["reason"] = verdict(m)
        rows.append(m)

    order = {"怪しい": 0, "要確認": 1, "判定不能": 2, "OK": 3, "対象外": 4, "テキスト層なし": 5}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), -r["misread_rate"]))

    if args.json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return 1 if (unreadable or any(r["verdict"] == "怪しい" for r in rows)) else 0

    counts: dict = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(
        "判定: "
        + " / ".join(
            f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9))
        )
    )
    print()
    for r in rows:
        if not args.all and r["verdict"] in ("OK", "対象外", "テキスト層なし"):
            continue
        print(f"[{r['verdict']}] {r['file'][:56]}")
        print(f"    {r['reason']}  {r['misread_kinds']} 種 / {r['misread_hits']} 箇所")
        if r["top_misreads"]:
            print(f"    {' '.join(r['top_misreads'])}")
    return 1 if (unreadable or counts.get("怪しい")) else 0


if __name__ == "__main__":
    sys.exit(main())
