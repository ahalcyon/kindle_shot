"""scripts/audit_rules.py のテスト（置換規則をコーパスに当てて点検する）

「置換後の形がコーパスに一度も出てこない」ものを疑う、という判定を固定する。
ふつうの誤読直しは直した形が同じコーパスに正しく出ているので挙がらず、
正しい語を壊す規則（アップロード -> アッブロード）は挙がる。
"""

import importlib.util
import os

from core.text_replacements import Replacer

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "audit_rules.py")
_spec = importlib.util.spec_from_file_location("audit_rules", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"読み込めない: {_SCRIPT}"
audit_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_rules)


def runs(text):
    return audit_rules.katakana_runs(text)


def test_a_rule_that_breaks_a_correct_word_is_reported():
    text = "アップロードした。アップロードする。アップロードが終わる。"
    found = audit_rules.suspicious(runs(text), Replacer({"プロード": "ブロード"}), min_count=3)
    assert found == [(3, "アップロード", "アッブロード", None)]


def test_an_ordinary_misread_fix_is_not_reported():
    """直した形がコーパスに実在するなら、ふつうの誤読直しとみなす。"""
    text = "クロープを入れる。クロープを挽く。クロープの香り。クローブの香り。"
    found = audit_rules.suspicious(runs(text), Replacer({"クロープ": "クローブ"}), min_count=3)
    assert found == []


def test_rare_runs_are_skipped():
    text = "アップロードした。"
    assert audit_rules.suspicious(runs(text), Replacer({"プロード": "ブロード"}), min_count=3) == []


def test_a_run_keeps_the_dots_and_dashes_that_hold_a_name_together():
    """中黒と長音符は連続に含める。人名を 1 つのまとまりとして見るため。

    含めないと エドウイン・ボーチャード が エドウイン と ボーチャード に
    割れ、「ボーチャード という正しい語を壊している」ことに気づけない。
    """
    assert audit_rules.katakana_runs("エドウイン・ボーチャード教授") == {
        "エドウイン・ボーチャード": 1
    }


def test_rule_detail_ignores_min_count():
    """1 件しか出ない語こそ、規則を入れるか外すかの判断材料になる。"""
    text = "プロードバンド。アップロードする。"
    rows = audit_rules.rule_detail(runs(text), Replacer({"プロード": "ブロード"}), "プロード")
    assert len(rows) == 2


def test_rule_detail_lists_every_run_that_contains_the_key():
    text = "アップロードする。ブロードバンド。プロードバンド。プロードバンド。"
    rows = audit_rules.rule_detail(runs(text), Replacer({"プロード": "ブロード"}), "プロード")
    assert rows == [
        (2, "プロードバンド", "ブロードバンド"),
        (1, "アップロード", "アッブロード"),
    ]


def test_a_rule_that_only_half_fixes_is_reported():
    """置換後にまだ誤読が残る規則を挙げる (#68)。

    長いキーが先に当たると短いキーの出番が来ず、右辺に誤読が残る。
    辞書のテストは規則の右辺しか見ないので、この形は捕まえられなかった。
    """
    # 本文には正しい「キットカット」も出てくる。だから「あるべき形」が分かる
    text = "キツトカツトの話。キツトカツトを買う。キツトカツトが好き。キットカットは美味しい。"
    found = audit_rules.suspicious(runs(text), Replacer({"カツト": "カット"}), min_count=3)
    assert found == [(3, "キツトカツト", "キツトカット", "キットカット")]


def test_a_form_the_corpus_does_not_know_is_left_alone():
    """小書きに直した形がコーパスに無ければ、何も言わない。

    固有名詞や造語を勝手に直さない。
    """
    text = "キツトカツトの話。キツトカツトを買う。キツトカツトが好き。"
    found = audit_rules.suspicious(runs(text), Replacer({"カツト": "カット"}), min_count=3)
    assert found == [(3, "キツトカツト", "キツトカット", None)]
