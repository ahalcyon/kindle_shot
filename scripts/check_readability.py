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
import glob
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from mine_misreads import frequency_candidates  # noqa: E402

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

# 判定の境目。342 冊の実測分布から決めた
# （中央 0.019 / 75% 0.033 / 90% 0.041 / 95% 0.052）
SUSPECT_RATE = 0.03
BAD_RATE = 0.05

# テキスト層があるとみなす下限
MIN_TEXT_CHARS = 500


def read_pdf(path):
    """PDF のテキスト層を読む。"""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    try:
        return "".join(doc[i].get_textpage().get_text_bounded() for i in range(len(doc)))
    finally:
        doc.close()


def load_frequencies():
    """wordfreq の日本語頻度表。入っていなければ ImportError。

    ``get_frequency_dict`` は表をそのまま返すので **MeCab を必要としない**。
    ``zipf_frequency()`` は語を分かち書きするため MeCab を要求する。
    """
    from wordfreq import get_frequency_dict

    return get_frequency_dict("ja")


def zipf_of(frequencies, word):
    value = frequencies.get(word)
    return math.log10(value * 1e9) if value else 0.0


def correction_for(word, frequencies, cache):
    """小書きカナ・濁点を入れ替えると頻度の高い語になるなら、その語を返す。"""
    if word in cache:
        return cache[word]
    found = None
    if zipf_of(frequencies, word) < MAX_ORIGINAL_ZIPF:
        for candidate in frequency_candidates(word):
            if zipf_of(frequencies, candidate) >= MIN_CORRECTED_ZIPF:
                found = candidate
                break
    cache[word] = found
    return found


def measure(text, frequencies, cache):
    """テキスト 1 本分の指標を返す。"""
    words = KATAKANA.findall(text)
    corrections = {}
    for word in set(words):
        fixed = correction_for(word, frequencies, cache)
        if fixed:
            corrections[word] = fixed
    counts = {w: words.count(w) for w in corrections}
    lines = [line for line in text.splitlines() if line.strip()]
    return {
        "chars": len(text),
        "japanese_ratio": len(JAPANESE.findall(text)) / max(1, len(text)),
        "hiragana_ratio": len(HIRAGANA.findall(text)) / max(1, len(text)),
        "katakana_words": len(words),
        "misread_kinds": len(corrections),
        "misread_hits": sum(counts.values()),
        "misread_rate": sum(counts.values()) / max(1, len(words)),
        "run_ratio": sum(len(m.group(0)) for m in RUN.finditer(text)) / max(1, len(text)),
        "short_line_ratio": sum(1 for x in lines if len(x.strip()) <= 3) / max(1, len(lines)),
        "top_misreads": [
            f"{w}->{corrections[w]}" for w in sorted(counts, key=lambda w: -counts[w])[:5]
        ],
    }


def verdict(m):
    """判定と理由。割合が当てにならない本は「判定不能」と言う。"""
    if m["chars"] < MIN_TEXT_CHARS:
        return "テキスト層なし", "画像 PDF か、文字がほとんど取れていない"
    if m["japanese_ratio"] < MIN_JAPANESE_RATIO:
        return "対象外", "日本語の本ではない（カタカナ語での判定が当てにならない）"
    if m["katakana_words"] < MIN_KATAKANA_WORDS:
        return "判定不能", f"カタカナ語が {m['katakana_words']} 語しかない"
    rate = f"直せる未知語が {m['misread_rate'] * 100:.1f}%"
    if m["misread_hits"] < MIN_HITS_TO_FLAG:
        # 割合だけ高くて実害が小さい本を上位に出さない
        return "OK", f"{rate}（{m['misread_hits']} 箇所のみ）"
    if m["misread_rate"] >= BAD_RATE:
        return "怪しい", rate
    if m["misread_rate"] >= SUSPECT_RATE:
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
    cache: dict = {}
    rows = []
    for path in iter_pdfs(args.paths):
        try:
            text = read_pdf(path)
        except Exception as e:  # noqa: BLE001 - 1 冊で止めない
            print(f"読めません: {path} ({e})", file=sys.stderr)
            continue
        m = measure(text, frequencies, cache)
        m["file"] = os.path.basename(path)
        m["verdict"], m["reason"] = verdict(m)
        rows.append(m)

    order = {"怪しい": 0, "要確認": 1, "判定不能": 2, "OK": 3, "対象外": 4, "テキスト層なし": 5}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), -r["misread_rate"]))

    if args.json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return 0

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
        print(f"    {r['reason']}（{r['misread_kinds']} 種 / {r['misread_hits']} 箇所）")
        if r["top_misreads"]:
            print(f"    {' '.join(r['top_misreads'])}")
    return 1 if counts.get("怪しい") else 0


if __name__ == "__main__":
    sys.exit(main())
