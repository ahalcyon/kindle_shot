"""本ごとに image_pdf / searchable_pdf を決める (#89)。

**間違え方は対称ではない。** 文章の本を image_pdf にするとテキスト層の無い本が
出来て撮り直すしかないが、漫画を searchable_pdf にしても OCR の時間を捨てるだけで
あとから剥がせる。だから**確信があるときだけ image_pdf に倒す**。
根拠の優先順位と、迷ったときに searchable へ倒れることを見る。
"""

import json

import pytest

from core.book_format import (
    IMAGE,
    SEARCHABLE,
    decide,
    genre_labels,
    measured_labels,
    series_key,
    series_labels,
)


def _log(tmp_path, entries):
    """book_start と ocr_validation が交互に並ぶバッチログを書く。"""
    path = tmp_path / "batch.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for title, pages, chars in entries:
            f.write(json.dumps({"event": "book_start", "title": title}) + "\n")
            f.write(json.dumps({"event": "page", "page": 1}) + "\n")
            f.write(json.dumps({"event": "ocr_validation", "pages": pages, "chars": chars}) + "\n")
    return str(path)


# ------------------------------------------------------------
# 実測（文字数/ページ）
# ------------------------------------------------------------


def test_a_sparse_book_is_image_pdf(tmp_path):
    labels, values = measured_labels([_log(tmp_path, [("漫画", 100, 2000)])], 150)
    assert labels["漫画"] == IMAGE
    assert values["漫画"] == pytest.approx(20.0)


def test_a_dense_book_is_searchable(tmp_path):
    labels, _ = measured_labels([_log(tmp_path, [("実用書", 100, 50000)])], 150)
    assert labels["実用書"] == SEARCHABLE


def test_the_threshold_is_exclusive(tmp_path):
    """ちょうど閾値なら searchable。迷う側は安いほうへ倒す。"""
    labels, _ = measured_labels([_log(tmp_path, [("境界", 10, 1500)])], 150)
    assert labels["境界"] == SEARCHABLE


def test_a_book_with_no_pages_is_not_labelled(tmp_path):
    """0 ページで割らない。判定材料にもしない。"""
    labels, _ = measured_labels([_log(tmp_path, [("空", 0, 0)])], 150)
    assert labels == {}


def test_several_logs_are_merged(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _log(tmp_path / "a", [("A", 100, 2000)])
    b = _log(tmp_path / "b", [("B", 100, 50000)])
    labels, _ = measured_labels([a, b], 150)
    assert labels == {"A": IMAGE, "B": SEARCHABLE}


def test_an_unfinished_book_does_not_steal_the_next_label(tmp_path):
    """撮影に失敗して ocr_validation が来なかった本が、次の本の判定を奪わない。"""
    path = tmp_path / "batch.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "book_start", "title": "落ちた本"}) + "\n")
        f.write(json.dumps({"event": "book_start", "title": "次の本"}) + "\n")
        f.write(json.dumps({"event": "ocr_validation", "pages": 100, "chars": 50000}) + "\n")
    labels, _ = measured_labels([str(path)], 150)
    assert labels == {"次の本": SEARCHABLE}


# ------------------------------------------------------------
# シリーズ名
# ------------------------------------------------------------


def test_volume_numbers_and_labels_are_dropped():
    assert series_key("てーきゅう　3 (アース・スターコミックス)") == series_key(
        "てーきゅう　12 (アース・スターコミックス)"
    )


def test_full_width_volume_numbers_are_dropped():
    assert series_key("ヒストリエ（１）") == series_key("ヒストリエ（３）")


def test_different_series_do_not_collide():
    assert series_key("未来日記(5)") != series_key("望郷太郎（１）")


def test_a_unanimous_series_decides():
    labels = {"A 1": IMAGE, "A 2": IMAGE}
    assert series_labels(labels)[series_key("A 1")] == IMAGE


def test_a_split_series_decides_nothing():
    """同じ系列で判定が割れていたら、その系列は根拠にしない。"""
    labels = {"A 1": IMAGE, "A 2": SEARCHABLE}
    assert series_labels(labels)[series_key("A 1")] is None


# ------------------------------------------------------------
# 根拠の優先順位
# ------------------------------------------------------------


def _decide(title, *, measured=None, series=None, genre=None, library=None):
    return decide(
        title,
        measured=measured or {},
        series=series or {},
        genre=genre or {},
        library=library or {},
    )


def test_measurement_wins_over_everything():
    fmt, why = _decide(
        "本",
        measured={"本": SEARCHABLE},
        series={series_key("本"): IMAGE},
        genre={"本": "コミック"},
        library={"本": IMAGE},
    )
    assert (fmt, why) == (SEARCHABLE, "measured")


def test_series_wins_over_genre():
    fmt, why = _decide("本 2", series={series_key("本 2"): SEARCHABLE}, genre={"本 2": "コミック"})
    assert (fmt, why) == (SEARCHABLE, "series")


def test_the_comic_genre_decides_image():
    fmt, why = _decide("漫画", genre={"漫画": "コミック"})
    assert (fmt, why) == (IMAGE, "genre-comic")


def test_another_genre_does_not_decide():
    """コミック以外のジャンルは当てにならない（実測で 78 冊中 34 冊が漫画側だった）。"""
    fmt, why = _decide("本", genre={"本": "その他（実用書ほか）"})
    assert (fmt, why) == (SEARCHABLE, "default")


def test_the_old_library_decides_image():
    fmt, why = _decide("本", library={"本": IMAGE})
    assert (fmt, why) == (IMAGE, "library-image")


def test_a_book_with_no_evidence_is_searchable():
    """迷ったら searchable。読み違えても剥がせばいいので安い。"""
    assert _decide("知らない本") == (SEARCHABLE, "default")


def test_the_old_library_never_forces_image_by_omission():
    """旧蔵書が searchable だったからといって image には倒さない。"""
    fmt, _ = _decide("本", library={"本": SEARCHABLE})
    assert fmt == SEARCHABLE


# ------------------------------------------------------------
# ジャンル分けの読み込み
# ------------------------------------------------------------


def test_genre_labels_are_flattened(tmp_path):
    path = tmp_path / "buckets.json"
    path.write_text(
        json.dumps({"コミック": [{"asin": "A", "title": "漫画"}], "新書・文庫": []}),
        encoding="utf-8",
    )
    assert genre_labels(str(path)) == {"漫画": "コミック"}
