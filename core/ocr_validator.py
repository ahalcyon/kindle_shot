"""読み取り結果の機械検証（純解析・副作用なし）

数百冊を無人で処理する以上、1 冊ずつ目視するのは不可能なので、出力が
使い物になっているかを機械的に判定する。core/validator.py がページ画像を
見る（白紙・重複・サイズ違い）のに対し、こちらは OCR の結果を見る。

検査は「確実に落とせるもの」から順に並べてある:

1. **種別** — ビューアの UI（柱・ノンブル）が読めてしまったら、ページ画像に
   UI が写り込んでいる。本文行が 0 のページも拾う
2. **信頼度** — NDLOCR-Lite が行ごとに返す confidence
3. **日本語としての妥当性** — 括弧の対応、既知の誤認識パターン

**本文の切れ（#28）はここでは判定しない。** 行の bbox が画像の端に接して
いるかを見る案を実装して実データで測ったが、切れているページ（旧方式）の
端接触行の信頼度中央値が 0.943、切れていないページ（要素撮影）が 0.947 と
差が無く、端に接すること自体は正常なページでも普通に起きる（実測で 6 ページ中
2 ページ）。誤検知が多すぎて使えない。切れの判定は画像側で決定的にできる
（``pipeline.clipped_sides``: 適用マージンが内容の位置を超えたら削っている）
ので、そちらに置いた。

「文章として成立しているか」の最終判断は人間や LLM に委ねるしかないが、
ここで「要確認」に落ちた本だけを見ればよくなる。
"""

import re

from core.ocr_layout import CHROME_CATEGORIES

# 行の bbox が画像の端からこの距離以内なら「接している」とみなす
EDGE_TOLERANCE = 2
# これ未満の信頼度の行を「低信頼」として数える
LOW_CONFIDENCE = 0.8

# 実測で頻出した誤認識。小書き（ッ ャ ュ ョ ィ ...）を大書きに読み違える
KNOWN_MISREADS = (
    (re.compile(r"[ァ-ヴー]*バツク"), "小書きの「ッ」を「ツ」と誤読している疑い"),
    (re.compile(r"フイード"), "小書きの「ィ」を「イ」と誤読している疑い"),
)

# 対応を数える括弧
BRACKET_PAIRS = (("「", "」"), ("『", "』"), ("（", "）"), ("(", ")"))


def analyze_page(layout):
    """1 ページ分の PageLayout を検査して dict を返す。"""
    body = [line for line in layout.lines if line.is_body]
    chrome = [line for line in layout.lines if line.category in CHROME_CATEGORIES]
    confidences = [line.confidence for line in layout.lines]
    return {
        "file": layout.filename,
        "positioned": layout.positioned,
        "lines": len(layout.lines),
        "body_lines": len(body),
        "chars": sum(len(line.text) for line in body),
        "chrome_lines": [line.text[:20] for line in chrome],
        "low_confidence": sum(1 for c in confidences if c < LOW_CONFIDENCE),
        "min_confidence": min(confidences) if confidences else None,
    }


def find_misreads(text):
    """既知の誤認識パターンを探して [(抜粋, 説明), ...] を返す。"""
    found = []
    for pattern, why in KNOWN_MISREADS:
        for match in pattern.finditer(text):
            found.append((match.group(0), why))
    return found


def unbalanced_brackets(text):
    """開閉が合っていない括弧を [(開き, 閉じ, 差), ...] で返す。"""
    problems = []
    for open_ch, close_ch in BRACKET_PAIRS:
        diff = text.count(open_ch) - text.count(close_ch)
        if diff:
            problems.append((open_ch, close_ch, diff))
    return problems


def analyze(layouts):
    """本 1 冊分の PageLayout 列を検査してレポート dict を返す。

    problems は「本文が失われている可能性」だけを入れる。warnings は
    「読めてはいるが精度が疑わしい」もの。呼び出し側は problems が空でない
    本を要確認として扱えばよい。
    """
    pages = [analyze_page(layout) for layout in layouts]
    text = "\n".join(layout.text for layout in layouts)

    chrome_pages = [p for p in pages if p["chrome_lines"]]
    empty_pages = [p for p in pages if p["body_lines"] == 0]
    unpositioned = [p for p in pages if not p["positioned"]]
    confidences = [p["min_confidence"] for p in pages if p["min_confidence"] is not None]
    low_total = sum(p["low_confidence"] for p in pages)
    line_total = sum(p["lines"] for p in pages)

    problems = []
    if chrome_pages:
        problems.append(
            f"ビューアの UI（柱・ノンブル）が写り込んでいるページが {len(chrome_pages)} あります"
        )

    warnings = []
    if empty_pages:
        warnings.append(f"本文が 1 行も読めなかったページが {len(empty_pages)} あります")
    if unpositioned:
        warnings.append(
            f"文字の位置が取れなかったページが {len(unpositioned)} あります"
            "（検索はできますが範囲選択コピーが位置と対応しません）"
        )
    if line_total and low_total / line_total > 0.2:
        warnings.append(f"信頼度 {LOW_CONFIDENCE} 未満の行が {low_total}/{line_total} 行あります")
    misreads = find_misreads(text)
    if misreads:
        warnings.append(f"既知の誤認識パターンが {len(misreads)} 件あります: {misreads[0][1]}")
    brackets = unbalanced_brackets(text)
    if brackets:
        detail = ", ".join(f"{o}{c} が {d:+d}" for o, c, d in brackets)
        warnings.append(f"括弧の対応が合っていません（{detail}）")

    return {
        "pages": len(pages),
        "chars": sum(p["chars"] for p in pages),
        "body_lines": sum(p["body_lines"] for p in pages),
        "chrome_pages": [p["file"] for p in chrome_pages],
        "empty_pages": [p["file"] for p in empty_pages],
        "unpositioned_pages": [p["file"] for p in unpositioned],
        "low_confidence_lines": low_total,
        "min_confidence": min(confidences) if confidences else None,
        "misreads": len(misreads),
        "unbalanced_brackets": brackets,
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
    }
