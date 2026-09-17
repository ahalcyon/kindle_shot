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

CONFIRMED = "confirmed"  # 位置から出したページに章名があった
MOVED = "moved"  # テキストで後ろへ補正した
UNCONFIRMED = "unconfirmed"  # テキストはあるが章名が見つからず、位置から出したページのまま
UNSEARCHED = "unsearched"  # テキストが無い・章名が短いので探していない。位置だけで決めた


@dataclass
class TocEntry:
    """しおり 1 件。page は PDF の 0 始まりのページ番号。"""

    level: int
    title: str
    position: int
    page: int = 0
    estimated: int = 0
    how: str = UNSEARCHED


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


def order_outliers(entries) -> list[int]:
    """目次の順に位置が並ぶ最長の列から外れる項目の番号を返す。

    実測では、並びの崩れた目次は「目次」「Cover」のような 1 項目だけが本の別の場所
    （末尾など）を指していた。その項目だけ外せば残りは順に並ぶ。
    """
    import bisect as _bisect

    positions = [e.position for e in entries]
    tails: list[int] = []  # 長さ k+1 の列の末尾の位置が最小になる項目の番号
    prev: list[int | None] = [None] * len(positions)
    for i, pos in enumerate(positions):
        k = _bisect.bisect_right([positions[t] for t in tails], pos)
        if k > 0:
            prev[i] = tails[k - 1]
        if k == len(tails):
            tails.append(i)
        else:
            tails[k] = i
    keep = set()
    cursor: int | None = tails[-1] if tails else None
    while cursor is not None:
        keep.add(cursor)
        cursor = prev[cursor]
    return [i for i in range(len(positions)) if i not in keep]


