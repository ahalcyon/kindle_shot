"""本ごとに image_pdf / searchable_pdf を決める (#89)。

**間違え方は対称ではない。** 文章の本を image_pdf にするとテキスト層の無い本が
出来て撮り直すしかないが、漫画を searchable_pdf にしても OCR の時間を捨てるだけで
あとから剥がせる。だから**確信があるときだけ image_pdf に倒す**。
根拠の優先順位と、迷ったときに searchable へ倒れることを見る。
"""

import importlib.util
import json
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "classify_formats.py",
)
_spec = importlib.util.spec_from_file_location("classify_formats", _SCRIPT)
assert _spec is not None and _spec.loader is not None
classify_formats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classify_formats)

from core.book_format import (
    IMAGE,
    SEARCHABLE,
    decide,
    genre_labels,
    library_labels,
    measured_labels,
    series_key,
    series_labels,
)
from core.safe_names import book_path_name


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


def test_a_series_seen_only_once_is_not_evidence():
    """**1 冊だけの系列は根拠にしない。**

    `len(c) == 1` は「判定が 1 種類」であって「複数冊が一致した」ではない。
    series_key は数字・括弧内・巻/話/第/上/中/下 を落とすので 1 文字違いで
    衝突する（「下町ロケット」と「町ロケット」が同じキーになる）。
    割れの検出は実測済みのタイトル同士でしか効かないので、未実測の本が
    衝突して入ってくる経路は素通りする。しかもこれは撮り直しの要る高い側の間違い。
    """
    assert series_labels({"A 1": IMAGE})[series_key("A 1")] is None


def test_two_agreeing_books_are_evidence():
    assert series_labels({"A 1": IMAGE, "A 2": IMAGE})[series_key("A 1")] == IMAGE


def test_the_series_key_really_does_collide():
    """衝突が机上の心配でないことを示す。ここが崩れたら上の 2 件の根拠も消える。"""
    assert series_key("下町ロケット") == series_key("町ロケット")
    assert series_key("空の中") == series_key("空の上")


def test_a_single_book_series_cannot_reach_decide():
    """1 冊だけの系列は decide まで届かない（既定の searchable に落ちる）。"""
    measured = {"下町ロケット": IMAGE}
    series = series_labels(measured)
    assert decide("町ロケット", measured=measured, series=series, genre={}, library={}) == (
        SEARCHABLE,
        "default",
    )


# ------------------------------------------------------------
# 旧蔵書の読み取り
# ------------------------------------------------------------


def _pdf(path, pages, text=None):
    """ページ数を指定した PDF を作る。text を渡すとテキストを載せる。"""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.setPageSize((200, 200))
        if text:
            c.drawString(20, 100, f"{text} {i}")
        c.showPage()
    c.save()


