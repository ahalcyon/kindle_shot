"""撮影済み PDF のしおりを、Kindle の本が持つ目次で作り直す (#114)。

対応づけの規則は ``core.kindle_toc``。ここでは 1 冊ぶんの段取りだけを持つ:
描画 API から目次とページ範囲を取り（``cached_structure``）、PDF のページに
対応づけ（``plan_book``）、要確認の印が無ければ書き換える（``rebuild_book``）。

呼び出し元は 2 つ:

- ``scripts/rebuild_bookmarks.py``: 蔵書をまとめて処理する（一覧 CSV を出す）
- ``core.pipeline.run_book``: 撮影の流れで、PDF ができた直後に作り直す
"""

from __future__ import annotations

import json
import os
from collections import Counter

from core.kindle_toc import (
    CONFIRMED,
    MOVED,
    UNCONFIRMED,
    UNSEARCHED,
    flatten_toc,
    map_to_pages,
    order_outliers,
    page_offset,
    write_outline,
)

# テキスト層があるとみなす、抜き取りページの合計文字数
TEXT_LAYER_MIN_CHARS = 50
# 並びから外れた項目を除いてよい上限。実測では外れるのは「目次」「Cover」の 1 項目だった。
# これを超えて外れる目次は、対応づけの前提（目次の順＝ページの順）が崩れている
MAX_ORDER_OUTLIERS = 2
MAX_ORDER_OUTLIER_RATIO = 0.1
# 一覧の「要確認」の理由
FLAG_NO_TOC = "目次なし"
FLAG_NO_PAGES = "ページ範囲が取れない"
FLAG_TOC_ORDER = "目次の位置の並びが大きく崩れている"
FLAG_COUNT = "ページ数の差が表紙で説明できない"
FLAG_UNRENDERABLE = "描画できない区間あり"
FLAG_INCOMPLETE = "本の終わりまで走査できていない"
FLAG_NO_TEXT = "テキスト層なし（位置だけで決める）"
# 表紙の分と違うずれ幅をテキストから選んだ区間がある。目視では多くが正しいが、選んだずれ幅で
# 見つかった項目も confirmed に数えるので、confirmed だけでは確かめたことにならない
FLAG_SHIFTED = "テキストからずれ幅を選んだ区間あり"

# これが立っている本はしおりを付けられない（--include-flagged でも書かない）
BLOCKING_FLAGS = {FLAG_NO_TOC, FLAG_NO_PAGES, FLAG_TOC_ORDER}


class UnsupportedBook(Exception):
    """Cloud Reader 非対応の本（Kindle アプリでしか開けない）。しおりは作れない。"""


def cached_structure(cache_dir, asin, *, profile_dir=None, refresh=False):
    """描画 API から取った目次とページ範囲。キャッシュが無ければ本を開いて取る。"""
    path = os.path.join(cache_dir, f"{asin}.json")
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            structure = json.load(f)
        if structure.get("complete"):
            return structure
    from core.headless_browser import open_reader
    from core.headless_capture import BOOK_URL, unsupported_reason
    from core.kindle_toc import fetch_book_structure

    with open_reader(BOOK_URL.format(asin=asin), headless=True, profile_dir=profile_dir) as page:
        if page is None:
            raise RuntimeError("本を開けませんでした")
        # 撮影済みでも、いま非対応になっている本がある（B071GN3JN2。#114 のコメントに実測）。
        # 描画 API の要求が出ないので、そのままだと「描画要求が出ませんでした」で失敗に数える。
        #
        # **判定は取れなかったあと。** 非対応のダイアログが出るまでには時間がかかり
        # （撮影経路は DEFAULT_LOAD_WAIT = 12 秒待ってから見る）、開いた直後に見ると
        # まだ出ていない本を見落とす。取れなかった本だけ見れば、リロードと 15 秒の待ちを
        # 挟んだあとの画面になるうえ、誤判定で取れる本を飛ばす経路も無くなる
        try:
            structure = fetch_book_structure(page)
        except RuntimeError as exc:
            reason = unsupported_reason(page)
            if reason:
                raise UnsupportedBook(reason) from exc
            raise
    if structure.get("complete"):
        # 途中で落ちても壊れた JSON を残さない
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(structure, f, ensure_ascii=False)
        os.replace(tmp, path)
    return structure


def count_outline(reader):
    def walk(items):
        return sum(walk(i) if isinstance(i, list) else 1 for i in items)

    try:
        return walk(reader.outline)
    except Exception:  # noqa: BLE001 - 壊れたしおりは 0 として数える
        return 0


