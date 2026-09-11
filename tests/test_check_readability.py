"""check_readability の契約テスト (#65)

342 冊のコピペをどれだけ信用してよいかを機械的に出すツール。判定の境目と、
「割合が当てにならない場合にそう言うこと」を固定する。

wordfreq は任意依存で CI には入っていないので、頻度表は偽物を組み立てる
（tests/test_mine_misreads.py と同じ流儀）。決定的になる利点もある。
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import check_readability as cr  # noqa: E402


def frequencies(**words):
    """zipf 値から wordfreq 形式の頻度表を作る（zipf = log10(頻度 * 1e9)）。"""
    return {word: 10 ** (zipf - 9) for word, zipf in words.items()}


FREQ = frequencies(
    セックス=4.0,
    ポルノ=3.5,
    パスタ=4.2,
    ディズニー=4.0,
    テープ=4.0,
    テーブル=4.0,
)
# 行で切れた断片を見分けるための索引（頻出語の先頭・末尾の並び）
FRAGMENTS = cr.build_fragment_index(FREQ)


def correction(word, freq=None, fragments=frozenset()):
    return cr.correction_for(word, freq if freq is not None else FREQ, {}, fragments)


def verdict_input(**over):
    m = {
        "chars": 90000,
        "pages": 100,
        "empty_page_ratio": 0.0,
        "japanese_ratio": 0.9,
        "hiragana_ratio": 0.4,
        "katakana_words": 3500,
        "misread_rate": 0.005,
        "misread_hits": 100,
        "misreads_per_1000": 0.2,
        "run_ratio": 0.0,
    }
    m.update(over)
    return m


# ------------------------------------------------------------
# 誤読の見分け
# ------------------------------------------------------------


def test_finds_a_small_kana_misread():
    """セツクス は実在しないが、小書きに直すと頻度の高い語になる。"""
    assert correction("セツクス") == "セックス"


def test_finds_a_dakuten_misread():
    assert correction("ボルノ") == "ポルノ"


def test_finds_a_combined_misread():
    """1 語に小書きと濁点の誤読が同時に入ることがある。"""
    assert correction("デイズニー") == "ディズニー"


def test_leaves_a_real_word_alone():
    assert correction("パスタ") is None


def test_leaves_an_unknown_proper_noun_alone():
    """頻度表に無いだけの固有名詞を誤読にしない。

    「未知語」をそのまま数えると専門用語を巻き込む（実測で洋書 100% / 数学書 71%）。
    """
    assert correction("ヴィチェンツァ") is None


def test_ignores_a_correction_that_is_still_rare():
    """直した先が珍しい語なら、誤読とは言えない。"""
    assert correction("セツクス", frequencies(セックス=2.0)) is None


def test_does_not_treat_a_line_break_fragment_as_a_misread():
    """PDF のテキスト層は行単位なので、行をまたぐ語は断片として現れる。

    テーブル が行末で切れると テーブ が独立した語に見え、1 文字置換で
    テープ になる。断片の数は段組や判型に比例するので、除かないと指標に
    「OCR の質」ではなく「レイアウト」が混じる。
    """
    assert correction("テーブ") == "テープ"  # 索引を渡さなければ誤検出する
    assert correction("テーブ", fragments=FRAGMENTS) is None


# ------------------------------------------------------------
# 判定
# ------------------------------------------------------------


def test_verdict_needs_enough_text():
    assert cr.verdict(verdict_input(chars=10))[0] == "テキスト層なし"


def test_verdict_notices_mostly_empty_pages():
    """本全体の合計で見ると、大半のページが 1 文字も取れていない本が通る。"""
    assert cr.verdict(verdict_input(empty_page_ratio=0.8))[0] == "テキスト層なし"


def test_verdict_skips_non_japanese_books():
    """洋書はカタカナ語がほとんど無く、割合が当てにならない。"""
    m = verdict_input(japanese_ratio=0.01, hiragana_ratio=0.0, katakana_words=3, misread_hits=3)
    assert cr.verdict(m)[0] == "対象外"


def test_verdict_does_not_call_a_broken_japanese_book_out_of_scope():
    """洋書と「OCR が総崩れした和書」を同じ箱に入れない。

    「対象外」は既定の出力から外れるので、入れると最悪の本が最も静かに消える。
    """
    assert cr.verdict(verdict_input(japanese_ratio=0.1, hiragana_ratio=0.08))[0] == "要確認"


def test_verdict_admits_when_the_sample_is_too_small():
    assert cr.verdict(verdict_input(katakana_words=5, misread_hits=2))[0] == "判定不能"


def test_verdict_says_unknown_instead_of_ok_when_hits_are_few():
    """「情報が足りない」を「問題が無い」と言い換えない。

    実測で「2 箇所しかないのに 6.1%」という本が出た。割合だけで格上げしないが、
    OK とも言わない（既定の出力から消えてしまう）。
    """
    m = verdict_input(katakana_words=33, misread_rate=0.061, misread_hits=2)
    assert cr.verdict(m)[0] == "判定不能"


def test_verdict_adds_runs_as_a_supporting_signal():
    """同じ文字の連なりは単独では判断材料にならない（マンガの擬音でも高く出る）。"""
    got, reason = cr.verdict(verdict_input(run_ratio=0.2))
    assert got == "要確認"
    assert "連なり" in reason


def test_verdict_flags_a_bad_book():
    m = verdict_input(misread_rate=0.142, misread_hits=496, misreads_per_1000=2.5)
    assert cr.verdict(m)[0] == "怪しい"


def test_verdict_marks_the_middle_band():
    assert cr.verdict(verdict_input(misread_hits=140, misreads_per_1000=1.2))[0] == "要確認"


def test_verdict_passes_a_clean_book():
    assert cr.verdict(verdict_input())[0] == "OK"


# ------------------------------------------------------------
# 計測
# ------------------------------------------------------------


def test_measure_counts_hits_not_kinds():
    """同じ誤読が何度も出る本を、種類数だけで軽く見ない。"""
    m = cr.measure(["。".join(["セツクス"] * 5 + ["パスタ"] * 5)], FREQ, {})
    assert m["misread_kinds"] == 1
    assert m["misread_hits"] == 5
    assert m["katakana_words"] == 10


def test_measure_counts_empty_pages():
    m = cr.measure(["本文があるページ。", "", "  ", "もう 1 ページ。"], FREQ, {})
    assert m["pages"] == 4
    assert m["empty_page_ratio"] == 0.5


def test_a_long_katakana_run_is_one_word():
    """OCR は区切りなしでカタカナを連結することがある。1 語として扱われる。

    そういう語は組み合わせが多すぎて調べられず、誤読として拾えない。
    取りこぼす側に倒れるのは意図どおり。
    """
    assert cr.measure(["セツクスパスタドラツグ"], FREQ, {})["katakana_words"] == 1


def test_measure_reports_japanese_ratio():
    assert cr.measure(["これは日本語の文章です。"], FREQ, {})["japanese_ratio"] > 0.8
    assert cr.measure(["This is an English sentence."], FREQ, {})["japanese_ratio"] == 0.0


def test_measure_uses_a_per_1000_characters_rate():
    """用途は「コピペしてメモ」なので、1000 文字あたり何語壊れているかが利用者の単位。"""
    m = cr.measure(["。".join(["セツクス"] * 10)], FREQ, {})
    assert m["misread_hits"] == 10
    assert m["misreads_per_1000"] > 100  # 短い入力なので大きく出る
