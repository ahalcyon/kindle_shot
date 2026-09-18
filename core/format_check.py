"""出来上がった PDF から、形式の判定が当たっていたかを後から確かめる (#96)。

`core.book_format.decide` は「実測 → シリーズ一致 → ジャンル分け → 旧蔵書」の順で
形式を決めるが、**実測が無い本を `image_pdf` に倒すと、以後まったく測られない**。
judgment が外れていればテキスト層の無い本が出来るのに、気づく道が無かった。

ここでは撮影済みの PDF を描き直して OCR し、**行の選び方は撮影時と同じ**
（``core.ocr_validator.analyze_page`` の本文行）で 1 ページあたりの文字数を出す。

**ページの選び方は撮影時と違う。** 撮影時は本 1 冊の全ページを数えるが、ここは
``SAMPLE_PAGES`` ページの抜き取りで、前付け・奥付を外すぶん系統的に上振れする
（外れの向きは「文字が多い」側＝安い誤りなので、この用途では困らない）。

**全文字を数えてはいけない。** 柱・ノンブル・絵柄から拾ったノイズ行まで入り、漫画では
桁が変わる（実測: てーきゅう 3 で、撮影時に空と判定されたページから平均 152.6 字。
本文行だけなら 0.6 字。#96 のコメント）。

実測 20 冊（判定の境目に近い本を厚めに選んだ。抜き取り 20 ページ。9 系列にばらしてある）:

    image_pdf       2.0 〜  70.1 文字/ページ  13 冊
    searchable_pdf  212.7 〜 493.7 文字/ページ   7 冊

この 20 冊では 2 つの形式が分かれた。**ただしこれは観測範囲であって保証ではない。**
冊ごとの値は抜き取りのばらつきで大きく振れる（同じ本で 20 ページなら 36.4、5 ページなら
97.4 になった例がある）。抜き取りを小さくするほど当てにならない。``THRESHOLD``（150）は
この谷の中に置いているが、余裕は image_pdf 側で 2 倍程度しかない。

**この道具は「疑い」を出すためのもので、自動では直さない。** 印が付いた本は人が見る。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from core.book_format import IMAGE, SEARCHABLE

# 抜き取るページ数。20 ページで 1 冊およそ 30 秒（描き直し + OCR）
SAMPLE_PAGES = 20
# 描き直す解像度。**撮影した画像そのものが出る値**。蔵書の PDF はページの大きさを
# 画像のピクセル数にしている（core/pdf_builder.py の setPageSize）ので、72dpi = 等倍。
# 実測: 200dpi にすると 2.78 倍に引き伸ばされ、1.6〜1.9 倍遅くなるのに結果はほぼ同じ
# （てーきゅう 3: 99.0 対 97.4、みんなのフィードバック大全: 1183.0 対 1187.8）
DPI = 72
# 本文の代表にならない前後を避ける割合。表紙・扉・目次・奥付・広告は文字数が本文と
# 大きく違う。0.05 は「前後 5% を捨てる」という目安で、細かく詰めていない
EDGE_RATIO = 0.05
# 形式の境目にする文字数/ページ。**この測り方（本文行・抜き取り）で決めた値**で、
# 撮影時の core.book_format.DEFAULT_THRESHOLD とはたまたま同じ値というだけ。
# 片方を動かしてももう片方は追随しない
THRESHOLD = 150.0

OK = "ok"
UNKNOWN = "形式が不明（image_pdf でも searchable_pdf でもない）"
SUSPECT_TEXT = "image_pdf だが文字が多い（searchable_pdf にすべき疑い）"
SUSPECT_IMAGE = "searchable_pdf だが文字が少ない（image_pdf にすべき疑い）"


def sample_indexes(pages, size=SAMPLE_PAGES, edge_ratio=EDGE_RATIO):
    """本文の代表になるページ番号（0 始まり）を等間隔に選ぶ。

    先頭と末尾は避ける。表紙・扉・奥付・広告は本文の文字数を代表しない。
    ``core.text_layer._sample_indexes`` は逆に先頭と末尾を必ず含む（テキスト層を剥がしても
    描画が変わらないことを確かめる用途で、端のページこそ見たい）。用途が違うので分けてある。
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


def verdict(fmt, cpp, *, threshold=THRESHOLD):
    """測った文字数/ページが、その形式と食い違っていないか。

    **間違え方は対称ではない**（``core.book_format`` の説明）。``image_pdf`` の外れは
    テキスト層の無い本が残り、直すには撮り直すしかない。``searchable_pdf`` の外れは
    テキスト層を剥がせば直る。どちらも印は出すが、急ぐのは前者。
    """
    if fmt not in (IMAGE, SEARCHABLE):
        return UNKNOWN  # 形式が分からない本を黙って ok にしない
    if fmt == IMAGE and cpp > threshold:
        return SUSPECT_TEXT
    if fmt == SEARCHABLE and cpp < threshold:
        return SUSPECT_IMAGE
    return OK
