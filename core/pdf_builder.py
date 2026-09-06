"""PDF生成モジュール

PNG画像をファイル名順に1つのPDFファイルに結合する。
OCR結果からテキストのみのPDF、または画像+不可視テキストの検索可能PDFも生成できる。

chapters 引数 ([Chapter, ...]) を渡すと、対応するページにしおり
(PDF アウトライン) を埋め込む。chapter_detector.detect_chapters の
出力をそのまま渡せる。

テキストを含む PDF (検索可能PDF / テキストPDF) は、Windows 標準の日本語
TrueType フォントをサブセット埋め込みして生成する (register_japanese_font)。
"""

import os
from xml.sax.saxutils import escape

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from core.chapter_detector import chapters_by_filename as _chapters_by_filename
from core.image_files import PDF_IMAGE_EXTENSIONS, list_images
from core.ocr_layout import PageLayout

# 埋め込み用の日本語 TrueType 候補 (path, subfontIndex)。上から順に試す。
JAPANESE_FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msgothic.ttc", 0),  # MS ゴシック
    (r"C:\Windows\Fonts\YuGothM.ttc", 0),  # 游ゴシック Medium
    (r"C:\Windows\Fonts\meiryo.ttc", 0),  # メイリオ
]

# 埋め込みに成功したときの reportlab 上のフォント名
EMBEDDED_FONT_NAME = "KindleShotJP"
# 候補がひとつも読めなかったときのフォールバック (非埋め込み CID フォント)
FALLBACK_FONT_NAME = "HeiseiMin-W3"

# register_japanese_font() の結果キャッシュ。pdfmetrics への登録は1回だけ行う。
_registered_font_name = None


def register_japanese_font():
    """日本語フォントを1回だけ reportlab に登録し、そのフォント名を返す。

    JAPANESE_FONT_CANDIDATES を順に試し、読めたものを TTFont として登録する。
    TTFont ならフォントのサブセット埋め込みと ToUnicode CMap が自動生成されるため、
    日本語 CMap を持たないビューアでも検索・テキスト抽出ができる。

    候補がひとつも使えない環境では従来どおり非埋め込みの
    UnicodeCIDFont('HeiseiMin-W3') にフォールバックする (挙動後退なし)。

    Returns:
        登録済みフォント名 (EMBEDDED_FONT_NAME または FALLBACK_FONT_NAME)
    """
    global _registered_font_name
    if _registered_font_name is not None:
        return _registered_font_name

    for path, subfont_index in JAPANESE_FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(EMBEDDED_FONT_NAME, path, subfontIndex=subfont_index))
        except Exception:
            continue  # 壊れた/未対応のフォントは飛ばして次の候補へ
        _registered_font_name = EMBEDDED_FONT_NAME
        return _registered_font_name

    pdfmetrics.registerFont(UnicodeCIDFont(FALLBACK_FONT_NAME))
    _registered_font_name = FALLBACK_FONT_NAME
    return _registered_font_name


def _emit_bookmark(c, key, title, level):
    """canvas に bookmark + outline entry を 1 件追加する。

    title が長すぎる場合 PDF ビューアで切り詰められるので 80 文字に制限する。
    """
    safe_title = title[:80] if title else "(untitled)"
    c.bookmarkPage(key)
    c.addOutlineEntry(safe_title, key, level=max(0, level - 1), closed=False)


class _BookmarkFlowable(Flowable):
    """SimpleDocTemplate 用: 描画時に bookmark + outline を追加するゼロサイズ Flowable。"""

    def __init__(self, key, title, level=1):
        super().__init__()
        self.key = key
        self.title = title
        self.level = level

    def wrap(self, _aW, _aH):
        return (0, 0)

    def draw(self):
        _emit_bookmark(self.canv, self.key, self.title, self.level)


class _OutlineCanvas(canvas.Canvas):
    """save 時にアウトラインパネルを開く canvas。SimpleDocTemplate に渡す用。"""

    def save(self):
        self.showOutline()
        super().save()


def images_to_pdf(folder_path, output_folder, output_filename, on_progress=None, chapters=None):
    """指定フォルダ内の画像をPDFに変換する。

    Args:
        folder_path: 画像フォルダパス
        output_folder: 出力先フォルダパス
        output_filename: 出力PDFファイル名
        on_progress: 進捗コールバック (current, total, filename)
        chapters: しおり用の章リスト (chapter_detector.Chapter のリスト)

    Returns:
        (success, message) のタプル
    """
    image_files = list_images(folder_path, PDF_IMAGE_EXTENSIONS)

    if not image_files:
        return False, "指定されたフォルダに画像ファイルが見つかりません。"

    if not output_filename.lower().endswith(".pdf"):
        output_filename += ".pdf"

    os.makedirs(output_folder, exist_ok=True)
    output_pdf = os.path.join(output_folder, output_filename)

    chapter_map = _chapters_by_filename(chapters)
    c = canvas.Canvas(output_pdf)
    total_files = len(image_files)

    try:
        for i, image_file in enumerate(image_files, 1):
            full_path = os.path.join(folder_path, image_file)
            with Image.open(full_path) as img:
                width, height = img.size
            c.setPageSize((width, height))
            c.drawImage(full_path, 0, 0, width, height)
            ch = chapter_map.get(image_file)
            if ch is not None:
                _emit_bookmark(c, f"page_{i}", ch.title, ch.level)
            c.showPage()

            if on_progress:
                on_progress(i, total_files, image_file)

        if chapter_map:
            c.showOutline()
        c.save()
    except Exception as e:
        return False, f"PDF作成中にエラー: {e}"

    return True, f"PDFファイルを作成しました: {output_pdf}"


