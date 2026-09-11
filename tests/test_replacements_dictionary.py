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


# 誤読形を部分文字列として含む「正しい語」。#67 のレビューで、追加した規則が
# これらを壊していることがコーパス (342 冊) で見つかった。辞書を辞書自身に
# 照らす上の 3 つのテストでは見つけられない種類なので、実例を置いておく。
# 末尾の数語は以前の回で見つかったもの。全部が今のコーパスに在るわけではない。
CORRECT_WORDS = [
    "アップロード",  # プロード -> ブロード が当たっていた
    "ヒューレット・パッカード",  # バツカー -> ハッカー
    "ルイス・バンバーガー",  # バンバーガー -> ハンバーガー
    "ホイラー・マンガー",  # ホイラー -> ボイラー
    "クラウス・フックス",  # フックス -> ブックス
    "スワンナプーム国際空港",  # プーム -> ブーム
    "データベーストランザクション",  # ベースト -> ペースト
    "ホリデイシーズン",  # デイシー -> デイジー
    "バーテイカル",  # バーテイ -> パーティ
    "ヘブライスト",  # ブライス -> プライス（規則ごと取り下げ、長いキーに置き換えた）
    "へブライスト",  # 同上。OCR が ヘ をひらがな へ に読んだ形
    "エプロンドレス",  # プロンド -> ブロンド
    "フンフン",  # フンフン -> ブンブン (擬音)
    "ハアハア",  # ハアハア -> ハァハァ (表記であって誤認識ではない)
    "ダービシュ・アンド・バングズ",  # バングズ -> バンクス
    "エドウイン・ボーチャード",  # ボーチ -> ポーチ
    "ボーター帽子",  # ボーター -> ポーター
    "ナツツバキ",  # ナツツ -> ナッツ
    "コーキーバークウィルス",  # キーバー -> キーパー
    "スクエアブリストル",  # アブリ -> アプリ
    "シンボルノル",  # ボルノ -> ポルノ
    "ボルシェビキ",  # ボルシェ -> ポルシェ
    "トンカツプレート",
    "ケースバイケース",
    "富士フイルム",
    "バイプレーヤー",
    "ホッグズ",
    "ダッチオーブン",
    "オマハ",
    "プドゥチェリー",
]


def test_a_correct_word_that_contains_a_misread_is_left_alone():
    """誤読形を部分文字列として含む正しい語を書き換えないこと。"""
    rep = replacer(load())
    broken = {w: rep.apply(w) for w in CORRECT_WORDS if rep.apply(w) != w}
    assert not broken, f"正しい語を壊す規則がある: {broken}"
