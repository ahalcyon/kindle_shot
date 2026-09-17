"""Kindle の本が持つ目次から、PDF のしおりを作る (#114)。

OCR の文字から章見出しを推測する方式（``core.chapter_detector``）は崩れやすい。
縦書きの中の横組み数字を読み違え、目次ページの各行をしおりにし、章番号と章題が
別ページの本では 1 章に 2 本付ける。蔵書の多くでしおりが崩れていた。

Kindle Cloud Reader の描画 API（``renderer/render``）の応答には、本の目次
（``toc.json``: 名前・階層・位置 ``tocPositionId``）と、各ページの位置範囲
（``layout.json``: ``startPositionId``〜``endPositionId``）が入っている。
**目次の位置を含むページ**にしおりを付ければ、名前も階層も本のとおりになる。

ページへの対応づけ（実測で決めた規則。#114 のコメントに目視の結果がある）:

- 描画のページと撮影したページは同じ並び。PDF の先頭に表紙を足した本は 1 ページずらす
- 既存の PDF には余分なページ・欠けたページがあり、位置だけだと数ページずれる本がある。
  PDF のテキスト層で、推定の**後ろ** ``SEARCH_AHEAD`` ページまでに章名があればそこへ動かす
- **前へは動かさない。** 前への補正は目次ページや本文中の言及に当たっていて、目視で全部誤りだった
- 章名は番号（「第1章」「1-2-3」「Chapter 4」）を落とした部分で探す。番号は OCR が崩しやすい。
  1 文字の章名は本文のどこにでも当たるので探さない
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# テキストで補正するときに見る、推定より後ろのページ数。実測のずれは +1〜+3 だった
SEARCH_AHEAD = 3
# 章名で探すときに使う長さ。長すぎると OCR の読み違いで当たらず、短すぎると本文に当たる
PROBE_LEN = 10
MIN_PROBE_LEN = 2

EXACT = "exact"  # 位置から出したページでそのまま（テキストでも確かめられたか、テキストが無い）
MOVED = "moved"  # テキストで後ろへ補正した
UNCONFIRMED = "unconfirmed"  # テキストはあるが章名が見つからず、位置から出したページのまま


@dataclass
class TocEntry:
    """しおり 1 件。page は PDF の 0 始まりのページ番号。"""

    level: int
    title: str
    position: int
    page: int = 0
    estimated: int = 0
    how: str = EXACT


def flatten_toc(toc) -> list[TocEntry]:
    """``toc.json`` の入れ子を、出てくる順の平らな列にする。level は 1 始まり。"""
    out: list[TocEntry] = []

    def walk(items, level):
        for item in items or []:
            label = (item.get("label") or "").strip()
            position = item.get("tocPositionId")
            if label and isinstance(position, int):
                out.append(TocEntry(level, label, position))
            walk(item.get("entries"), level + 1)

    walk(toc, 1)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text or "")).lower()


_NUMBERING_RE = re.compile(
    r"^(?:第\s*\d+\s*[章部節編巻]|chapter\s*\d+|part\s*\d+|\d+(?:[-.]\d+)*)\s*", re.IGNORECASE
)


def title_probe(title: str) -> str:
    """テキスト層で探すときの文字列。番号を落とし、``/`` で区切られていれば最初の名前だけ使う。"""
    s = _NUMBERING_RE.sub("", unicodedata.normalize("NFKC", title))
    for part in re.split(r"[/／]", s):
        probe = _norm(part)
        if probe:
            return probe[:PROBE_LEN]
    return _norm(title)[:PROBE_LEN]


def page_offset(pdf_pages: int, render_pages: int) -> int | None:
    """PDF と描画のページ数の差を、先頭に足した表紙で説明できればその数を返す。

    0 か 1 以外は None（余分なページ・欠けたページがあり、しおりがずれうる）。
    """
    diff = pdf_pages - render_pages
    return diff if diff in (0, 1) else None


def map_to_pages(entries, page_starts, pdf_pages, page_text=None, offset=0):
    """各しおりに PDF のページを割り当てる（entries を書き換えて返す）。

    Args:
        entries: ``flatten_toc`` の結果
        page_starts: 描画の各ページの ``startPositionId``（昇順）
        pdf_pages: PDF のページ数
        page_text: ``page_text(i)`` で i ページ目のテキストを返す関数。None ならテキストで補正しない
        offset: PDF の先頭に足したページ数（表紙）
    """
    last = max(pdf_pages - 1, 0)
    prev = 0
    for entry in entries:
        index = max(bisect.bisect_right(page_starts, entry.position) - 1, 0)
        estimated = min(index + offset, last)
        # 目次の並びどおりに、ページが戻らないようにする
        estimated = max(estimated, prev)
        entry.estimated = estimated
        entry.page = estimated
        entry.how = EXACT
        probe = title_probe(entry.title)
        if page_text is not None and len(probe) >= MIN_PROBE_LEN:
            found = None
            for p in range(estimated, min(estimated + SEARCH_AHEAD, last) + 1):
                if probe in _norm(page_text(p)):
                    found = p
                    break
            if found is None:
                entry.how = UNCONFIRMED
            elif found != estimated:
                entry.page = found
                entry.how = MOVED
        prev = entry.page
    return entries


# ---------------------------------------------------------------------------
# 描画 API から目次とページの位置範囲を取る
# ---------------------------------------------------------------------------

# 1 回の要求で描かせるページ数。多いと描けないページが混ざったときに要求ごと落ちる (#104)
SCAN_NUM_PAGES = 4
# 描けない区間の先を探すときの刻み。区間の長さは実測で数十〜数百位置
SKIP_STEP = 25
SKIP_STEP_FAR = 500
SKIP_NEAR = 2000
# 描けない区間を探す上限（位置の数）。これを超えたら走査を打ち切り、打ち切ったと記録する
SKIP_LIMIT = 200_000
# 通信の一時的な失敗のやり直し回数
FETCH_RETRIES = 4


def _sub_query(url, **params):
    for key, value in params.items():
        url = re.sub(rf"([?&]{key}=)[^&]*", rf"\g<1>{value}", url)
    return url


def _untar(body):
    import io
    import json
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(body)) as archive:
        names = set(archive.getnames())

        def load(name):
            if name not in names:
                return None
            member = archive.extractfile(name)
            return json.loads(member.read()) if member else None

        return load("layout.json"), load("toc.json"), load("metadata.json")


def fetch_book_structure(page, *, emit=None, wait_ms=15000):
    """開いている本について、目次と全ページの位置範囲を描画 API から取る。

    **読書位置は動かさない。** リーダーのページ送りはせず、リーダーが最初に出した
    描画要求の URL と認証ヘッダーを使って、位置を指定した要求を順に送る。
    描けない区間（#104）は先の位置を探して飛ばし、``unrenderable`` に残す。

    Returns:
        {"toc", "metadata", "pages": [[start, end], ...], "unrenderable": [[from, resume], ...],
         "complete": 最後の位置まで取れたか}
    """
    emit = emit or (lambda *a, **k: None)
    requests = []

    def on_request(request):
        if "/renderer/render" in request.url:
            requests.append(request)

    page.on("request", on_request)
    page.reload(wait_until="domcontentloaded")
    waited = 0
    while not requests and waited < wait_ms:
        page.wait_for_timeout(500)
        waited += 500
    if not requests:
        raise RuntimeError("描画要求が出ませんでした（本を開けていない可能性）")
    first = requests[0]
    state = {
        "url": _sub_query(first.url, skipPageCount=0),
        "headers": {k: v for k, v in first.headers.items() if not k.startswith(":")},
    }

    def refresh():
        requests.clear()
        page.reload(wait_until="domcontentloaded")
        for _ in range(wait_ms // 500):
            if requests:
                break
            page.wait_for_timeout(500)
        if requests:
            state["url"] = _sub_query(requests[0].url, skipPageCount=0)
            state["headers"] = {
                k: v for k, v in requests[0].headers.items() if not k.startswith(":")
            }

    def get(num_pages, position):
        for _ in range(FETCH_RETRIES):
            try:
                resp = page.request.get(
                    _sub_query(state["url"], numPage=num_pages, startingPosition=position),
                    headers=state["headers"],
                    timeout=90000,
                )
            except Exception:  # noqa: BLE001 - 通信の一時的な失敗はやり直す
                page.wait_for_timeout(5000)
                continue
            if resp.status in (401, 403):
                refresh()
                continue
            if resp.status >= 500:
                return None
            if resp.status != 200:
                page.wait_for_timeout(5000)
                continue
            return _untar(resp.body())
        raise RuntimeError(f"描画 API の要求に失敗し続けました（位置 {position}）")

    pages: list[list[int]] = []
    unrenderable: list[list[int | None]] = []
    toc = metadata = None
    position = 0
    complete = False
    while True:
        got = get(SCAN_NUM_PAGES, position) or get(1, position)
        if got is None:
            # 描けない。先へ探して、描ける位置から続ける
            probe = position + SKIP_STEP
            resume = None
            while probe - position < SKIP_LIMIT:
                again = get(1, probe)
                if again and again[0] and again[0][0]["startPositionId"] > position:
                    resume = again[0][0]["startPositionId"]
                    break
                probe += SKIP_STEP if probe - position < SKIP_NEAR else SKIP_STEP_FAR
            unrenderable.append([position, resume])
            emit("toc_unrenderable", start=position, resume=resume)
            if resume is None:
                break
            position = resume
            continue
        layout, toc_json, meta = got
        toc = toc if toc is not None else toc_json
        metadata = metadata if metadata is not None else meta
        ranges = [[p["startPositionId"], p["endPositionId"]] for p in (layout or [])]
        if not ranges or ranges[-1][1] + 1 <= position:
            break
        pages.extend(ranges)
        position = ranges[-1][1] + 1
        if metadata and position > metadata.get("lastPositionId", position):
            complete = True
            break
    return {
        "toc": toc,
        "metadata": metadata,
        "pages": pages,
        "unrenderable": unrenderable,
        "complete": complete,
    }


# ---------------------------------------------------------------------------
# PDF のしおりを書き換える
# ---------------------------------------------------------------------------


def write_outline(path, entries, *, verify=True):
    """PDF のしおりを entries で置き換える。ページの画像とテキスト層には触らない。

    **検証に通らなければ元のファイルを残す。** 蔵書を直接書き換えるので、
    ページ数・抜き取りページの描画結果・抽出文字数が変わっていないことを確かめてから
    差し替える（``core.text_layer.strip_file`` と同じ作法）。

    Returns:
        結果の dict。``ok`` が False なら ``error`` に理由が入り、元のファイルは手つかず。
    """
    import os
    import tempfile

    from pypdf import PdfReader, PdfWriter

    from core.text_layer import _sample_indexes, extracted_chars, render_digests

    result = {"path": path, "ok": False}
    try:
        reader = PdfReader(path)
        pages = len(reader.pages)
        del reader
        indexes = _sample_indexes(pages)
        before_text = extracted_chars(path, indexes) if verify else None
        before_render = render_digests(path, indexes) if verify else None

        writer = PdfWriter(clone_from=path)
        # 既存のしおりを捨てる
        root = writer._root_object
        if "/Outlines" in root:
            del root["/Outlines"]
        parents: dict[int, Any] = {}
        written = 0
        for entry in entries:
            if not 0 <= entry.page < pages:
                continue
            level = max(1, entry.level)
            # 親の無い深さに飛ばない（1 つ上の階層が無ければ詰める）
            while level > 1 and level - 1 not in parents:
                level -= 1
            parent = parents.get(level - 1) if level > 1 else None
            item = writer.add_outline_item(entry.title[:120], entry.page, parent=parent)
            parents[level] = item
            for deeper in [k for k in parents if k > level]:
                del parents[deeper]
            written += 1
        writer.page_mode = "/UseOutlines"

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
                if extracted_chars(created, indexes) != before_text:
                    raise ValueError("テキスト層が変わりました")
                if render_digests(created, indexes) != before_render:
                    raise ValueError("描画結果が変わりました")
            os.replace(created, path)
            tmp = None
            result.update(ok=True, pages=pages, bookmarks=written)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
