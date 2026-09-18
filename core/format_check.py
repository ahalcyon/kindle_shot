"""出来上がった PDF から、形式の判定が当たっていたかを後から確かめる (#96)。

`core.book_format.decide` は「実測 → シリーズ一致 → ジャンル分け → 旧蔵書」の順で
形式を決めるが、**実測が無い本を `image_pdf` に倒すと、以後まったく測られない**。
judgment が外れていればテキスト層の無い本が出来るのに、気づく道が無かった。

ここでは撮影済みの PDF を描き直して OCR し、**撮影時と同じ数え方**（
``core.ocr_validator.analyze_page`` の本文行）で 1 ページあたりの文字数を出す。

**全文字を数えてはいけない。** 柱・ノンブル・絵柄から拾ったノイズ行まで入り、漫画では
桁が変わる（実測: てーきゅう 3 で、撮影時に空と判定されたページから平均 152.6 字。
本文行だけなら 0.6 字。#96 のコメント）。

実測 20 冊（判定の境目に近い本を厚めに選んだ。抜き取り 20 ページ）:

    image_pdf       2.0 〜  70.1 文字/ページ  13 冊
    searchable_pdf  212.7 〜 493.7 文字/ページ   7 冊

**2 つの形式は完全に分かれる。** 冊ごとの値は撮影時と 3 倍ずれることもあるが
（20 ページの抜き取りなのでばらつく）、必要なのは分離であって再現ではない。
既定の閾値 ``DEFAULT_THRESHOLD``（150）はこの谷の中にある。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from core.book_format import DEFAULT_THRESHOLD, IMAGE, SEARCHABLE

# 抜き取るページ数。20 ページで 1 冊およそ 50 秒（描き直し + OCR）
SAMPLE_PAGES = 20
# 描き直す解像度。撮影時の画像（1600x1200）に近い大きさになる
DPI = 200
# 本文の代表にならない前後（表紙・扉・奥付・広告）を避ける割合
EDGE_RATIO = 0.05

OK = "ok"
SUSPECT_TEXT = "image_pdf だが文字が多い（searchable_pdf にすべき疑い）"
SUSPECT_IMAGE = "searchable_pdf だが文字が少ない（image_pdf にすべき疑い）"


def sample_indexes(pages, size=SAMPLE_PAGES, edge_ratio=EDGE_RATIO):
    """本文の代表になるページ番号（0 始まり）を等間隔に選ぶ。

    先頭と末尾は避ける。表紙・扉・奥付・広告は本文の文字数を代表しない。
    """
    if pages <= 0:
        return []
    lo, hi = int(pages * edge_ratio), max(int(pages * (1 - edge_ratio)), 1)
    if hi - lo < size:
        lo, hi = 0, pages  # 短い本は縁を落とすと足りなくなる。全ページから選ぶ
    span = hi - lo
    if size >= span:
        return list(range(lo, hi))
    step = span / size
    return [min(hi - 1, int(lo + i * step)) for i in range(size)]


def measure_pdf(path, *, sample=SAMPLE_PAGES, dpi=DPI):
    """PDF を描き直して OCR し、1 ページあたりの本文の文字数を測る。

    Returns:
        ``{"pdf_pages", "sampled", "chars", "cpp"}``。OCR が使えなければ RuntimeError。
    """
    import pypdfium2 as pdfium

    from core.ocr_engine import process_folder_collect
    from core.ocr_validator import analyze_page

    work = tempfile.mkdtemp(prefix="format_check_")
    try:
        doc = pdfium.PdfDocument(path)
        try:
            pages = len(doc)
            picks = sample_indexes(pages, sample)
            for i in picks:
                image = doc[i].render(scale=dpi / 72).to_pil()
                image.save(os.path.join(work, f"{i:05d}.png"))
        finally:
            doc.close()
        if not picks:
            raise RuntimeError("ページがありません")
        ok, results = process_folder_collect(work, layout=True)
        if not ok:
            raise RuntimeError(str(results))
        chars = sum(analyze_page(layout)["chars"] for layout in results)
        return {
            "pdf_pages": pages,
            "sampled": len(picks),
            "chars": chars,
            "cpp": round(chars / len(picks), 1),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verdict(fmt, cpp, *, threshold=DEFAULT_THRESHOLD):
    """測った文字数/ページが、その形式と食い違っていないか。

    **間違え方は対称ではない**（``core.book_format`` の説明）。``image_pdf`` の外れは
    テキスト層の無い本が残り、直すには撮り直すしかない。``searchable_pdf`` の外れは
    テキスト層を剥がせば直る。どちらも印は出すが、急ぐのは前者。
    """
    if fmt == IMAGE and cpp > threshold:
        return SUSPECT_TEXT
    if fmt == SEARCHABLE and cpp < threshold:
        return SUSPECT_IMAGE
    return OK
