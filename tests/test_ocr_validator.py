"""core/ocr_validator.py のテスト

数百冊を無人で回す以上、出力が使い物になっているかは機械で判定するしかない。

本文の切れ（#28）はここでは判定しない。端に接するかを見る案を実データで
測ったところ誤検知だらけだったため、画像側の pipeline.clipped_sides が
決定的に判定する（tests/test_pipeline.py 参照）。
"""

from core import ocr_validator
from core.ocr_layout import Line, PageLayout


def line(text, left, top, right, bottom, conf=0.95, category="本文"):
    return Line(
        text=text,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        vertical=True,
        confidence=conf,
        category=category,
    )


def page(*lines, filename="002.png", width=1280, height=1050):
    return PageLayout(filename=filename, width=width, height=height, lines=list(lines))


def healthy_page(**kw):
    """余白の内側に収まっている無傷のページ。"""
    return page(
        line("本文のいちれつめ", 1071, 40, 1091, 900),
        line("本文のにれつめ", 1038, 40, 1058, 900),
        **kw,
    )


def test_healthy_page_has_no_problem():
    report = ocr_validator.analyze([healthy_page()])

    assert report["ok"] is True
    assert report["problems"] == []


def test_edge_touching_lines_are_not_treated_as_clipping():
    """端に接すること自体は正常なページでも普通に起きる。

    実測: 切れているページ（旧方式）の端接触行の信頼度中央値 0.943、
    切れていないページ（要素撮影）が 0.947 で差が無く、要素撮影でも 6 ページ中
    2 ページが端に接していた。ここで問題にすると誤検知だらけになる。
    """
    report = ocr_validator.analyze([page(line("画像の端まで来ている本文", 1071, 0, 1091, 1050))])

    assert report["ok"] is True
    assert report["problems"] == []


# ------------------------------------------------------------
# ビューアの UI の写り込み
# ------------------------------------------------------------


def test_flags_reader_chrome():
    """柱・ノンブルが読めたら、ページ画像に UI が写り込んでいる。"""
    report = ocr_validator.analyze(
        [page(line("みんなのフィードバック大全", 400, 40, 900, 60, category="柱"))]
    )

    assert report["ok"] is False
    assert report["chrome_pages"] == ["002.png"]
    assert "UI" in report["problems"][0]


# ------------------------------------------------------------
# 警告（読めてはいるが疑わしい）
# ------------------------------------------------------------


def test_warns_about_pages_without_any_body_text():
    report = ocr_validator.analyze([page(filename="005.png")])

    assert report["empty_pages"] == ["005.png"]
    assert any("1 行も読めなかった" in w for w in report["warnings"])
    assert report["ok"] is True  # 白紙ページはありうるので problem にはしない


def test_warns_when_many_lines_have_low_confidence():
    lines = [line(f"あやしい{i}", 100 + i * 30, 40, 120 + i * 30, 900, conf=0.4) for i in range(5)]
    report = ocr_validator.analyze([page(*lines)])

    assert report["low_confidence_lines"] == 5
    assert report["min_confidence"] == 0.4
    assert any("信頼度" in w for w in report["warnings"])


def test_warns_about_pages_without_coordinates():
    report = ocr_validator.analyze([PageLayout(filename="002.png", fallback_text="本文")])

    assert report["unpositioned_pages"] == ["002.png"]
    assert any("文字の位置が取れなかった" in w for w in report["warnings"])


def test_warns_about_known_misreads():
    """実測で頻出した誤認識（小書きの ッ ィ を大書きに読む）。"""
    report = ocr_validator.analyze([page(line("フィードバツクの話", 1071, 40, 1091, 900))])

    assert report["misreads"] == 1
    assert any("誤認識" in w for w in report["warnings"])


def test_warns_about_unbalanced_brackets():
    report = ocr_validator.analyze([page(line("「開いたまま", 1071, 40, 1091, 900))])

    assert report["unbalanced_brackets"] == [("「", "」", 1)]
    assert any("括弧" in w for w in report["warnings"])


# ------------------------------------------------------------
# 集計
# ------------------------------------------------------------


def test_counts_pages_and_characters():
    report = ocr_validator.analyze([healthy_page(), healthy_page(filename="003.png")])

    assert report["pages"] == 2
    assert report["body_lines"] == 4
    assert report["chars"] == len("本文のいちれつめ本文のにれつめ") * 2


def test_survives_an_empty_book():
    report = ocr_validator.analyze([])
    assert report["pages"] == 0
    assert report["ok"] is True
    assert report["min_confidence"] is None
