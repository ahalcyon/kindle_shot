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