def _draw_vertical_line(c, line, page_height, font_name, scale):
    """縦書きの 1 列を、1 回の描画でまとめて置く。

    **なぜ 1 回にするか** — 1 文字ずつ独立に置くと PDF の内容ストリームには
    バラバラのグリフが並ぶだけで「どれが同じ列か」の情報が残らない。抽出器は
    幾何的に推測するしかなく、横組みフォントの送りに引きずられて列をまたいで
    横に読んでしまう (#37)。1 回の文字列描画にすれば列がそのまま残る。

    **どう下向きに送るか** — 横組みフォントのまま、テキスト行列を -90 度
    回転させる。文字送りが下向きになり、列全体が 1 つの文字列として並ぶ。
    不可視 (レンダーモード 3) なので字形の向きは問題にならない。

    縦組み CMap の CID フォント (UniJIS-UCS2-V) を使う手もあるが、そちらは
    句読点や括弧が縦書き用の異体字に対応付けられ、**コピーすると 。「 が
    ︒ ﹁ (U+FE12 / U+FE41) になってしまう**。回転方式なら埋め込み済みの
    TrueType をそのまま使えるので、コピーした文字は原文どおりになる。

    送りは bbox の高さを文字数で割った実測値に合わせる。欧文や数字は字幅が
    全角より狭く列が詰まってしまうので、実際の文字列幅との差を
    ``setCharSpace`` で配り直す。
    """
    n = len(line.text)
    step = line.height / n * scale
    size = max(1.0, step)
    # 回転後は「文字列の幅」が列の長さになる。実測の送りとの差を均等に配る
    advance = pdfmetrics.stringWidth(line.text, font_name, size)
    text_obj = c.beginText()
    text_obj.setTextRenderMode(3)  # invisible
    text_obj.setFont(font_name, size)
    text_obj.setCharSpace((step * n - advance) / n)
    # (0, -1, 1, 0) = -90 度回転。原点は列の左上に置く
    text_obj.setTextTransform(0, -1, 1, 0, line.left * scale, page_height - line.top * scale)
    text_obj.textOut(line.text)
    c.drawText(text_obj)


def _draw_line_per_char(c, line, page_height, font_name, scale):
    """1 行を 1 文字ずつ、bbox を等分した位置に置く。

    横書きはこちら。文字送りが書字方向 (横) と一致するので、1 文字ずつでも
    抽出器は正しく 1 行としてまとめる。フォントの字幅に依らず OCR の座標を
    そのまま使えるため、欧文混じりの行でも位置がずれない。
    """
    size = max(1.0, line.font_size * scale)
    text_obj = c.beginText()
    text_obj.setTextRenderMode(3)  # invisible
    text_obj.setFont(font_name, size)
    for ch, left, top in line.char_positions():
        # 文字の左下に置く。top は文字の上端なので 1 文字分下げる
        text_obj.setTextOrigin(left * scale, page_height - (top + line.font_size) * scale)
        text_obj.textOut(ch)
    c.drawText(text_obj)


def _draw_positioned_text(c, layout, page_height, font_name, scale=1.0):
    """OCR が返した座標に不可視テキストを置く。置いた行数を返す。

    連結テキストをページ左上から流し込む従来の方法と違い、範囲選択で
    コピーした内容が見えている場所と一致する。

    縦書きの行は 1 列まるごと、横書きの行は 1 文字ずつ置く
    (理由は各ヘルパーの docstring を参照)。

    PDF の原点は左下、OCR の座標は左上なので y を反転する。
    """
    drawn = 0
    for line in layout.lines:
        if not line.text:
            continue
        if line.vertical:
            _draw_vertical_line(c, line, page_height, font_name, scale)
        else:
            _draw_line_per_char(c, line, page_height, font_name, scale)
        drawn += 1
    return drawn


def _draw_flowed_text(c, text, page_height, font_name):
    """座標が無いページ用。ページ左上から横書きで流し込む（検索のみ対応）。"""
    font_size = 12
    leading = font_size * 1.4
    c.setFont(font_name, font_size)
    text_obj = c.beginText(0, page_height - font_size)
    text_obj.setTextRenderMode(3)  # invisible
    for line in text.split("\n"):
        line = line.strip()
        if line:
            text_obj.textLine(line)
        else:
            text_obj.moveCursor(0, leading)
    c.drawText(text_obj)


