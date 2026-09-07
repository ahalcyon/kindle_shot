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
import itertools
import json
import math
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
    """PDF のテキスト層を読む。pypdfium2 を読み込めなければ ImportError。

    Windows では pypdfium2 が入っていてもネイティブ DLL のロードに失敗すると
    ImportError になる。「入っていない」と決めつけず、理由をそのまま上げる。
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    try:
        return "\n".join(doc[i].get_textpage().get_text_bounded() for i in range(len(doc)))
    finally:
        # Windows ではファイルがロックされたままになる
        doc.close()


def iter_files(paths):
    """引数のパス（ファイル / ディレクトリ / glob）を平らにして返す。

    **実在するパスを先に見る。** glob を先に試すと、書名に ``[`` を含む本
    （``面接質問50 [新版].pdf``）が glob のパターンとして解釈され、指定した
    ファイルが黙って無視されて別のファイルが読まれる。
    """
    seen = set()

    def emit(path):
        key = os.path.realpath(path)
        if key not in seen:
            seen.add(key)
            yield path

    for raw in paths:
        expanded = [raw] if os.path.exists(raw) else glob.glob(raw)
        if not expanded:
            print(f"見つかりません: {raw}", file=sys.stderr)
            continue
        for path in expanded:
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for name in sorted(files):
                        if name.lower().endswith(TEXT_EXTENSIONS + PDF_EXTENSIONS):
                            yield from emit(os.path.join(root, name))
            elif path.lower().endswith(TEXT_EXTENSIONS + PDF_EXTENSIONS):
                yield from emit(path)
            else:
                print(f"対象外の拡張子です: {path}", file=sys.stderr)


def load_corpus(paths):
    """対象ファイルを読んで 1 本のテキストにする。(text, 読めたファイル数)。"""
    chunks = []
    count = 0
    for path in iter_files(paths):
        lower = path.lower()
        if lower.endswith(PDF_EXTENSIONS):
            try:
                text = read_pdf(path)
            except ImportError as e:
                print(f"PDF を読めません（pypdfium2: {e}）", file=sys.stderr)
                return "", 0
            except Exception as e:  # noqa: BLE001 - 壊れた PDF 1 冊で止めない
                print(f"読めません: {path} ({e})", file=sys.stderr)
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
    # カタカナ語がつながって 1 語になり、語として数えられなくなる。
    # ファイルの間も改行で区切る（末尾の語と次の先頭の語がつながらないように）
    return re.sub(r"[^\S\n]+", "", "\n".join(chunks)), count


def count_words(text):
    """カタカナ語の出現数を数える。"""
    return collections.Counter(KATAKANA_WORD.findall(text))


# 1 語あたりに試す直し方の上限（部分集合の数）。大書きが多い語で組み合わせが
# 爆発しないように。実データ 13 冊では 1 語も引っかからなかったが、抽出器が
# 語の間に空白を入れなかった行では長い連なりが 1 語になることがある
MAX_COMBINATIONS = 1 << 12


def corrected(word):
    """語の中の大書きカナを小書きにした候補を、組み合わせすべて返す。

    1 文字ずつではなく総当たりにするのは、``ギャツプフイードバツク`` のように
    **1 語に複数の誤読が入る**ため。1 文字ずつだと ``ギャツプフィードバツク``
    のような「まだ誤読が残った形」が正解として採られてしまう。置換辞書は
    ルールを連鎖させないので、それでは直りきらない。

    先頭の 1 文字は対象外。小書きのカナが語頭に来ることは無い。
    """
    positions = [i for i in range(1, len(word)) if word[i] in BIG_TO_SMALL]
    if not positions:
        return
    if (1 << len(positions)) > MAX_COMBINATIONS:
        # 黙って捨てると「大書きが無い語」と区別が付かない。手当てできるよう出す
        print(f"大書きが多すぎるため調べません: {word}", file=sys.stderr)
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
    ものだけを拾う。

    同じ語に複数の直し方が残ったときは、**直した箇所が最も多いもの**を採る。
    回数が最大のものを採ると、``フイードバック`` のように「一部だけ直した形」
    がコーパスに多いときにそれが選ばれ、誤読を別の誤読に書き換えるだけの
    ルールができる。置換辞書はルールを連鎖させないので、それでは直りきらない。
    同数なら回数が多いほうを採る。
    """
    found = []
    for word, wrong_count in words.items():
        best = None
        for candidate in corrected(word):
            right_count = words.get(candidate, 0)
            if right_count < min_count or right_count < wrong_count * min_ratio:
                continue
            # 候補は語と同じ長さ（大書き -> 小書きの置換しかしない）
            fixes = sum(1 for before, after in zip(word, candidate, strict=True) if before != after)
            if best is None or (fixes, right_count) > (best[2], best[1]):
                best = (candidate, right_count, fixes)
        if best is not None:
            found.append((word, best[0], wrong_count, best[1]))
    # 効き目の大きい順（誤読の回数）に並べる
    found.sort(key=lambda item: (-item[2], item[0]))
    return found


# ============================================================
# 頻度コーパスを証拠にする経路 (#44)
# ============================================================
#
# 上の mine() は「同じコーパスの中で正しい形のほうが多く出てくる」ことを証拠に
# する。取りこぼしが構造的にある: **正しい形が 1 度も出てこない誤読は拾えない**。
# 実データでは 濁点・半濁点の取り違え (イベント -> イペント) がこれに当たり、
# 誤読しか現れない語がいくつもあった。
#
# そこで外部の頻度表 (wordfreq) を証拠に使う。「観測した語はほとんど使われない
# のに、混同しやすい字を直した形はよく使われる」なら誤読とみなす。
#
# **辞書に載っているかどうかでは判定できない。** モアーズ/モアース も
# ウーバー/ウーパー も両方とも辞書にある。どちらが正しいかは頻度でしか決まらない。

# 濁点・半濁点の取り違え。OCR が最も多く間違える組。小書き化と違い**語頭にも
# 起きる** (バッグ -> パッグ) ので、位置の制限を分けてある
DAKUTEN_PAIRS = (
    ("ブ", "プ"),
    ("ビ", "ピ"),
    ("ベ", "ペ"),
    ("ボ", "ポ"),
    ("バ", "パ"),
    ("ハ", "バ"),
    ("ヒ", "ビ"),
    ("フ", "ブ"),
    ("ヘ", "ベ"),
    ("ホ", "ボ"),
    ("ソ", "ゾ"),
    ("シ", "ジ"),
    ("ス", "ズ"),
    ("タ", "ダ"),
    ("ク", "グ"),
    ("カ", "ガ"),
)

CONFUSABLE: dict[str, set[str]] = {}
for _a, _b in DAKUTEN_PAIRS:
    CONFUSABLE.setdefault(_a, set()).add(_b)
    CONFUSABLE.setdefault(_b, set()).add(_a)

# 1 語あたりに試す組み合わせの上限。濁点になりうる字が多い語で爆発させない
MAX_FREQUENCY_COMBINATIONS = 1 << 12

# 採用の閾値。実データ 62 冊で合わせた:
#   gap 2.0 未満 … バス/パス (0.62) ボール/ポール (0.34) のような
#                  「どちらも普通に使う語」が混ざる
#   zipf 3.0 未満 … 直した先が珍しすぎて、直す根拠にならない
MIN_ZIPF_GAP = 2.0
MIN_CANDIDATE_ZIPF = 3.0

# 断片の親とみなす語の下限。**MIN_CANDIDATE_ZIPF とは別の問いに答える。**
# あちらは「直した先は書き換えを正当化できるほど普通の語か」、こちらは
# 「この並びは実在の語が切れたものでありうるか」。行で切れるのに珍しさは
# 関係ないので、こちらはずっと低くてよい。
# 3.0 だと ハイクオリティ(2.99) ウエイト(2.94) が親に入らず、
# ハイク → バイク / ウエイ → ウェイ という誤りが出た（実測）。
# 2.5 まで下げるとこの 2 件と ホームス が止まり、失う正しいルールは
# 62 冊で 2 件（パルト / ロピー）だった。誤りは文字列を壊すので、
# 取りこぼしより高くつく。
MIN_FRAGMENT_HOST_ZIPF = 2.5


def substitutions_at(word, index):
    """位置 index で試す置き換えを返す。

    小書き化は語頭を除く（語頭に小書きのカナは来ない）。濁点・半濁点の
    取り違えは語頭にも起きるので除かない（パッグ -> バッグ）。
    """
    subs = set(CONFUSABLE.get(word[index], ()))
    if index > 0 and word[index] in BIG_TO_SMALL:
        subs.add(BIG_TO_SMALL[word[index]])
    return subs


def frequency_candidates(word):
    """混同しやすい字を置き換えた候補を、組み合わせすべて返す。

    1 文字ずつではなく総当たりにする理由は corrected() と同じ。
    1 語に複数の誤読が入る（ポジテイプ -> ポジティブ）ため。
    """
    slots = [i for i in range(len(word)) if substitutions_at(word, i)]
    if not slots:
        return
    combinations = 1
    for i in slots:
        combinations *= len(substitutions_at(word, i)) + 1
    if combinations > MAX_FREQUENCY_COMBINATIONS:
        print(f"組み合わせが多すぎるため調べません: {word}", file=sys.stderr)
        return
    for size in range(1, len(slots) + 1):
        for combo in itertools.combinations(slots, size):
            options = [sorted(substitutions_at(word, i)) for i in combo]
            for replacement in itertools.product(*options):
                chars = list(word)
                for i, ch in zip(combo, replacement, strict=True):
                    chars[i] = ch
                yield "".join(chars)


def load_frequencies():
    """wordfreq の日本語頻度表を返す。入っていなければ ImportError。

    ``get_frequency_dict`` は表をそのまま返すので **MeCab を必要としない**。
    ``zipf_frequency()`` は語を分かち書きするため MeCab を要求するが、
    こちらは語をそのまま引くだけなのでネイティブ依存が要らない。
    """
    import wordfreq

    return wordfreq.get_frequency_dict("ja")


def zipf_of(frequencies, word):
    """Zipf 頻度 (log10(出現率 * 1e9))。表に無ければ 0.0。"""
    frequency = frequencies.get(word)
    return 0.0 if not frequency else math.log10(frequency * 1e9)


def build_fragment_index(frequencies, *, min_zipf=MIN_FRAGMENT_HOST_ZIPF):
    """行で切れた語を見分けるための、頻出語の先頭・末尾の並びの集合を返す。

    PDF のテキスト層は行単位なので、行をまたぐ語は断片として現れる
    (テーブル -> ``テーブ`` + ``ル``)。断片は辞書に無いので必ず「珍しい語」に
    なり、頻度差だけで見ると別の語への誤読に化ける (テーブ -> テープ)。

    断片は**正しく読めた語**が切れたものなので、頻出語の先頭か末尾に一致する。
    誤読はそうならない (フィードバツク はどの語の一部でもない)。この違いで
    両者を分ける。実データではこれで テーブ / ブラジ(ル) / パジャ(マ) /
    (ショッ)ピング / クラフ(ト) が落ち、正しいルールは 1 つも失われなかった。

    **コーパス側の語をこの索引に入れてはいけない。** 誤読語も入ってしまい、
    フィードバツク が フィードバツクコメント の断片とみなされて落ちる。
    """
    prefixes: set[str] = set()
    suffixes: set[str] = set()
    for word in frequencies:
        if len(word) < 4 or zipf_of(frequencies, word) < min_zipf:
            continue
        for i in range(3, len(word)):
            prefixes.add(word[:i])
            suffixes.add(word[-i:])
    return prefixes | suffixes


# これ以上の頻度で使われている語を「直す」なら、実在の語を別の語に
# 書き換えている可能性がある。棄却はしない（コービー 2.60 / メデイア 2.23 の
# ように正しいものも含まれる）が、人が見るところに載せる
SUSPICIOUS_BASE_ZIPF = 2.0


def already_common_rules(found, *, min_base=SUSPICIOUS_BASE_ZIPF):
    """観測語自体がそこそこ使われている語のルールを挙げる。

    採用の条件は候補との**差**しか見ていないので、それ自体が実在の語でも
    もっとよく使う語との差が開いていれば通ってしまう
    (ガメラ → カメラ、ジングル → シングル)。頻度表を数えると、この形の語は
    242 件あった。棄却するとコービー(2.60) のような正しいものまで落ちるので、
    報告に載せて人に見せる。
    """
    return [(w, r, base) for w, r, _c, base, _z in found if base >= min_base]


def straddling_rules(found, text):
    """語の境目に当たりうるルールを選び出す。[(誤, 正, [巻き込む語, ...]), ...]

    replacements.json の ``literal`` は語の境界を見ない ``str.replace`` なので、
    短いキーは**別の語の途中**に当たる。実例が既に注記されている:
    ``カツプ -> カップ`` を入れると ``トンカツプレート`` (トンカツ + プレート)
    が ``トンカッ`` + ``レート`` に化ける。

    そこで、キーがコーパス中の**より長いカタカナ語の一部**として現れるものを
    挙げる。誤読語の一部として現れるだけなら直して構わないので、落とさずに
    「要確認」として出し、人が見て決める。
    """
    tokens = set(KATAKANA_WORD.findall(text))
    risky = []
    for wrong, right, *_rest in found:
        hosts = sorted(t for t in tokens if t != wrong and wrong in t)
        if hosts:
            risky.append((wrong, right, hosts))
    return risky


def drop_fragments_of_other_misreads(found):
    """誤読語が行で切れた断片を落とす。

    正しい語の断片は build_fragment_index で落ちるが、**誤読語**の断片は
    辞書に無いので落ちない (ハンパーグ -> "ハン" + "パーグ" の パーグ)。

    **文字列が重なっているだけでは落とさない。** 短いほうが長いほうの一部で
    あっても、両方の直し方が食い違わないなら独立したルールとして正しい:

        ネツト -> ネット      ネツトワーク -> ネットワーク    ← 両立する
        パーグ -> パーク      ハンパーグ -> ハンバーグ        ← 食い違う（断片）

    replacements.json は長いキーから順に当てるので、長いほうが先に効いて
    短いほうが残りを拾う。両立するルールを落とすと、単独で出てきた語が
    直らなくなる（実データの ネツト / インターネツト / ネツトワーク は
    3 つとも辞書に入っている）。
    """
    kept = []
    for wrong, right, *rest in found:
        piece = False
        for other_wrong, other_right, *_ in found:
            if other_wrong == wrong:
                continue
            if other_wrong.startswith(wrong):
                piece = not other_right.startswith(right)
            elif other_wrong.endswith(wrong):
                piece = not other_right.endswith(right)
            if piece:
                break
        if not piece:
            kept.append((wrong, right, *rest))
    return kept


def mine_by_frequency(
    words,
    frequencies,
    *,
    min_gap=MIN_ZIPF_GAP,
    min_zipf=MIN_CANDIDATE_ZIPF,
    min_count=2,
):
    """頻度差で誤読とみなせる語を返す。(採用, 判断保留)。

    採用は [(誤, 正, 誤の回数, 誤の zipf, 正の zipf), ...]、
    判断保留は [(誤, 誤の回数, [(候補, zipf), ...]), ...]。

    **直し方が 1 通りに決まらない語は採らない。** パッグ からは バッグ (4.44)
    と パック (4.48) の両方が、どちらも 1 箇所だけ直して得られる。頻度が最大の
    ものを選ぶと パック になるが、実データでの正解は バッグ だった
    （「キタムラのパッグ」）。決められないものは人が見るほうに回す。
    """
    found = []
    ambiguous = []
    fragments = build_fragment_index(frequencies)
    for word, count in words.items():
        # 1 度しか出ない語は証拠が弱い。実データでは漫画の効果音が混ざった
        if count < min_count or word in fragments:
            continue
        base = zipf_of(frequencies, word)
        passing = []
        for candidate in frequency_candidates(word):
            candidate_zipf = zipf_of(frequencies, candidate)
            if candidate_zipf < min_zipf or candidate_zipf - base < min_gap:
                continue
            edits = sum(1 for a, b in zip(word, candidate, strict=True) if a != b)
            passing.append((candidate, candidate_zipf, edits))
        if not passing:
            continue
        # 直した箇所が最も少ない候補を採る。総当たりなので「2 箇所直せば
        # 別の語になる」候補まで出てくる (ヒツト からは 1 箇所の ヒット と
        # 2 箇所の ビット が両方閾値を超える)。同数で並んだら決められない
        fewest = min(edits for _c, _z, edits in passing)
        best = [item for item in passing if item[2] == fewest]
        if len(best) == 1:
            found.append((word, best[0][0], count, base, best[0][1]))
        else:
            ambiguous.append(
                (word, count, [(c, z) for c, z, _e in sorted(passing, key=lambda x: -x[1])])
            )

    found = drop_fragments_of_other_misreads(found)
    found.sort(key=lambda item: (-item[2], item[0]))
    ambiguous.sort(key=lambda item: (-item[1], item[0]))
    return found, ambiguous


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0] or None)
    parser.add_argument("paths", nargs="+", help="PDF / テキスト / それらを含むフォルダ")
    parser.add_argument(
        "--evidence",
        choices=("corpus", "frequency"),
        default="corpus",
        help=(
            "何を証拠に誤読とみなすか。corpus: 同じ本の中で正しい形のほうが多い"
            "（既定・小書きカナ向け）。frequency: 外部の頻度表と比べる"
            '（濁点・半濁点向け。pip install "wordfreq>=3.1,<4" が要る）'
        ),
    )
    parser.add_argument(
        "--min-gap",
        type=float,
        default=MIN_ZIPF_GAP,
        help=f"--evidence frequency で要求する Zipf 頻度の差（既定: {MIN_ZIPF_GAP}）",
    )
    parser.add_argument(
        "--min-zipf",
        type=float,
        default=MIN_CANDIDATE_ZIPF,
        help=f"--evidence frequency で直した先に要求する Zipf 頻度（既定: {MIN_CANDIDATE_ZIPF}）",
    )
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
        help=(
            "最低出現回数（既定: 2）。corpus では**正しい形**の、"
            "frequency では**観測した語**の回数を見る"
        ),
    )
    parser.add_argument("--out", help="replacements.json に貼れる JSON の書き出し先")
    parser.add_argument(
        "--force", action="store_true", help="--out の書き出し先が既にあっても上書きする"
    )
    args = parser.parse_args(argv)

    # 経路ごとに効かないオプションがある。黙って無視すると、指定したのに
    # 何も変わらない理由が分からない
    given = set(argv if argv is not None else sys.argv[1:])
    ignored = (
        {"--min-ratio"} if args.evidence == "frequency" else {"--min-gap", "--min-zipf"}
    ) & given
    if ignored:
        print(
            f"--evidence {args.evidence} では {' '.join(sorted(ignored))} は効きません",
            file=sys.stderr,
        )

    # 既存ファイルを黙って潰さない。--out replacements.json と打たれると
    # regex ルールも、手で外した判断も消える
    if args.out and os.path.exists(args.out) and not args.force:
        print(f"既にあります（--force で上書き）: {args.out}", file=sys.stderr)
        return 1

    # 頻度表の有無は PDF を読む前に確かめる。62 冊読んでから
    # 「入っていません」と言われても数分を捨てるだけになる
    frequencies = None
    if args.evidence == "frequency":
        try:
            frequencies = load_frequencies()
        except ImportError as e:
            print(
                f'頻度表を読み込めません（{e}）。pip install "wordfreq>=3.1,<4" で入ります',
                file=sys.stderr,
            )
            return 1

    text, files = load_corpus(args.paths)
    if not files:
        print("読めるファイルがありません", file=sys.stderr)
        return 1
    words = count_words(text)
    print(f"{files} ファイル / カタカナ語 {len(words)} 種 {sum(words.values())} 件")

    if frequencies is not None:
        found, ambiguous = mine_by_frequency(
            words,
            frequencies,
            min_gap=args.min_gap,
            min_zipf=args.min_zipf,
            min_count=args.min_count,
        )
        print(f"誤読とみなせる語 {len(found)} 種 {sum(item[2] for item in found)} 件\n")
        print(f"{'誤(回)':>7}  誤 -> 正   (zipf)")
        for wrong, right, count, base, candidate_zipf in found:
            print(f"{count:7d}  {wrong} -> {right}   {base:.2f} -> {candidate_zipf:.2f}")
        if ambiguous:
            # 落としたものを黙って消さない。人が見て決めるためにそのまま出す
            print(f"\n判断保留（候補が 2 つ以上）{len(ambiguous)} 語")
            for wrong, count, candidates in ambiguous:
                shown = ", ".join(f"{c}({z:.2f})" for c, z in candidates)
                print(f"{count:7d}  {wrong} -> {shown}")
        risky = straddling_rules(found, text)
        if risky:
            print(f"\n要確認（より長い語の一部として現れる）{len(risky)} 語")
            for wrong, right, hosts in risky:
                shown = ", ".join(hosts[:4]) + (" ..." if len(hosts) > 4 else "")
                print(f"         {wrong} -> {right}   巻き込む語: {shown}")
        common = already_common_rules(found)
        if common:
            print(f"\n要確認（直そうとしている語自体もそこそこ使われる）{len(common)} 語")
            for wrong, right, base in common:
                print(f"         {wrong} -> {right}   {wrong} の zipf {base:.2f}")
        found = [(w, r, c, 0) for w, r, c, _b, _z in found]
    else:
        found = mine(words, min_ratio=args.min_ratio, min_count=args.min_count)
        print(f"誤読とみなせる語 {len(found)} 種 {sum(item[2] for item in found)} 件\n")
        print(f"{'誤(回)':>7} {'正(回)':>7}  誤 -> 正")
        for wrong, right, wrong_count, right_count in found:
            print(f"{wrong_count:7d} {right_count:7d}  {wrong} -> {right}")

    if args.out:
        # キー順で書く。回数順にすると流し直すたびに diff が意味なく揺れる
        rules = {"literal": dict(sorted((wrong, right) for wrong, right, _w, _r in found))}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"\n書き出しました: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
