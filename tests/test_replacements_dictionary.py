"""リポジトリ同梱の replacements.json が満たすべき性質のテスト

辞書は機械で洗い出した候補を人が見て足していく運用（README「誤読を洗い出す」）
なので、足すたびに壊れやすい性質をここで固定する。
"""

import json

from core.text_replacements import Replacer, default_path


def load():
    with open(default_path(), encoding="utf-8") as f:
        return json.load(f)


def replacer(doc):
    return Replacer(doc.get("literal"), doc.get("regex"))


def test_no_rule_leaves_a_form_that_is_still_wrong():
    """直した結果に、まだ直せる誤読が残っていないこと。

    literal は同じルールを 2 度は適用しないが、**短いルールは長いルールの
    あとに走る**ので、長いルールの右辺に誤読が残っていると短いルールが
    拾って直す形になっている。どのルールでも直りきることを確かめる。

    実際に破れていた: #38 の「ボジティブフィードバツク -> ボジティブ
    フィードバック」は右辺の「ボ」が誤読のままで、半分しか直っていなかった。
    """
    doc = load()
    rep = replacer(doc)
    unfinished = {
        wrong: (right, rep.apply(right))
        for wrong, right in doc["literal"].items()
        if rep.apply(right) != right
    }
    assert not unfinished, f"直りきらないルール: {unfinished}"


def test_a_rule_never_changes_the_length_of_the_text():
    """literal は字の取り違えを直すためのもので、字数は変わらないはず。

    字数が変わるルールは、語の境目に当たったときの被害が大きい
    （トンカツプレート -> トンカップレート のように語を削る）。
    """
    doc = load()
    odd = {w: r for w, r in doc["literal"].items() if len(w) != len(r)}
    assert not odd, f"字数の変わるルール: {odd}"


def test_the_corrected_form_is_not_itself_a_rule_key():
    """ある規則の右辺が別の規則の左辺になっていないこと。

    なっていると「直したものをさらに別の形へ書き換える」ことになり、
    どちらが正しいのか辞書の中で矛盾する。
    """
    literal = load()["literal"]
    conflicting = {w: r for w, r in literal.items() if r in literal}
    assert not conflicting, f"右辺が別の規則の左辺になっている: {conflicting}"