def images_to_searchable_pdf(image_folder, results, output_path, on_progress=None, chapters=None):
    """画像+不可視OCRテキストの検索可能PDFを生成する。

    見た目は画像PDFと同一だが、OCRテキストが不可視レイヤーとして
    重ねられており、テキスト検索（Ctrl+F）と範囲選択コピーができる。

    results に PageLayout（行ごとの座標つき）を渡すと、不可視テキストを
    **文字の位置に重ねる**。(filename, text) のタプルを渡した場合は座標が
    無いので、従来どおりページ左上から流し込む（検索はできるが、範囲選択で
    コピーした内容は見えている場所と対応しない。縦書きでは特に破綻する）。

    Args:
        image_folder: 画像フォルダパス
        results: [PageLayout, ...] または [(filename, text), ...] のリスト
        output_path: 出力PDFファイルパス
        on_progress: 進捗コールバック (current, total, filename)
        chapters: しおり用の章リスト (chapter_detector.Chapter のリスト)

    Returns:
        (success, message) のタプル
    """
    if not results:
        return False, "変換するデータがありません。"

    if not output_path.lower().endswith(".pdf"):
        output_path += ".pdf"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    try:
        font_name = register_japanese_font()

        chapter_map = _chapters_by_filename(chapters)
        c = canvas.Canvas(output_path)
        total = len(results)

        for i, page in enumerate(results, 1):
            layout = page if isinstance(page, PageLayout) else None
            filename, text = page.as_pair() if layout else page
            image_path = os.path.join(image_folder, filename)
            if not os.path.exists(image_path):
                continue

            img = Image.open(image_path)
            width, height = img.size
            c.setPageSize((width, height))

            # 画像を描画
            c.drawImage(image_path, 0, 0, width, height)
            img.close()

            # 不可視テキストをオーバーレイ
            if layout is not None and layout.positioned:
                # OCR にかけた画像が前処理で拡大されていると座標系がずれる。
                # 記録された画像サイズと実物の比で合わせる
                scale = width / layout.width if layout.width else 1.0
                _draw_positioned_text(c, layout, height, font_name, scale=scale)
            elif text.strip():
                _draw_flowed_text(c, text, height, font_name)

            ch = chapter_map.get(filename)
            if ch is not None:
                _emit_bookmark(c, f"page_{i}", ch.title, ch.level)

            c.showPage()

            if on_progress:
                on_progress(i, total, filename)

        if chapter_map:
            c.showOutline()
        c.save()
        return True, f"検索可能PDFを作成しました: {output_path}"

    except Exception as e:
        return False, f"検索可能PDF生成エラー: {e}"


def text_to_pdf(results, output_path, on_progress=None, chapters=None):
    """OCR結果からテキストのみのPDFを生成する。

    Args:
        results: [(filename, text), ...] のリスト
        output_path: 出力PDFファイルパス
        on_progress: 進捗コールバック (current, total, filename)
        chapters: しおり用の章リスト (chapter_detector.Chapter のリスト)

    Returns:
        (success, message) のタプル
    """
    if not results:
        return False, "変換するデータがありません。"

    if not output_path.lower().endswith(".pdf"):
        output_path += ".pdf"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    try:
        # 日本語フォントを登録（可能なら TTF をサブセット埋め込み）
        font_name = register_japanese_font()

        # スタイル定義
        heading_style = ParagraphStyle(
            "JaHeading",
            fontName=font_name,
            fontSize=14,
            leading=20,
            spaceAfter=10,
            spaceBefore=15,
        )
        body_style = ParagraphStyle(
            "JaBody",
            fontName=font_name,
            fontSize=10,
            leading=16,
            firstLineIndent=10,
        )

        chapter_map = _chapters_by_filename(chapters)
        canvasmaker = _OutlineCanvas if chapter_map else canvas.Canvas

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
        )

        story = []
        total = len(results)

        for i, (filename, text) in enumerate(results, 1):
            if i > 1:
                story.append(PageBreak())

            ch = chapter_map.get(filename)
            if ch is not None:
                story.append(_BookmarkFlowable(f"page_{i}", ch.title, ch.level))

            # ページヘッダー（ファイル名）
            story.append(Paragraph(escape(filename), heading_style))
            story.append(Spacer(1, 5 * mm))

            # テキスト本文を段落ごとに追加
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(escape(line), body_style))
                else:
                    story.append(Spacer(1, 3 * mm))

            if on_progress:
                on_progress(i, total, filename)

        doc.build(story, canvasmaker=canvasmaker)
        return True, f"テキストPDFを作成しました: {output_path}"

    except Exception as e:
        return False, f"テキストPDF生成エラー: {e}"
