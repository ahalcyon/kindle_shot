"""scripts/audit_rules.py のテスト（置換規則をコーパスに当てて点検する）

「置換後の形がコーパスに一度も出てこない」ものを疑う、という判定を固定する。
ふつうの誤読直しは直した形が同じコーパスに正しく出ているので挙がらず、
正しい語を壊す規則（アップロード -> アッブロード）は挙がる。
"""

import importlib.util
import json
import os

import pytest

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


# ------------------------------------------------------------
# 収束していない規則（コーパスに聞かない判定）
# ------------------------------------------------------------


def test_a_rule_that_needs_a_second_pass_is_reported():
    """置換を 2 回かけて変わるなら、1 回で直りきっていない (#68)。

    長いキーが先に当たり、短いキーの出番が来ないまま右辺に誤読が残る形。
    **コーパスに聞かない**ので、置換後の形がコーパスに在るために
    suspicious で落ちるもの（still_misread の盲点）もここで拾える。
    """
    rules = {"ウエッジウッド": "ウェッジウッド", "エツジ": "エッジ"}
    text = "ウエツジウッド。ウエツジウッド。ウエツジウッド。"
    found = audit_rules.not_settled(runs(text), Replacer(rules), min_count=3)
    assert found == [(3, "ウエツジウッド", "ウエッジウッド", "ウェッジウッド")]


def test_a_settled_rule_is_not_reported():
    """1 回で直りきる規則は挙げない。"""
    text = "カツト。カツト。カツト。"
    found = audit_rules.not_settled(runs(text), Replacer({"カツト": "カット"}), min_count=3)
    assert found == []


# ------------------------------------------------------------
# 小書きに直す判定そのもの
# ------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "present", "expected"),
    [
        # この差分の判断はどれも「エ -> ェ」経由で出た。ツ だけでは足りない
        ("フエニックス", {"フェニックス"}, "フェニックス"),
        ("サヴエッジ", {"サヴェッジ"}, "サヴェッジ"),
        ("キツトカット", {"キットカット"}, "キットカット"),
        ("コンプレツクス", {"コンプレックス"}, "コンプレックス"),
        # コーパスが知らない形は直さない（固有名詞・造語を勝手に変えない）
        ("フエニックス", set(), None),
        # 語頭の小書きは無い
        ("エリア", {"ェリア"}, None),
    ],
)
def test_still_misread(after, present, expected):
    assert audit_rules.still_misread(after, present) == expected


# ------------------------------------------------------------
# 出力（この差分のユーザーに見える部分）
# ------------------------------------------------------------


def _corpus(tmp_path, text):
    path = tmp_path / "book.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_the_report_marks_a_half_fixed_rule(tmp_path, capsys, monkeypatch):
    """★ と「あるべき形」と件数を出す。"""
    rules = tmp_path / "r.json"
    rules.write_text(
        json.dumps({"literal": {"カツト": "カット"}, "regex": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    path = _corpus(tmp_path, "キツトカツト。キツトカツト。キツトカツト。キットカット。")
    assert audit_rules.main([path, "--rules", str(rules)]) == 0
    out = capsys.readouterr().out
    assert "★" in out
    assert "キットカット" in out
    assert "規則を足すこと" in out


def test_the_report_marks_an_unsettled_rule(tmp_path, capsys):
    """収束していない規則を、要確認より先に出す。"""
    rules = tmp_path / "r.json"
    rules.write_text(
        json.dumps(
            {"literal": {"ウエッジウッド": "ウェッジウッド", "エツジ": "エッジ"}, "regex": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    path = _corpus(tmp_path, "ウエツジウッド。ウエツジウッド。ウエツジウッド。")
    assert audit_rules.main([path, "--rules", str(rules)]) == 0
    out = capsys.readouterr().out
    assert "収束していない規則" in out
    assert out.index("収束していない規則") < out.index("要確認")


def test_a_clean_dictionary_reports_neither(tmp_path, capsys):
    """直りきっていて、置換後の形もコーパスに在るなら、何も言わない。"""
    rules = tmp_path / "r.json"
    rules.write_text(
        json.dumps({"literal": {"カツト": "カット"}, "regex": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    path = _corpus(tmp_path, "カツト。カツト。カツト。カット。")
    assert audit_rules.main([path, "--rules", str(rules)]) == 0
    out = capsys.readouterr().out
    assert "★" not in out
    assert "収束していない規則" not in out


def test_a_suspicious_but_fully_fixed_rule_is_not_marked(tmp_path, capsys):
    """置換後の形がコーパスに無いだけで、誤読は残っていない規則に ★ を付けない。

    要確認の大半は固有名詞・複合語で、規則としては正しい。まとめの件数を
    「要確認の件数」と取り違えると、直すべき規則が何件あるのか分からなくなる。
    """
    rules = tmp_path / "r.json"
    rules.write_text(
        json.dumps({"literal": {"マデイソン": "マディソン"}, "regex": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    path = _corpus(tmp_path, "マデイソン。マデイソン。マデイソン。")
    assert audit_rules.main([path, "--rules", str(rules)]) == 0
    out = capsys.readouterr().out
    assert "要確認 1 件" in out
    assert "★" not in out
    assert "規則を足すこと" not in out
