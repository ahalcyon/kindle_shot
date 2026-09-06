"""pdf_builder の日本語フォント埋め込みテスト

生成した PDF を pypdfium2（Edge/Chrome と同じ PDF エンジン）で読み直し、
日本語テキストが抽出できることと、TrueType フォントが埋め込まれていることを見る。
フォント候補が使えない環境では従来の HeiseiMin-W3 に落ちることも確認する。
"""

import glob
import os

import pytest
from PIL import Image

from core import pdf_builder

SAMPLE_TEXT = "吾輩は猫である\n名前はまだ無い\nsearchable PDF test"

# 日本語グリフを持つフォントの探索先。Windows は本番の候補をそのまま使い、
# それ以外の OS では一般的な配置を見る。
_CJK_PATTERNS = (
    "/usr/share/fonts/**/*CJK*.tt[cf]",
    "/usr/share/fonts/**/Noto*JP*.tt[cf]",
    "/usr/share/fonts/**/ipa*.tt[cf]",
    os.path.expanduser("~/.local/share/fonts/**/*CJK*.tt[cf]"),
    os.path.expanduser("~/.local/share/fonts/**/Noto*JP*.tt[cf]"),
    "/Library/Fonts/**/*Hiragino*.tt[cf]",
)


def _registrable(path, index):
    """reportlab が TTFont として登録できるフォントか。

    Noto Sans CJK のような CFF ベース (OTF) のフォントは TrueType outline を
    持たないため reportlab では扱えない。存在するだけでは判定できないので
    sfnt のバージョンタグを読んで確かめる。

    TTFont を実際に作らないのは、判定のためだけにフォントを読む必要が
    無いため。
    """
    try:
        with open(path, "rb") as f:
            tag = f.read(4)
            if tag == b"ttcf":
                f.seek(12 + 4 * index)
                offset = int.from_bytes(f.read(4), "big")
                f.seek(offset)
                tag = f.read(4)
    except OSError:
        return False
    # TrueType outline は 0x00010000 か "true"。"OTTO" は CFF なので扱えない。
    return tag in (b"\x00\x01\x00\x00", b"true")


def _find_japanese_font():
    """日本語グリフを持ち、reportlab で登録できる TrueType を探す。無ければ None。"""
    candidates = list(pdf_builder.JAPANESE_FONT_CANDIDATES)
    for pattern in _CJK_PATTERNS:
        candidates += [(path, 0) for path in sorted(glob.glob(pattern, recursive=True))]
    for path, index in candidates:
        if os.path.exists(path) and _registrable(path, index):
            return path, index
    return None


@pytest.fixture
def any_truetype(monkeypatch):
    """埋め込みの仕組みだけを見るテスト用に、確実に存在する TrueType を候補にする。

    reportlab が同梱している Vera.ttf を使う。日本語グリフは無いが、
    「TTF を登録して /FontFile2 が埋まる」ことの確認には十分で、
    Windows 標準フォントの有無に依存しなくなる。
    """
    import reportlab

    vera = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf")
    if not os.path.exists(vera):
        pytest.skip("reportlab の同梱フォントが見つかりません")
    monkeypatch.setattr(pdf_builder, "JAPANESE_FONT_CANDIDATES", [(vera, 0)])
    # 登録名を分ける。reportlab の pdfmetrics はプロセス全体で名前を保持するため、
    # 本番と同じ名前で日本語グリフの無い Vera を登録すると、後続のテストが
    # 同名で再登録しても最初の登録が残り、日本語が落ちる（Windows で実測）。
    monkeypatch.setattr(pdf_builder, "EMBEDDED_FONT_NAME", "KindleShotTestVera")
    return vera


@pytest.fixture
def japanese_font(monkeypatch):
    """日本語テキストの抽出を見るテスト用に、CJK グリフを持つフォントを候補にする。"""
    found = _find_japanese_font()
    if found is None:
        pytest.skip(
            "reportlab で登録できる日本語 TrueType が見つかりません"
            "（Linux では CFF ベースの Noto CJK しか無いことが多い）"
        )
    monkeypatch.setattr(pdf_builder, "JAPANESE_FONT_CANDIDATES", [found])
    return found


