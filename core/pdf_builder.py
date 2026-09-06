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


def _descent_offset(font_name, size):
    """回転して置いた文字の字面を、列の左端に合わせるための x のずらし幅。

    -90 度回転すると、フォントのベースラインから上 (ascent) が x の正方向に
    写る。原点をそのまま列の左端に置くと字面が descent の分だけ左へはみ出し、
    列の右端に何も無い帯ができる。descent はフォントの単位 (1/1000 em) で
    負の値なので、その分だけ右へずらす。
    """
    try:
        descent = pdfmetrics.getFont(font_name).face.descent
    except Exception:  # noqa: BLE001 - 取れないフォントではずらさない
        return 0.0
    return -float(descent) / 1000.0 * size


def _draw_vertical_line(c, line, page_height, font_name, scale):
    """縦書きの 1 列を、テキスト行列を -90 度回転させて置く。

    **なぜ回転させるか** — 抽出器は読み順をテキスト行列から決める。横組みの
    ままだと「右へ進む文字が格子状に並んでいる」と解釈され、列をまたいで
    横に読まれる (#37)。実測では poppler が 4 列中 0 列しか復元できなかった。
    行列を回して送りを下向きにすれば、poppler も PDFium も列として読む。

    **なぜ 1 文字ずつ置くか** — 列を 1 回の文字列描画にまとめると poppler は
    正しく読むが、**PDFium が列を x の昇順に並べ替えるため読み順が左→右に
    逆転する** (実測。描画順を変えても直らない)。1 文字ずつ置けば両方とも
    右→左のまま読む。1 文字ずつなら OCR の座標をそのまま使えるので、
    列の途中の文字位置もずれない。

    縦組み CMap の CID フォント (UniJIS-UCS2-V) を使う手もあるが、そちらは
    句読点や括弧が縦書き用の異体字に対応付けられ、**コピーすると 。「 が
    ︒ ﹁ (U+FE12 / U+FE41) になってしまう**。回転方式なら埋め込み済みの
    TrueType をそのまま使えるので、コピーした文字は原文どおりになる。
    """
    size = max(1.0, line.font_size * scale)
    step = line.height / len(line.text) * scale
    x = line.left * scale + _descent_offset(font_name, size)
    top = page_height - line.top * scale
    text_obj = c.beginText()
    text_obj.setTextRenderMode(3)  # invisible
    text_obj.setFont(font_name, size)
    for i, ch in enumerate(line.text):
        # 回転後は字幅が列の縦方向の送りになる。半角文字でも 1 マス分になるよう伸ばす
        width = pdfmetrics.stringWidth(ch, font_name, size) or size
        text_obj.setHorizScale(100.0 * step / width)
        # (0, -1, 1, 0) = -90 度回転
        text_obj.setTextTransform(0, -1, 1, 0, x, top - step * i)
        text_obj.textOut(ch)
    # Tz はグラフィクス状態なので ET では戻らない。後続の行に漏らさない
    text_obj.setHorizScale(100.0)
    c.drawText(text_obj)


def _draw_horizontal_line(c, line, page_height, font_name, scale):
    """横書きの 1 行を、字幅の比で bbox に配分して 1 回で置く。

    **なぜ等分ではないか** — NDLOCR-Lite は行単位の bbox しか返さないので、
    以前は行の幅を文字数で等分していた。和文だけなら字幅が一定なので合うが、
    数字や欧字が混ざると字幅が 2 倍以上違い、狭い字のまわりに隙間が空く。
    抽出器はその隙間を語の切れ目とみなすため、``200g`` が ``2 0 0 g``、
    ``Wi-Fi`` が ``W i - F i`` としてコピーされる (#40 の実測。PDFium と
    poppler の両方で再現)。料理本の分量、技術書のコードや型名、和欧混植の
    行がそのまま壊れる。

    フォントの字幅の比で配分すれば、実際に組まれた位置に近いところへ落ちる。
    行全体の幅を bbox に合わせるぶんは Tz で伸縮する。**Tz はテキスト行列の
    x にも掛かる**ので、原点は先に割ってから置く。

    縦書き (``_draw_vertical_line``) と違って 1 文字ずつに分けないのは、
    横書きでは文字送りが書字方向と一致していて、抽出器が並べ替えないため。
    """
    size = max(1.0, line.font_size * scale)
    widths = [pdfmetrics.stringWidth(ch, font_name, size) or size for ch in line.text]
    # 字幅が取れない (総和 0) / bbox がつぶれている行では伸縮しない
    horiz_scale = (line.width * scale / sum(widths)) if sum(widths) and line.width else 1.0
    text_obj = c.beginText()
    text_obj.setTextRenderMode(3)  # invisible
    text_obj.setFont(font_name, size)
    text_obj.setHorizScale(100.0 * horiz_scale)
    # 文字の左下に置く。top は文字の上端なので 1 文字分下げる
    text_obj.setTextOrigin(
        line.left * scale / horiz_scale, page_height - (line.top + line.font_size) * scale
    )
    text_obj.textOut(line.text)
    # Tz はグラフィクス状態なので ET では戻らない。後続の行に漏らさない
    text_obj.setHorizScale(100.0)
    c.drawText(text_obj)


def _draw_positioned_text(c, layout, page_height, font_name, scale=1.0):
    """OCR が返した座標に不可視テキストを置く。置いた行数を返す。

    連結テキストをページ左上から流し込む従来の方法と違い、範囲選択で
    コピーした内容が見えている場所と一致する。

    縦書きの行は 1 文字ずつ、横書きの行は 1 行まるごと置く
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
            _draw_horizontal_line(c, line, page_height, font_name, scale)
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
