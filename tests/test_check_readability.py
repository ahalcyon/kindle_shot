"""check_readability の契約テスト (#65)

342 冊のコピペをどれだけ信用してよいかを機械的に出すツール。判定の境目は
実測分布から決めているので、境目の振る舞いと「割合が当てにならない場合」を固定する。

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
)


def correction(word, freq=None):
    return cr.correction_for(word, freq if freq is not None else FREQ, {})


def test_finds_a_small_kana_misread():
    """セツクス は実在しないが、小書きに直すと頻度の高い語になる。"""
    assert correction("セツクス") == "セックス"


def test_finds_a_dakuten_misread():
    assert correction("ボルノ") == "ポルノ"


def test_finds_a_combined_misread():
    """1 語に小書きと濁点の誤読が同時に入ることがある。"""
    assert correction("デイズニー") == "ディズニー"


def test_leaves_a_real_word_alone():
    """実在する語を誤読とみなさない。"""
    assert correction("パスタ") is None


def test_leaves_an_unknown_proper_noun_alone():
    """頻度表に無いだけの固有名詞を誤読にしない。

    「未知語」をそのまま数えると専門用語を巻き込む（実測で洋書 100% / 数学書 71%）。
    """
    assert correction("ヴィチェンツァ") is None


def test_ignores_a_correction_that_is_still_rare():
    """直した先が珍しい語なら、誤読とは言えない。"""
    rare = frequencies(セックス=2.0)
    assert correction("セツクス", rare) is None


def test_verdict_needs_enough_text():
    m = {
        "chars": 10,
        "japanese_ratio": 0.9,
        "katakana_words": 100,
        "misread_rate": 0.9,
        "misread_hits": 90,
    }
    assert cr.verdict(m)[0] == "テキスト層なし"


def test_verdict_skips_non_japanese_books():
    """洋書はカタカナ語がほとんど無く、割合が当てにならない。"""
    m = {
        "chars": 9000,
        "japanese_ratio": 0.01,
        "katakana_words": 3,
        "misread_rate": 1.0,
        "misread_hits": 3,
    }
    assert cr.verdict(m)[0] == "対象外"


def test_verdict_admits_when_the_sample_is_too_small():
    m = {
        "chars": 9000,
        "japanese_ratio": 0.9,
        "katakana_words": 5,
        "misread_rate": 0.4,
        "misread_hits": 2,
    }
    assert cr.verdict(m)[0] == "判定不能"


def test_verdict_does_not_flag_a_high_rate_with_few_hits():
    """実測で「2 箇所しかないのに 6.1%」という本が出た。割合だけで上げない。"""
    m = {
        "chars": 90000,
        "japanese_ratio": 0.9,
        "katakana_words": 33,
        "misread_rate": 0.061,
        "misread_hits": 2,
    }
    verdict, reason = cr.verdict(m)
    assert verdict == "OK"
    assert "2 箇所" in reason


def test_verdict_flags_a_bad_book():
    m = {
        "chars": 90000,
        "japanese_ratio": 0.9,
        "katakana_words": 3500,
        "misread_rate": 0.142,
        "misread_hits": 496,
    }
    assert cr.verdict(m)[0] == "怪しい"


def test_verdict_marks_the_middle_band():
    m = {
        "chars": 90000,
        "japanese_ratio": 0.9,
        "katakana_words": 3500,
        "misread_rate": 0.04,
        "misread_hits": 140,
    }
    assert cr.verdict(m)[0] == "要確認"


def test_verdict_passes_a_clean_book():
    m = {
        "chars": 90000,
        "japanese_ratio": 0.9,
        "katakana_words": 3500,
        "misread_rate": 0.005,
        "misread_hits": 17,
    }
    assert cr.verdict(m)[0] == "OK"


def test_measure_counts_hits_not_kinds():
    """同じ誤読が何度も出る本を、種類数だけで軽く見ない。"""
    m = cr.measure("。".join(["セツクス"] * 5 + ["パスタ"] * 5), FREQ, {})
    assert m["misread_kinds"] == 1
    assert m["misread_hits"] == 5
    assert m["katakana_words"] == 10


def test_a_long_katakana_run_is_one_word():
    """OCR は区切りなしでカタカナを連結することがある。1 語として扱われる。

    そういう語は組み合わせが多すぎて調べられず、誤読として拾えない。
    取りこぼす側に倒れるのは意図どおり。
    """
    m = cr.measure("セツクスパスタドラツグ", FREQ, {})
    assert m["katakana_words"] == 1


def test_measure_reports_japanese_ratio():
    assert cr.measure("これは日本語の文章です。", FREQ, {})["japanese_ratio"] > 0.8
    assert cr.measure("This is an English sentence.", FREQ, {})["japanese_ratio"] == 0.0