@pytest.fixture
def font_cache_reset(monkeypatch):
    """register_japanese_font のモジュールキャッシュを毎回まっさらにする。"""
    monkeypatch.setattr(pdf_builder, "_registered_font_name", None)


@pytest.fixture
def image_folder(tmp_path):
    """検索可能PDFの下地になる白ページ1枚のフォルダ。"""
    folder = tmp_path / "images"
    folder.mkdir()
    Image.new("RGB", (600, 800), "white").save(str(folder / "001.png"))
    return folder


def extract_text(pdf_path):
    """PDF 全ページのテキストを連結して返す。"""
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        return "\n".join(page.get_textpage().get_text_bounded() for page in doc)
    finally:
        doc.close()


def test_register_japanese_font_embeds_truetype(font_cache_reset, any_truetype):
    """候補が読めれば TTF 名が返り、2回目はキャッシュから同じ名前が返る。"""
    name = pdf_builder.register_japanese_font()
    assert name == pdf_builder.EMBEDDED_FONT_NAME
    assert pdf_builder.register_japanese_font() == name


def test_register_japanese_font_falls_back(font_cache_reset, monkeypatch):
    """候補がひとつも無い環境では HeiseiMin-W3 にフォールバックする。"""
    monkeypatch.setattr(pdf_builder, "JAPANESE_FONT_CANDIDATES", [])
    assert pdf_builder.register_japanese_font() == pdf_builder.FALLBACK_FONT_NAME


def test_register_japanese_font_skips_unreadable_candidate(
    font_cache_reset, any_truetype, monkeypatch, tmp_path
):
    """壊れた候補は飛ばして次の候補を使う。"""
    broken = tmp_path / "broken.ttc"
    broken.write_bytes(b"not a font")
    monkeypatch.setattr(
        pdf_builder,
        "JAPANESE_FONT_CANDIDATES",
        [(str(broken), 0), *pdf_builder.JAPANESE_FONT_CANDIDATES],
    )
    assert pdf_builder.register_japanese_font() == pdf_builder.EMBEDDED_FONT_NAME


def test_searchable_pdf_text_is_extractable(
    font_cache_reset, japanese_font, image_folder, tmp_path
):
    """検索可能PDFの日本語テキストが pypdfium2 で抽出できる。"""
    out = tmp_path / "searchable.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(
        str(image_folder),
        [("001.png", SAMPLE_TEXT)],
        str(out),
    )
    assert ok, msg

    extracted = extract_text(out)
    for line in SAMPLE_TEXT.split("\n"):
        assert line in extracted


def test_searchable_pdf_embeds_font_file(font_cache_reset, any_truetype, image_folder, tmp_path):
    """生成PDFに TrueType 埋め込み (/FontFile2) が含まれる。"""
    out = tmp_path / "searchable.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(
        str(image_folder),
        [("001.png", SAMPLE_TEXT)],
        str(out),
    )
    assert ok, msg
    assert b"/FontFile2" in out.read_bytes()


def test_searchable_pdf_fallback_still_builds(
    font_cache_reset, monkeypatch, image_folder, tmp_path
):
    """フォント候補が無くても検索可能PDFの生成は成功する（挙動後退なし）。"""
    monkeypatch.setattr(pdf_builder, "JAPANESE_FONT_CANDIDATES", [])
    out = tmp_path / "fallback.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(
        str(image_folder),
        [("001.png", SAMPLE_TEXT)],
        str(out),
    )
    assert ok, msg
    assert out.exists()
    assert b"/FontFile2" not in out.read_bytes()


def test_text_pdf_text_is_extractable(font_cache_reset, japanese_font, tmp_path):
    """テキストPDFも日本語が抽出でき、フォントが埋め込まれる。"""
    out = tmp_path / "text.pdf"
    ok, msg = pdf_builder.text_to_pdf([("001.png", SAMPLE_TEXT)], str(out))
    assert ok, msg

    assert b"/FontFile2" in out.read_bytes()
    extracted = extract_text(out)
    assert "吾輩は猫である" in extracted


