"""scripts/mine_misreads.py のテスト（小書きカナの誤読を出力から洗い出す）

一般規則（「ツ の後ろが無声子音なら促音」など）が実データで誤爆することを
確認したうえで語単位の literal ルールに寄せた経緯があるため、
「コーパスに裏付けが無い語は拾わない」ことを固定する。
"""

import importlib.util
import json
import os

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "mine_misreads.py")
_spec = importlib.util.spec_from_file_location("mine_misreads", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"読み込めない: {_SCRIPT}"
mine_misreads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mine_misreads)


def mine(text, **kw):
    return mine_misreads.mine(mine_misreads.count_words(text), **kw)


def test_finds_a_misread_when_the_corrected_form_is_more_common():
    text = "フィードバック " * 5 + "フィードバツク"
    assert mine(text) == [("フィードバツク", "フィードバック", 1, 5)]


def test_ignores_a_word_whose_corrected_form_never_appears():
    """裏付けが無ければ拾わない。

    ``キャベツステーキ`` を ``キャベッステーキ`` にしてしまう類の誤爆を防ぐ。
    実データで一般規則を当てて確認した誤爆の一つ。
    """
    assert mine("キャベツステーキ " * 10) == []


def test_ignores_a_corrected_form_that_is_not_common_enough():
    """正しい形のほうが少なければ、そちらが誤読の可能性がある。"""
    assert mine("テイクアウト " * 10 + "ティクアウト ") == []


def test_corrects_every_misread_in_one_word():
    """1 語に複数の誤読が入っていても、まとめて直した形を採る。

    置換辞書はルールを連鎖させないので、1 文字だけ直した形を答えにすると
    ``ギャツプフィードバツク`` のような中途半端な形で止まってしまう。
    """
    text = "ギャップフィードバック " * 5 + "ギャツプフイードバツク"
    assert mine(text) == [("ギャツプフイードバツク", "ギャップフィードバック", 1, 5)]


def test_does_not_touch_the_first_character():
    """小書きのカナは語頭に来ない。"""
    assert mine("ャリア " * 5 + "ヤリア") == []


def test_prefers_the_fully_corrected_form_over_a_half_corrected_one():
    """一部だけ直した形がコーパスに多くても、そちらは採らない。

    回数が最大の候補を採ると ``フイードバツク -> フイードバック`` という、
    誤読を別の誤読に書き換えるだけのルールができる。置換辞書はルールを
    連鎖させないので、それでは直りきらない。
    """
    text = "フィードバック " * 20 + "フイードバック " * 30 + "フイードバツク"
    rules = {wrong: right for wrong, right, _w, _r in mine(text)}
    assert rules["フイードバツク"] == "フィードバック"


def test_skips_a_word_with_too_many_big_kana(capsys):
    """大書きが多すぎる語は調べない。黙って捨てず理由を出す。

    抽出器が語の間に空白を入れなかった行では、長い連なりが 1 語になる。
    組み合わせが爆発するので諦めるが、区別が付かないと手当てできない。
    """
    word = "ア" + "ツ" * 13
    assert mine(word + " " + word) == []
    assert "大書きが多すぎる" in capsys.readouterr().err


def test_does_not_join_words_across_files(tmp_path):
    """ファイルの末尾と次のファイルの先頭がつながらない。

    つながると、実在しない語が 1 つでき、本物の語の回数がその分減る。
    """
    (tmp_path / "a.txt").write_text("フィード", encoding="utf-8")
    (tmp_path / "b.txt").write_text("バック", encoding="utf-8")

    text, files = mine_misreads.load_corpus([str(tmp_path)])

    assert files == 2
    assert "フィードバック" not in text


def test_drops_spaces_inside_a_line_but_keeps_line_breaks(tmp_path):
    """抽出器が字間に入れた空白は落とすが、改行は残す。

    改行まで落とすと隣り合うカタカナ語がつながって 1 語になり、
    語として数えられなくなる。
    """
    source = tmp_path / "book.txt"
    source.write_text("フィ ード バック\nメッセージ\n", encoding="utf-8")

    text, _files = mine_misreads.load_corpus([str(source)])

    assert "フィードバック" in text
    assert "フィードバックメッセージ" not in text


