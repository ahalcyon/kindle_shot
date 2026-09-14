"""検索可能 PDF から OCR テキスト層を剥がす (#89)。

漫画を誤って searchable_pdf で作ったとき、**撮り直さずに** image_pdf 相当へ
戻せることを確かめる。見るのは 3 つ。

- 剥がした結果が、はじめから image_pdf で作ったものと**描画で一致する**
- 文字が取れなくなる
- 検証に落ちたら**元のファイルを書き換えない**

日本語フォントには依存させない。本文は ASCII にし、reportlab 内蔵の
フォールバック (HeiseiMin-W3) で足りるようにしてある (#13)。
"""

import hashlib
import os

import pypdfium2 as pdfium
import pytest
from PIL import Image
from pypdf import PdfReader

from core import text_layer
from core.pdf_builder import images_to_pdf, images_to_searchable_pdf

PAGES = 3
SIZE = (240, 320)


def _patterned(path, seed):
    """ページごとに違う絵。単色にすると描画の突き合わせが素通りする。"""
    image = Image.new("RGB", SIZE, (250, 250, 250))
    for i in range(0, SIZE[0], 30):
        for j in range(0, SIZE[1], 30):
            if (i + j + seed * 30) % 60 == 0:
                image.paste((20, 20, 20), (i, j, i + 15, j + 15))
    image.save(path)


@pytest.fixture
def book(tmp_path):
    """画像フォルダと、そこから作った image_pdf / searchable_pdf を返す。"""
    images = tmp_path / "images"
    images.mkdir()
    names = []
    for i in range(1, PAGES + 1):
        name = f"{i:03d}.png"
        _patterned(images / name, i)
        names.append(name)

    ok, message = images_to_pdf(str(images), str(tmp_path), "plain.pdf")
    assert ok, message

    results = [(name, f"page {i} sample text") for i, name in enumerate(names, 1)]
    ok, message = images_to_searchable_pdf(str(images), results, str(tmp_path / "ocr.pdf"))
    assert ok, message

    return tmp_path / "plain.pdf", tmp_path / "ocr.pdf"


def _render(path, scale=1.0):
    doc = pdfium.PdfDocument(str(path))
    try:
        return [
            hashlib.sha256(doc[i].render(scale=scale).to_pil().tobytes()).hexdigest()
            for i in range(len(doc))
        ]
    finally:
        doc.close()


def _all_text(path):
    return "".join((p.extract_text() or "") for p in PdfReader(str(path)).pages).strip()


# ------------------------------------------------------------
# 剥がした結果が image_pdf と同じになる
# ------------------------------------------------------------


def test_the_fixture_actually_differs(book):
    """前提の確認。searchable のほうに文字が載っていなければ、以降が空振りする。"""
    plain, ocr = book
    assert _all_text(ocr) != ""
    assert _all_text(plain) == ""


def test_stripped_matches_image_pdf_rendering(book):
    """剥がした PDF は、はじめから image_pdf で作ったものと描画が一致する。"""
    plain, ocr = book
    before = _render(plain)
    assert text_layer.strip_file(str(ocr))["ok"]
    assert _render(ocr) == before


def test_stripping_removes_all_text(book):
    _, ocr = book
    result = text_layer.strip_file(str(ocr))
    assert result["ok"]
    # ページごとの空の BT (上の idempotent のテストを見よ) より明らかに多いこと
    assert result["removed"] > PAGES
    assert _all_text(ocr) == ""


def test_page_count_is_kept(book):
    _, ocr = book
    assert text_layer.strip_file(str(ocr))["ok"]
    assert len(PdfReader(str(ocr)).pages) == PAGES


def test_font_resource_is_dropped(book):
    """文字が無いのにサブセットフォントだけ残らないこと。"""
    _, ocr = book
    assert text_layer.strip_file(str(ocr))["ok"]
    for page in PdfReader(str(ocr)).pages:
        resources = page.get("/Resources") or {}
        assert "/Font" not in resources


def test_stripping_is_idempotent(book):
    """既に image_pdf のものに当てても壊さない。

    **image_pdf にも BT ... ET はある。** reportlab が 1 ページにつき
    ``BT /F1 12 Tf 14.4 TL ET`` を必ず書く（フォントを選ぶだけで何も描かない）。
    画像は ``Do`` でその外に描かれるので消しても影響しない。したがって
    ``removed`` が 0 でないことは「文字を消した」ことを意味しない。
    見るのは描画結果と抽出文字数のほう。
    """
    plain, _ = book
    before = _render(plain)
    result = text_layer.strip_file(str(plain))
    assert result["ok"], result.get("error")
    assert _render(plain) == before
    assert _all_text(plain) == ""


# ------------------------------------------------------------
# 検証に落ちたら元のファイルを残す
# ------------------------------------------------------------