def shift_summary(entries):
    """区間ごとのずれ幅を、本の先頭からの並びで返す（例: ``2→0→2``）。"""
    out: list[str] = []
    for e in entries:
        if not out or out[-1] != str(e.shift):
            out.append(str(e.shift))
    return "→".join(out)


def plan_book(pdf_path, structure):
    """1 冊分の対応づけと、一覧の 1 行ぶんの数字を作る。

    Returns:
        (entries, row, flags)。flags に BLOCKING_FLAGS のどれかがあれば entries は書けない。
    """
    from pypdf import PdfReader

    from core.text_layer import _sample_indexes

    reader = PdfReader(pdf_path)
    pdf_pages = len(reader.pages)
    ranges = structure.get("pages") or []
    render_pages = len(ranges)
    flags = []

    entries = flatten_toc(structure.get("toc"))
    if not entries:
        flags.append(FLAG_NO_TOC)
    if not ranges:
        flags.append(FLAG_NO_PAGES)
    dropped = 0
    if entries:
        outliers = order_outliers(entries)
        dropped = len(outliers)
        if dropped > max(MAX_ORDER_OUTLIERS, len(entries) * MAX_ORDER_OUTLIER_RATIO):
            flags.append(FLAG_TOC_ORDER)
        else:
            # 並びから外れた項目（別の場所を指す「目次」「Cover」など）だけ外して付ける
            skip = set(outliers)
            entries = [e for i, e in enumerate(entries) if i not in skip]
    offset = page_offset(pdf_pages, render_pages)
    if offset is None:
        flags.append(FLAG_COUNT)
    if structure.get("unrenderable"):
        flags.append(FLAG_UNRENDERABLE)
    if not structure.get("complete"):
        flags.append(FLAG_INCOMPLETE)

    sample = _sample_indexes(pdf_pages, size=6)
    chars = sum(len((reader.pages[i].extract_text() or "").strip()) for i in sample)
    has_text = chars >= TEXT_LAYER_MIN_CHARS
    if not has_text:
        flags.append(FLAG_NO_TEXT)

    cache: dict[int, str] = {}

    def page_text(i):
        if i not in cache:
            cache[i] = reader.pages[i].extract_text() or ""
        return cache[i]

    text = page_text if has_text else None
    mappable = not (BLOCKING_FLAGS & set(flags))
    if mappable:
        # ずれ幅は区間ごとにテキストで決まる（core.kindle_toc.map_to_pages）。表紙の分は既定値
        map_to_pages(entries, ranges, pdf_pages, text, offset or 0)
        if offset is not None and any(e.shift != offset for e in entries):
            flags.append(FLAG_SHIFTED)
    hows = Counter(e.how for e in entries) if mappable else Counter()
    row = {
        "pdf_pages": pdf_pages,
        "render_pages": render_pages,
        "shifts": shift_summary(entries) if mappable else "",
        "toc_entries": len(entries),
        "dropped": dropped,
        "old_bookmarks": count_outline(reader),
        "confirmed": hows[CONFIRMED],
        "moved": hows[MOVED],
        "unconfirmed": hows[UNCONFIRMED],
        "unsearched": hows[UNSEARCHED],
        "flags": " / ".join(flags),
    }
    return entries, row, flags


def rebuild_book(pdf_path, structure, *, include_flagged=False, allowed_flags=()):
    """1 冊ぶんのしおりを作り直す。

    Args:
        pdf_path: 撮影済みの PDF
        structure: ``cached_structure`` の結果
        include_flagged: 要確認の印が付いた本も書き換える
        allowed_flags: これだけが付いた本は要確認としない印。``FLAG_NO_TEXT`` を渡すのは
            テキスト層が無いと分かっている形式（``image_pdf``）のとき。漫画は位置だけで
            決まるが、すき間の規則を実機で確かめてあり（目視 10/10）、蔵書 175 冊に
            適用済み（#114 のコメント）。``BLOCKING_FLAGS`` はこれでも書かない

    Returns:
        結果の dict。``written`` が真なら書き換えた。``reason`` に書かなかった理由、
        ``row`` に一覧用の数字、``flags`` に要確認の理由が入る。
    """
    entries, row, flags = plan_book(pdf_path, structure)
    result = {"written": False, "reason": "", "row": row, "flags": flags, "entries": len(entries)}
    if BLOCKING_FLAGS & set(flags):
        result["reason"] = "しおりを付けられない"
        return result
    if set(flags) - set(allowed_flags) and not include_flagged:
        result["reason"] = "要確認"
        return result
    written = write_outline(pdf_path, entries)
    result["written"] = bool(written.get("ok"))
    if not written.get("ok"):
        result["reason"] = str(written.get("error"))
    return result
