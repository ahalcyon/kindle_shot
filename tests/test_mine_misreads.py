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


def test_chains_are_folded_into_the_final_form():
    """A -> B と B -> C が出たら A -> C にする。

    置換辞書は各ルールを 1 回ずつしか適用しないので、畳んでおかないと
    A が B 止まりになる。
    """
    text = "フィードバック " * 9 + "フイードバック " * 3 + "フイードバツク"
    rules = {wrong: right for wrong, right, _w, _r in mine(text, min_ratio=2.0, min_count=2)}
    assert rules["フイードバツク"] == "フィードバック"
    assert rules["フイードバック"] == "フィードバック"


def test_reads_a_text_file_and_writes_rules(tmp_path, capsys):
    source = tmp_path / "book.txt"
    source.write_text("フィードバック\n" * 5 + "フィードバツク\n", encoding="utf-8")
    out = tmp_path / "rules.json"

    assert mine_misreads.main([str(source), "--out", str(out)]) == 0

    rules = json.loads(out.read_text(encoding="utf-8"))
    assert rules == {"literal": {"フィードバツク": "フィードバック"}}


def test_reports_when_nothing_can_be_read(tmp_path, capsys):
    assert mine_misreads.main([str(tmp_path / "missing.txt")]) == 1
    assert "読めるファイルがありません" in capsys.readouterr().err