def test_the_original_survives_a_failed_verification(book, monkeypatch):
    """描画が変わったと判定されたら、蔵書のファイルには触らない。"""
    _, ocr = book
    original = ocr.read_bytes()

    calls = []

    def fake_render(path, indexes, scale=0.5):
        calls.append(path)
        # 2 回目（剥がしたあと）だけ違う値を返し、描画が変わったことにする
        return ["same"] if len(calls) == 1 else ["different"]

    monkeypatch.setattr(text_layer, "render_digests", fake_render)
    result = text_layer.strip_file(str(ocr))

    assert not result["ok"]
    assert "描画結果" in result["error"]
    assert ocr.read_bytes() == original


def test_the_original_survives_leftover_text(book, monkeypatch):
    _, ocr = book
    original = ocr.read_bytes()
    monkeypatch.setattr(text_layer, "extracted_chars", lambda path, indexes: 42)

    result = text_layer.strip_file(str(ocr))

    assert not result["ok"]
    assert "テキストが残っています" in result["error"]
    assert ocr.read_bytes() == original


def test_no_temporary_file_is_left_behind(book, monkeypatch):
    """失敗しても作業ファイルを蔵書フォルダに残さない。"""
    _, ocr = book
    monkeypatch.setattr(text_layer, "extracted_chars", lambda path, indexes: 42)
    text_layer.strip_file(str(ocr))
    assert sorted(os.listdir(ocr.parent)) == ["images", "ocr.pdf", "plain.pdf"]


def test_a_broken_file_is_reported_not_raised(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    result = text_layer.strip_file(str(broken))
    assert not result["ok"]
    assert result["error"]


# ------------------------------------------------------------
# 検証に使うページの選び方
# ------------------------------------------------------------


def test_sample_covers_both_ends():
    """先頭と末尾は必ず見る。末尾を落とすと、後ろだけ壊れた本を通してしまう。"""
    picks = text_layer._sample_indexes(100)
    assert picks[0] == 0
    assert picks[-1] == 99
    assert len(picks) == text_layer.VERIFY_PAGES


def test_sample_of_a_short_book_is_every_page():
    assert text_layer._sample_indexes(2) == [0, 1]


def test_sample_of_an_empty_book_is_empty():
    assert text_layer._sample_indexes(0) == []


# ------------------------------------------------------------
# 蔵書との突き合わせ (#95)
# ------------------------------------------------------------


def test_strip_finds_a_book_whose_name_was_truncated(tmp_path):
    """**剥がす対象も正引きで探す (#95)。**

    蔵書の名前は book_path_name を通っていて、長いタイトルは `_<8桁hash>` で
    切り詰められる。ファイル名から逆引きすると切り詰められた本が黙って外れ、
    漫画に誤った OCR テキスト層が残ったままになる。
    """
    import importlib.util
    import json

    from core.safe_names import book_path_name

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "strip_text_layer.py",
    )
    spec = importlib.util.spec_from_file_location("strip_text_layer", script)
    assert spec is not None and spec.loader is not None
    strip_text_layer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strip_text_layer)

    images = tmp_path / "images"
    images.mkdir()
    names = []
    for i in range(1, PAGES + 1):
        name = f"{i:03d}.png"
        _patterned(images / name, i)
        names.append(name)

    library = tmp_path / "library"
    library.mkdir()
    long_title = "あ" * 300
    stored = book_path_name(long_title, str(library))
    assert stored != long_title, "この長さでは切り詰めが起きない。前提が崩れている"

    results = [(name, f"page {i} sample text") for i, name in enumerate(names, 1)]
    ok, message = images_to_searchable_pdf(str(images), results, str(library / f"{stored}.pdf"))
    assert ok, message
    assert _all_text(library / f"{stored}.pdf") != ""

    books = tmp_path / "books.json"
    books.write_text(
        json.dumps([{"asin": "A", "title": long_title, "format": "image_pdf"}], ensure_ascii=False),
        encoding="utf-8",
    )
    assert strip_text_layer.main(["--folder", str(library), "--books", str(books), "--json"]) == 0
    assert _all_text(library / f"{stored}.pdf") == "", "切り詰められた本が剥がし漏れた"


def test_strip_fails_loudly_when_nothing_matches(tmp_path, capsys):
    """**1 冊も一致しないまま成功で終わらない (#95)。**

    突き合わせが外れる形（--folder が撮影時の --out と違う、books.json の
    タイトルが別物）は黙って「0 冊」になるだけで、剥がし漏れに気づけない。
    """
    import importlib.util
    import json

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "strip_text_layer.py",
    )
    spec = importlib.util.spec_from_file_location("strip_text_layer_2", script)
    assert spec is not None and spec.loader is not None
    strip_text_layer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strip_text_layer)

    library = tmp_path / "library"
    library.mkdir()
    images = tmp_path / "images"
    images.mkdir()
    _patterned(images / "001.png", 1)
    ok, message = images_to_pdf(str(images), str(library), "よその本.pdf")
    assert ok, message

    books = tmp_path / "books.json"
    books.write_text(
        json.dumps([{"asin": "A", "title": "持っていない本", "format": "image_pdf"}]),
        encoding="utf-8",
    )
    code = strip_text_layer.main(["--folder", str(library), "--books", str(books)])
    assert code != 0, "1 冊も一致しなかったのに成功で終わった"
    assert "1 冊も一致しませんでした" in capsys.readouterr().err