def test_reads_a_path_that_looks_like_a_glob(tmp_path):
    """書名に [ ] を含む本を、glob のパターンと取り違えない。"""
    (tmp_path / "面接質問50 [新版].txt").write_text("フィードバック\n" * 3, encoding="utf-8")
    (tmp_path / "面接質問505.txt").write_text("まぎらわしいほう\n", encoding="utf-8")

    text, files = mine_misreads.load_corpus([str(tmp_path / "面接質問50 [新版].txt")])

    assert files == 1
    assert "まぎらわしいほう" not in text


def test_reads_a_text_file_and_writes_rules(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("フィードバック\n" * 5 + "フィードバツク\n", encoding="utf-8")
    out = tmp_path / "rules.json"

    assert mine_misreads.main([str(source), "--out", str(out)]) == 0

    rules = json.loads(out.read_text(encoding="utf-8"))
    assert rules == {"literal": {"フィードバツク": "フィードバック"}}


def test_does_not_overwrite_an_existing_file_without_force(tmp_path, capsys):
    """--out replacements.json と打たれても、既存の辞書を潰さない。

    書き出すのは literal だけなので、上書きすると regex ルールごと消える。
    """
    source = tmp_path / "book.txt"
    source.write_text("フィードバック\n" * 5 + "フィードバツク\n", encoding="utf-8")
    out = tmp_path / "rules.json"
    out.write_text('{"literal": {}, "regex": []}', encoding="utf-8")

    assert mine_misreads.main([str(source), "--out", str(out)]) == 1
    assert "既にあります" in capsys.readouterr().err
    assert "regex" in out.read_text(encoding="utf-8")

    assert mine_misreads.main([str(source), "--out", str(out), "--force"]) == 0
    assert "regex" not in out.read_text(encoding="utf-8")


def test_reports_a_path_that_does_not_exist(tmp_path, capsys):
    assert mine_misreads.main([str(tmp_path / "missing.txt")]) == 1
    err = capsys.readouterr().err
    assert "見つかりません" in err
    assert "読めるファイルがありません" in err


# ============================================================
# 頻度コーパスを証拠にする経路 (#44)
# ============================================================
#
# 実データ 62 冊で閾値と除外規則を決めた経緯があるので、
# 「何を拾わないか」を固定する。誤って拾うと**正しく読めている文字列が壊れる**。


def frequencies(**words):
    """zipf 値から wordfreq 形式の頻度表を作る（zipf = log10(頻度 * 1e9)）。"""
    return {word: 10 ** (zipf - 9) for word, zipf in words.items()}


def mine_freq(text, freq, **kw):
    found, ambiguous = mine_misreads.mine_by_frequency(mine_misreads.count_words(text), freq, **kw)
    return [(w, r) for w, r, *_ in found], ambiguous


def test_finds_a_dakuten_misread_the_corpus_cannot_prove():
    """正しい形が同じ本に 1 度も出てこなくても拾える。

    corpus 経路の構造的な取りこぼしがここ。イベント が 1 度も現れない本でも
    イペント は誤読だと分かる。
    """
    found, _ = mine_freq("イペント " * 3, frequencies(イペント=0.0, イベント=5.21))
    assert found == [("イペント", "イベント")]


def test_ignores_a_pair_that_are_both_ordinary_words():
    """バス/パス のように両方よく使う語は直さない（差が小さい）。"""
    found, _ = mine_freq("パス " * 3, frequencies(パス=4.42, バス=5.04))
    assert found == []


def test_ignores_a_candidate_that_is_itself_rare():
    """直した先が珍しすぎるなら、直す根拠にならない。"""
    found, _ = mine_freq("アバア " * 3, frequencies(アバア=0.0, アパア=2.0))
    assert found == []


def test_holds_back_a_word_with_two_plausible_candidates():
    """候補が 2 つ以上残ったら採らない。

    パッグ からは バッグ と パック の両方が閾値を超える。頻度が最大のものを
    採ると パック になるが、実データでの正解は バッグ だった。
    """
    found, ambiguous = mine_freq("パッグ " * 3, frequencies(パッグ=0.0, バッグ=4.44, パック=4.48))
    assert found == []
    assert [w for w, _n, _c in ambiguous] == ["パッグ"]


def test_ignores_a_word_cut_by_a_line_break():
    """行をまたいで切れた語を別の語に化けさせない。

    PDF のテキスト層は行単位なので テーブル は "テーブ" + "ル" に切れる。
    断片は頻出語の先頭・末尾に一致するので、そこで見分ける。
    """
    found, _ = mine_freq("テーブ " * 3, frequencies(テーブ=0.0, テープ=4.35, テーブル=4.40))
    assert found == []


def test_a_misread_is_not_mistaken_for_a_fragment():
    """誤読は頻出語の一部にならないので、断片の除外に巻き込まれない。"""
    found, _ = mine_freq(
        "イペント " * 3, frequencies(イペント=0.0, イベント=5.21, イベントカレンダー=3.5)
    )
    assert found == [("イペント", "イベント")]


def test_ignores_a_word_seen_only_once():
    """1 度しか出ない語は証拠が弱い（実データでは漫画の効果音が混ざった）。"""
    found, _ = mine_freq("イペント", frequencies(イペント=0.0, イベント=5.21))
    assert found == []


def test_fixes_a_misread_at_the_start_of_a_word():
    """濁点・半濁点の取り違えは語頭にも起きる（小書き化と違う点）。"""
    found, _ = mine_freq("ポリューム " * 3, frequencies(ポリューム=0.0, ボリューム=4.5))
    assert found == [("ポリューム", "ボリューム")]


def test_drops_a_fragment_of_another_mined_misread():
    """誤読語が行で切れた断片を拾わない（ハンパーグ -> "ハン" + "パーグ"）。"""
    found, _ = mine_freq(
        "ハンパーグ " * 3 + "パーグ " * 3,
        frequencies(ハンパーグ=0.0, ハンバーグ=3.83, パーグ=0.0, パーク=4.21),
    )
    assert found == [("ハンパーグ", "ハンバーグ")]


def test_reports_rules_that_can_straddle_a_word_boundary():
    """literal は語の境界を見ないので、別の語の途中に当たるものを挙げる。

    カツプ -> カップ を入れると トンカツプレート が壊れる、という実例が
    replacements.json に注記されている。機械では可否を決められないので報告する。
    """
    risky = mine_misreads.straddling_rules([("カツプ", "カップ")], "トンカツプレート カツプ")
    assert risky == [("カツプ", "カップ", ["トンカツプレート"])]


def test_does_not_report_a_rule_that_stands_alone():
    assert mine_misreads.straddling_rules([("イペント", "イベント")], "イペント") == []


def test_reports_how_to_install_the_frequency_table(tmp_path, monkeypatch, capsys):
    """頻度表が入っていないときに、入れ方まで案内する。

    verify は optional group なので、既定の環境では入っていない。
    「使えません」だけだと利用者が次に何をすればよいか分からない。
    """
    source = tmp_path / "book.txt"
    source.write_text("イペント\n" * 3, encoding="utf-8")

    def missing():
        raise ImportError("No module named 'wordfreq'")

    monkeypatch.setattr(mine_misreads, "load_frequencies", missing)
    assert mine_misreads.main([str(source), "--evidence", "frequency"]) == 1
    err = capsys.readouterr().err
    assert "wordfreq" in err
    assert '".[verify]"' in err


def test_does_not_need_the_frequency_table_by_default(tmp_path, monkeypatch):
    """既定 (--evidence corpus) では頻度表を読みに行かない。"""

    def fail():
        raise AssertionError("corpus 経路で頻度表を読んではいけない")

    monkeypatch.setattr(mine_misreads, "load_frequencies", fail)
    source = tmp_path / "book.txt"
    source.write_text("フィードバック\n" * 5 + "フィードバツク\n", encoding="utf-8")
    assert mine_misreads.main([str(source)]) == 0