def test_text_pdf_fallback_still_builds(font_cache_reset, monkeypatch, tmp_path):
    """テキストPDFもフォント候補が無い環境で生成できる。"""
    monkeypatch.setattr(pdf_builder, "JAPANESE_FONT_CANDIDATES", [])
    out = tmp_path / "text_fallback.pdf"
    ok, msg = pdf_builder.text_to_pdf([("001.png", SAMPLE_TEXT)], str(out))
    assert ok, msg
    assert out.exists()


# ------------------------------------------------------------
# 不可視テキストの位置（範囲選択でコピーした内容が見えている場所と一致するか）
# ------------------------------------------------------------


def extract_region(pdf_path, page_index, left, bottom, right, top):
    """ページの一部の矩形からテキストを抜く。座標は 0..1 の割合。"""
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        page = doc[page_index]
        w, h = page.get_width(), page.get_height()
        return page.get_textpage().get_text_bounded(
            left=w * left, bottom=h * bottom, right=w * right, top=h * top
        )
    finally:
        doc.close()


@pytest.fixture
def wide_image_folder(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    Image.new("RGB", (1000, 1000), "white").save(str(folder / "001.png"))
    return folder


def test_searchable_pdf_puts_text_where_it_is_shown(
    font_cache_reset, japanese_font, wide_image_folder, tmp_path
):
    """縦書きの右上の列は PDF でも右上から取れる。

    従来はページ左上から横書きで流し込んでいたため、範囲選択でコピーした
    内容が見えている場所と対応しなかった（縦書きでは特に破綻した）。
    """
    from core.ocr_layout import Line, PageLayout

    layout = PageLayout(
        filename="001.png",
        width=1000,
        height=1000,
        lines=[
            # 右端の列＝縦書きの 1 列目
            Line(text="みぎのはしら", left=940, top=0, right=980, bottom=240, vertical=True),
            # 左端の列＝最後の列
            Line(text="ひだりのはしら", left=20, top=700, right=60, bottom=980, vertical=True),
        ],
    )
    out = tmp_path / "positioned.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(str(wide_image_folder), [layout], str(out))
    assert ok, msg

    top_right = extract_region(out, 0, 0.85, 0.65, 1.0, 1.0)
    bottom_left = extract_region(out, 0, 0.0, 0.0, 0.15, 0.35)

    assert "みぎのはしら" in top_right
    assert "ひだりのはしら" not in top_right
    assert "ひだりのはしら" in bottom_left
    assert "みぎのはしら" not in bottom_left


def test_searchable_pdf_scales_text_to_the_image(
    font_cache_reset, japanese_font, wide_image_folder, tmp_path
):
    """OCR にかけた画像が前処理で拡大されていても、座標を実画像に合わせる。"""
    from core.ocr_layout import Line, PageLayout

    # OCR は 2 倍に拡大した画像を見ている（width=2000）が、PDF に載せるのは 1000px
    layout = PageLayout(
        filename="001.png",
        width=2000,
        height=2000,
        lines=[Line(text="みぎのはしら", left=1880, top=0, right=1960, bottom=480, vertical=True)],
    )
    out = tmp_path / "scaled.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(str(wide_image_folder), [layout], str(out))
    assert ok, msg

    assert "みぎのはしら" in extract_region(out, 0, 0.85, 0.65, 1.0, 1.0)


def test_searchable_pdf_still_accepts_plain_pairs(
    font_cache_reset, japanese_font, image_folder, tmp_path
):
    """座標が取れない経路（ページ毎起動のフォールバック）でも従来どおり作れる。"""
    out = tmp_path / "pairs.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(
        str(image_folder), [("001.png", SAMPLE_TEXT)], str(out)
    )
    assert ok, msg
    assert SAMPLE_TEXT.split("\n")[0] in extract_text(out)


def test_searchable_pdf_page_without_coordinates_keeps_its_text(
    font_cache_reset, japanese_font, image_folder, tmp_path
):
    from core.ocr_layout import PageLayout

    out = tmp_path / "fallback.pdf"
    ok, msg = pdf_builder.images_to_searchable_pdf(
        str(image_folder),
        [PageLayout(filename="001.png", fallback_text=SAMPLE_TEXT)],
        str(out),
    )
    assert ok, msg
    assert SAMPLE_TEXT.split("\n")[0] in extract_text(out)