def positions_in_order(entries) -> bool:
    """目次の位置が、出てくる順に減っていないか。

    減っている目次は対応づけの前提（目次の順＝ページの順）が崩れているので、
    しおりを付けずに要確認にする。
    """
    return all(a.position <= b.position for a, b in zip(entries, entries[1:], strict=False))


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

    前提: ``positions_in_order(entries)`` が真で、``page_starts`` が空でない。
    呼び出し側で確かめること（崩れていると全部のしおりが誤ったページに付く）。

    Args:
        entries: ``flatten_toc`` の結果
        page_starts: 描画の各ページの ``startPositionId``（昇順）
        pdf_pages: PDF のページ数
        page_text: ``page_text(i)`` で i ページ目のテキストを返す関数。None ならテキストで補正しない
        offset: PDF の先頭に足したページ数（表紙）
    """
    if not page_starts:
        raise ValueError("描画のページ範囲がありません")
    last = max(pdf_pages - 1, 0)
    prev = 0
    for entry in entries:
        index = max(bisect.bisect_right(page_starts, entry.position) - 1, 0)
        estimated = min(index + offset, last)
        # 前の項目をテキストで後ろへ補正した結果、位置の順では後ろの項目が前に来ることがある。
        # そのときだけ前の項目に揃える（位置の順は呼び出し側で確かめてある）
        estimated = max(estimated, prev)
        entry.estimated = estimated
        entry.page = estimated
        entry.how = UNSEARCHED
        probe = title_probe(entry.title)
        if page_text is not None and len(probe) >= MIN_PROBE_LEN:
            found = None
            for p in range(estimated, min(estimated + SEARCH_AHEAD, last) + 1):
                if probe in _norm(page_text(p)):
                    found = p
                    break
            if found is None:
                entry.how = UNCONFIRMED
            elif found == estimated:
                entry.how = CONFIRMED
            else:
                entry.page = found
                entry.how = MOVED
        prev = entry.page
    return entries


# ---------------------------------------------------------------------------
# 描画 API から目次とページの位置範囲を取る
# ---------------------------------------------------------------------------

# 1 回の要求で描かせるページ数。多いと描けないページが混ざったときに要求ごと落ちる (#104)
SCAN_NUM_PAGES = 4
# 描けない区間の先を探すときの刻み。実測の区間は数十位置（#104）。近くは細かく、遠くは粗く
SKIP_STEP = 25
SKIP_STEP_FAR = 500
SKIP_NEAR = 2000
# 描けない区間 1 つを探す要求の上限。25 刻みで 2000 位置（80 回）+ 500 刻みで 10 万位置（200 回）。
# 合本 1 冊（300 万位置）の 3% を超える区間は、探すより要確認にして人が見るほうがよい
SKIP_MAX_REQUESTS = 280
# 1 冊の要求の上限。最大の本（ハリー・ポッター全 7 巻 2,657 ページ）で 4 ページずつ約 670 回だった。
# その 10 倍を超えたら何かがおかしいので打ち切る
BOOK_MAX_REQUESTS = 7000
# 通信の一時的な失敗・5xx のやり直し回数と間隔（ミリ秒）
FETCH_RETRIES = 4
SERVER_ERROR_RETRIES = 2
RETRY_WAIT_MS = 5000


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


def scan_positions(get, *, emit=None):
    """``get(num_pages, position)`` を順に呼んで、目次と全ページの位置範囲を集める。

    ``get`` は描けたら ``(layout, toc, metadata)`` を、描けなければ None を返す。
    通信の失敗は ``get`` の中でやり直し、続けて失敗したら例外にすること。

    Returns:
        {"toc", "metadata", "pages": [[start, end], ...], "unrenderable": [[from, resume], ...],
         "complete": 本の最後の位置まで取れたか}
    """
    emit = emit or (lambda *a, **k: None)
    calls = [0]

    def call(num_pages, position):
        calls[0] += 1
        if calls[0] > BOOK_MAX_REQUESTS:
            raise RuntimeError(f"描画 API の要求が上限（{BOOK_MAX_REQUESTS} 回）を超えました")
        return get(num_pages, position)

    pages: list[list[int]] = []
    unrenderable: list[list[int | None]] = []
    toc = metadata = None
    position = 0
    complete = False

    def last_position():
        return metadata.get("lastPositionId") if isinstance(metadata, dict) else None

    def first_start(got):
        layout = got[0] if got else None
        return layout[0]["startPositionId"] if layout else None

    while True:
        last = last_position()
        if last is not None and position > last:
            complete = True
            break
        got = call(SCAN_NUM_PAGES, position) or call(1, position)
        if got is None:
            # 描けない。先へ探して、描ける位置から続ける
            failed, resume, tries, probe = position, None, 0, position + SKIP_STEP
            while tries < SKIP_MAX_REQUESTS and (last is None or probe <= last):
                tries += 1
                start = first_start(call(1, probe))
                if start is not None and start > position:
                    resume = start
                    break
                failed = probe
                probe += SKIP_STEP if probe - position < SKIP_NEAR else SKIP_STEP_FAR
            if resume is not None:
                # 見つかったのは探した位置を含むページ。失敗した位置との間に描けるページが
                # あれば取りこぼすので、二分探索で最初に描けるページまで戻す
                lo, hi = failed, resume
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    start = first_start(call(1, mid))
                    if start is not None and start > position:
                        resume = min(resume, start)
                        hi = mid
                    else:
                        lo = mid
            unrenderable.append([position, resume])
            emit("toc_unrenderable", start=position, resume=resume)
            if resume is None:
                break
            position = resume
            continue
        layout, toc_json, meta = got
        toc = toc if toc is not None else toc_json
        metadata = metadata if metadata is not None else meta
        # 前回までに取ったページがもう一度返ってきたら捨てる。隣り合うページは位置の範囲が
        # 少し重なることがある（実測: [483330, 484702] の次が [484697, 485937]）ので、
        # 「要求した位置より前から始まる」では判定しない。開始位置が前に取ったページ以下なら重複
        ranges: list[list[int]] = []
        for p in layout or []:
            last_start = ranges[-1][0] if ranges else (pages[-1][0] if pages else -1)
            if p["startPositionId"] > last_start:
                ranges.append([p["startPositionId"], p["endPositionId"]])
        if not ranges:
            break
        pages.extend(ranges)
        position = max(position, ranges[-1][1] + 1)
    return {
        "toc": toc,
        "metadata": metadata,
        "pages": pages,
        "unrenderable": unrenderable,
        "complete": complete,
    }


def fetch_book_structure(page, *, emit=None, wait_ms=15000):
    """開いている本について、目次と全ページの位置範囲を描画 API から取る。

    **読書位置は動かさない。** リーダーのページ送りはせず、リーダーが最初に出した
    描画要求の URL と認証ヘッダーを使って、位置を指定した要求を順に送る。
    描けない区間（#104）は先の位置を探して飛ばし、``unrenderable`` に残す。
    """
    requests: list[Any] = []

    def on_request(request):
        if "/renderer/render" in request.url:
            requests.append(request)

    def wait_for_request():
        waited = 0
        while not requests and waited < wait_ms:
            page.wait_for_timeout(500)
            waited += 500

    page.on("request", on_request)
    try:
        page.reload(wait_until="domcontentloaded")
        wait_for_request()
        if not requests:
            raise RuntimeError("描画要求が出ませんでした（本を開けていない可能性）")
        first = requests[0].url
        for key in ("numPage", "startingPosition", "skipPageCount"):
            if not re.search(rf"[?&]{key}=", first):
                # 置き換えられないまま送ると、どの要求も読書位置のページを返す
                raise RuntimeError(f"描画要求の形が想定と違います（{key} がありません）")
        state = {
            "url": _sub_query(first, skipPageCount=0),
            "headers": {k: v for k, v in requests[0].headers.items() if not k.startswith(":")},
        }

        def refresh():
            requests.clear()
            page.reload(wait_until="domcontentloaded")
            wait_for_request()
            if requests:
                state["url"] = _sub_query(requests[0].url, skipPageCount=0)
                state["headers"] = {
                    k: v for k, v in requests[0].headers.items() if not k.startswith(":")
                }

        def get(num_pages, position):
            server_errors = 0
            for _ in range(FETCH_RETRIES + SERVER_ERROR_RETRIES):
                try:
                    resp = page.request.get(
                        _sub_query(state["url"], numPage=num_pages, startingPosition=position),
                        headers=state["headers"],
                        timeout=90000,
                    )
                except Exception:  # noqa: BLE001 - 通信の一時的な失敗はやり直す
                    page.wait_for_timeout(RETRY_WAIT_MS)
                    continue
                if resp.status in (401, 403):
                    refresh()
                    continue
                if resp.status >= 500:
                    # 描けないページでも一時的な失敗でも 500 が返る。数回やり直してから決める
                    server_errors += 1
                    if server_errors > SERVER_ERROR_RETRIES:
                        return None
                    page.wait_for_timeout(RETRY_WAIT_MS)
                    continue
                if resp.status != 200:
                    page.wait_for_timeout(RETRY_WAIT_MS)
                    continue
                return _untar(resp.body())
            raise RuntimeError(f"描画 API の要求に失敗し続けました（位置 {position}）")

        return scan_positions(get, emit=emit)
    finally:
        page.remove_listener("request", on_request)


# ---------------------------------------------------------------------------
# PDF のしおりを書き換える
# ---------------------------------------------------------------------------

# しおりの名前の長さの上限。ビューアで切り詰められるので、長すぎるものはここで切る
TITLE_MAX = 120


def _outline_pages(reader):
    out = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                out.append(reader.get_destination_page_number(item))

    walk(reader.outline)
    return out


def write_outline(path, entries, *, verify=True):
    """PDF のしおりを entries で置き換える。ページの画像とテキスト層には触らない。

    **検証に通らなければ元のファイルを残す。** 蔵書を直接書き換えるので、
    ページ数・抜き取りページの描画結果・抽出文字数が変わっていないこと、
    書いたしおりを読み戻して件数とページが一致することを確かめてから差し替える
    （``core.text_layer.strip_file`` と同じ作法）。書けるしおりが 1 件も無ければ書き換えない。

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
        usable = [e for e in entries if 0 <= e.page < pages]
        if not usable:
            raise ValueError("書けるしおりがありません")
        indexes = _sample_indexes(pages)
        before_text = extracted_chars(path, indexes) if verify else None
        before_render = render_digests(path, indexes) if verify else None

        writer = PdfWriter(clone_from=path)
        # 既存のしおりを捨てる
        root = writer._root_object
        if "/Outlines" in root:
            del root["/Outlines"]
        parents: dict[int, Any] = {}
        expected_pages = []
        for entry in usable:
            level = max(1, entry.level)
            # 親の無い深さに飛ばない（1 つ上の階層が無ければ詰める）
            while level > 1 and level - 1 not in parents:
                level -= 1
            parent = parents.get(level - 1) if level > 1 else None
            item = writer.add_outline_item(entry.title[:TITLE_MAX], entry.page, parent=parent)
            parents[level] = item
            for deeper in [k for k in parents if k > level]:
                del parents[deeper]
            expected_pages.append(entry.page)
        writer.page_mode = "/UseOutlines"
        # 捨てたしおりのオブジェクトを残さない
        writer.compress_identical_objects(remove_identicals=False, remove_orphans=True)

        folder = os.path.dirname(path) or "."
        fd, created = tempfile.mkstemp(prefix=".bookmarks-", suffix=".tmp", dir=folder)
        os.close(fd)
        tmp: str | None = created
        try:
            with open(created, "wb") as f:
                writer.write(f)
            if verify:
                after = PdfReader(created)
                if len(after.pages) != pages:
                    raise ValueError(f"ページ数が変わりました: {pages} -> {len(after.pages)}")
                written = _outline_pages(after)
                if written != expected_pages:
                    raise ValueError("書いたしおりを読み戻すと件数かページが違います")
                del after
                if extracted_chars(created, indexes) != before_text:
                    raise ValueError("テキスト層が変わりました")
                if render_digests(created, indexes) != before_render:
                    raise ValueError("描画結果が変わりました")
            try:
                os.replace(created, path)
            except PermissionError as exc:
                raise PermissionError(f"{exc}（PDF を開いているなら閉じて再実行）") from exc
            tmp = None
            result.update(ok=True, pages=pages, bookmarks=len(expected_pages))
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    except Exception as exc:  # noqa: BLE001 - 1 冊の失敗で一括処理を止めない
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
