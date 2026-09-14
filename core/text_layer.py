"""検索可能 PDF から不可視の OCR テキスト層を剥がして画像 PDF に戻す。

`image_pdf` と `searchable_pdf` は**同じ画像を同じ座標に描いている**
（`pdf_builder.images_to_pdf` と `images_to_searchable_pdf` のどちらも
`c.drawImage(path, 0, 0, width, height)`）。違いは searchable 側に
不可視テキスト (`setTextRenderMode(3)`) とフォントが載ることだけ。

したがって**テキスト描画だけを取り除けば image_pdf と同じものになる**。
撮り直す必要がない。漫画を誤って searchable_pdf で作ってしまったとき、
1 冊 10 分のキャプチャではなく十数秒の後処理で直せる。

剥がすのは `BT` ... `ET` のブロックのみ。画像 XObject にも、ページサイズにも、
しおりにも触らない。
"""

import os
import tempfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, NameObject

# 検証で描画を突き合わせるページ数。全ページ描画すると 1 冊あたり分単位になる。
# 先頭・中央・末尾を見れば、テキスト層の剥がし漏れも画像の破壊も出る
VERIFY_PAGES = 4

# 剥がしたあとに許容する抽出文字数。0 にできない PDF は作っていないので 0。
MAX_TEXT_AFTER = 0


def strip_text_operations(page, writer):
    """1 ページ分の content stream からテキスト描画を取り除く。

    Returns:
        取り除いた BT ブロックの数
    """
    contents = page.get_contents()
    if contents is None:
        return 0
    stream = ContentStream(contents, writer)
    kept = []
    removed = 0
    in_text = False
    for operands, operator in stream.operations:
        if operator == b"BT":
            in_text = True
            removed += 1
            continue
        if operator == b"ET":
            in_text = False
            continue
        if in_text:
            continue
        kept.append((operands, operator))
    stream.operations = kept
    page.replace_contents(stream)

    # フォントを残すと、文字が無いのにサブセットフォントだけ抱えた PDF になる
    resources = page.get("/Resources")
    if resources is not None and "/Font" in resources:
        del resources[NameObject("/Font")]
    return removed


def _sample_indexes(count, size=VERIFY_PAGES):
    """検証に使うページ番号（0 始まり）を先頭・中央・末尾から選ぶ。"""
    if count <= 0:
        return []
    if count <= size:
        return list(range(count))
    picks = {0, count - 1}
    for i in range(1, size - 1):
        picks.add(count * i // (size - 1))
    return sorted(picks)[:size]


def extracted_chars(path, indexes):
    """指定ページから抽出できる文字数の合計。"""
    reader = PdfReader(path)
    total = 0
    for i in indexes:
        if 0 <= i < len(reader.pages):
            total += len((reader.pages[i].extract_text() or "").strip())
    return total


def render_digests(path, indexes, scale=0.5):
    """指定ページを描画した結果のハッシュ。画像が壊れていないかを見るため。

    描画には pypdfium2 を使う（PDF 化の検証で既に使っている依存）。
    """
    import hashlib

    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    try:
        out = []
        for i in indexes:
            if 0 <= i < len(doc):
                bitmap = doc[i].render(scale=scale).to_pil()
                out.append(hashlib.sha256(bitmap.tobytes()).hexdigest())
        return out
    finally:
        doc.close()


def strip_file(path, *, verify=True):
    """PDF のテキスト層を剥がして同じパスに置き換える。

    **検証に通らなければ元のファイルを残す。** 蔵書を直接書き換えるので、
    「たぶん大丈夫」で差し替えない。見るのは次の 3 つ:

    - ページ数が変わっていないこと
    - 抜き取ったページの描画結果が 1 ピクセルも変わっていないこと
    - 抜き取ったページから文字が取れなくなっていること

    Returns:
        結果の dict。``ok`` が False なら ``error`` に理由が入り、
        元のファイルは手つかずで残る。
    """
    result = {"path": path, "ok": False}
    try:
        reader = PdfReader(path)
        pages = len(reader.pages)
        indexes = _sample_indexes(pages)
        before_text = extracted_chars(path, indexes) if verify else None
        before_render = render_digests(path, indexes) if verify else None
        del reader

        writer = PdfWriter(clone_from=path)
        removed = sum(strip_text_operations(page, writer) for page in writer.pages)
        writer.compress_identical_objects()

        folder = os.path.dirname(path) or "."
        fd, created = tempfile.mkstemp(suffix=".pdf", dir=folder)
        os.close(fd)
        tmp: str | None = created
        try:
            with open(created, "wb") as f:
                writer.write(f)

            if verify:
                after = PdfReader(created)
                if len(after.pages) != pages:
                    raise ValueError(f"ページ数が変わりました: {pages} -> {len(after.pages)}")
                del after
                after_text = extracted_chars(created, indexes)
                if after_text > MAX_TEXT_AFTER:
                    raise ValueError(f"テキストが残っています: {after_text} 文字")
                after_render = render_digests(created, indexes)
                if after_render != before_render:
                    raise ValueError("描画結果が変わりました（画像を壊しています）")
                result["verified_pages"] = len(indexes)
                result["chars_before"] = before_text
                result["chars_after"] = after_text

            before_size = os.path.getsize(path)
            os.replace(created, path)
            tmp = None
            result.update(
                ok=True,
                pages=pages,
                removed=removed,
                size_before=before_size,
                size_after=os.path.getsize(path),
            )
        finally:
            if tmp is not None and os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:  # noqa: BLE001 - 1 冊の失敗で残りを止めない
        result["error"] = f"{type(e).__name__}: {e}"
    return result