def test_a_zero_page_pdf_is_not_called_image_pdf(tmp_path):
    """**「文字が無い」と「読めなかった」を混ぜない。**

    中断したバッチの書きかけが蔵書フォルダに残ることがある。0 ページの PDF を
    「テキスト層なし」の根拠にすると、その本が撮り直しの要る image_pdf 側
    （高いほうの間違い）へ倒れる。
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    with (tmp_path / "空.pdf").open("wb") as f:
        writer.write(f)
    assert library_labels(str(tmp_path), ["空"]) == {}


def test_a_pdf_without_text_is_image_pdf(tmp_path):
    _pdf(tmp_path / "絵だけ.pdf", 3)
    assert library_labels(str(tmp_path), ["絵だけ"]) == {"絵だけ": IMAGE}


def test_a_pdf_with_text_is_searchable(tmp_path):
    _pdf(tmp_path / "文章.pdf", 3, text="hello")
    assert library_labels(str(tmp_path), ["文章"]) == {"文章": SEARCHABLE}


def test_an_unreadable_file_is_skipped_not_guessed(tmp_path):
    (tmp_path / "壊れ.pdf").write_bytes(b"not a pdf")
    assert library_labels(str(tmp_path), ["壊れ"]) == {}


# ------------------------------------------------------------
# books.json の書き出し
# ------------------------------------------------------------


def test_per_book_settings_survive(tmp_path):
    """**本ごとの設定を落とさない。**

    make_books.py は page_turn / split_words / max_pages を本ごとに書く
    （selection.example.json の「横書きは page_turn: right」）。ここで落とすと、
    横書きの本が既定のページ送りで撮られる。
    """
    books = tmp_path / "books.json"
    books.write_text(
        json.dumps(
            [{"asin": "A", "title": "本", "page_turn": "right", "split_words": 450000}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    assert classify_formats.main(["--books", str(books), "--out", str(out)]) == 0

    written = json.loads(out.read_text(encoding="utf-8"))[0]
    assert written["page_turn"] == "right"
    assert written["split_words"] == 450000
    assert written["format"] == SEARCHABLE


def test_the_written_file_is_accepted_by_the_batch_loader(tmp_path):
    """書き出したものが cli.py batch にそのまま渡せること。

    format を足したせいで未知キー扱いになったり、拾い落としで asin が
    消えたりしていないかを、本物の検証器で見る。
    """
    from core.pipeline import load_batch_file

    books = tmp_path / "books.json"
    books.write_text(
        json.dumps([{"asin": "A", "title": "本", "page_turn": "right"}], ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    classify_formats.main(["--books", str(books), "--out", str(out)])

    loaded, code = load_batch_file(str(out))
    assert code is None, "書き出した books.json が batch に渡せない"
    assert loaded[0]["fmt"] == SEARCHABLE
    assert loaded[0]["page_turn"] == "right"


# ------------------------------------------------------------
# 名前を切り詰められた本 (#95)
# ------------------------------------------------------------


def test_a_truncated_name_is_still_found(tmp_path):
    """**ファイル名からタイトルを逆引きしない (#95)。**

    蔵書の名前は book_path_name を通っていて、長いタイトルは `_<8桁hash>` で
    切り詰められる。ハッシュは元のタイトルから作るので逆引きできない。
    素朴に `ファイル名[:-4] == title` で比べると、切り詰められた本が黙って外れる:

    - strip_text_layer なら剥がし漏れ（誤った OCR テキスト層が残る）
    - --only-missing なら完成済みの本を 1 冊 10 分かけて撮り直す
    """
    long_title = "あ" * 300
    stored = book_path_name(long_title, str(tmp_path))
    assert stored != long_title, "この長さでは切り詰めが起きない。前提が崩れている"

    _pdf(tmp_path / f"{stored}.pdf", 3)
    assert library_labels(str(tmp_path), [long_title]) == {long_title: IMAGE}


def test_the_pdf_path_goes_through_the_sanitiser(tmp_path):
    """Windows のファイル名に使えない文字を含む本も正引きできる。"""
    title = "本: その 1 / 続き"
    stored = book_path_name(title, str(tmp_path))
    _pdf(tmp_path / f"{stored}.pdf", 2, text="hello")
    assert library_labels(str(tmp_path), [title]) == {title: SEARCHABLE}


def test_a_title_with_no_file_is_not_labelled(tmp_path):
    assert library_labels(str(tmp_path), ["持っていない本"]) == {}


def test_only_missing_skips_a_truncated_name(tmp_path):
    """完成済みの判定も正引きで行う。切り詰められた本を撮り直さない。"""
    out = tmp_path / "library"
    out.mkdir()
    long_title = "い" * 300
    _pdf(out / f"{book_path_name(long_title, str(out))}.pdf", 2)

    books = tmp_path / "books.json"
    books.write_text(
        json.dumps([{"asin": "A", "title": long_title}], ensure_ascii=False), encoding="utf-8"
    )
    result = tmp_path / "typed.json"
    classify_formats.main(["--books", str(books), "--out", str(result), "--only-missing", str(out)])
    assert json.loads(result.read_text(encoding="utf-8")) == [], "完成済みなのに撮り直しに回った"
