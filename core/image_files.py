"""画像ファイルの拡張子定義と列挙の一元化

以前は同義の拡張子タプルが cli.py / trimmer.py / boundary_detector.py /
ui/trim_tab.py / ocr_engine.py / pdf_builder.py に別々に定義されており、
内容も微妙に食い違っていた (変更漏れの温床)。定義をここに集約する。

3 つの定数は意図的に別集合になっている:
- IMAGE_EXTENSIONS: キャプチャ/トリミング/検証の対象 (扱える画像全般)
- OCR_IMAGE_EXTENSIONS: NDLOCR-Lite に渡せる形式 (gif 非対応・tiff 対応)
- PDF_IMAGE_EXTENSIONS: reportlab drawImage で直接 PDF 化できる形式
"""

import os
import re

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
OCR_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif")
PDF_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")


def page_order_key(name):
    """ファイル名をページ順に並べるためのキー。数字の並びは数値として比べる。

    **名前の辞書順で並べない (#107)。** 撮影は ``f"{n:03d}.png"`` で書くので、
    1000 枚を超えると辞書順では ``1000.png`` が ``100.png`` の直後に来る。
    枚数も validate も正常に見えたまま PDF のページ順だけが崩れ、実際に
    2004 ページの本が壊れていた。
    """
    # re.split にキャプチャ付きで渡すと、奇数番目が必ず \d+ の塊になる。
    # isdigit() で判定しない: "①" や "²" は isdigit() が True だが int() で落ちる
    return [
        (0, int(part), part) if i % 2 else (1, 0, part)
        for i, part in enumerate(re.split(r"(\d+)", name))
    ]


def list_images(folder, extensions=IMAGE_EXTENSIONS):
    """フォルダ内の画像ファイル名をページ順で返す。

    os.listdir は順序保証がないため、必ずソートして返す。並びは
    page_order_key（数字を数値として比べる）。

    Raises:
        FileNotFoundError: フォルダが存在しない場合
    """
    return sorted(
        (f for f in os.listdir(folder) if f.lower().endswith(extensions)), key=page_order_key
    )


def clear_images(folder, extensions=IMAGE_EXTENSIONS):
    """フォルダ内の画像ファイルを削除し、削除した枚数を返す。

    前回実行の残骸画像が後段の PDF 化に紛れ込むのを防ぐために使う。
    """
    removed = 0
    for f in list_images(folder, extensions):
        os.remove(os.path.join(folder, f))
        removed += 1
    return removed
