"""Kindle の本が持つ目次から、PDF のしおりを作る (#114)。

OCR の文字から章見出しを推測する方式（``core.chapter_detector``）は崩れやすい。
縦書きの中の横組み数字を読み違え、目次ページの各行をしおりにし、章番号と章題が
別ページの本では 1 章に 2 本付ける。蔵書の多くでしおりが崩れていた。

Kindle Cloud Reader の描画 API（``renderer/render``）の応答には、本の目次
（``toc.json``: 名前・階層・位置 ``tocPositionId``）と、各ページの位置範囲
（``layout.json``: ``startPositionId``〜``endPositionId``）が入っている。
**目次の位置を含むページ**にしおりを付ければ、名前も階層も本のとおりになる。

ページへの対応づけ（実測で決めた規則。#114 のコメントに目視の結果がある）:

- 描画のページと撮影したページはおおむね同じ並び。ただし位置の範囲にはすき間があり、
  **すき間にある目次の位置は次のページの始まり**を指している（目視 7/7、漫画 10/10。
  テキストで後ろへ補正した項目の 9 割がすき間の項目で、補正幅はほぼ +1）
- 撮影した PDF には余分なページ・欠けたページがあり、**ずれ幅は本の途中で変わる**
  （ハリー・ポッター全 7 巻: 第 1 巻 +2、第 2 巻 0、第 3 巻の途中から +2）。本全体で 1 つの
  ずれ幅を選ぶと、少ない側の区間が全部ずれる。そこで、各項目で章名がテキストに見つかる
  ずれ幅を出し、**続けて何項目も同じずれ幅で見つかるときだけ**ずれ幅を切り替える
  （``SWITCH_COST``。前へのずれ幅も選びうる）。1 項目だけの一致は目次ページ・柱・本文中の言及であることが多く、
  それで前へ動かすと誤る（初期の目視で前への補正 4 件がすべて誤りだった）
- 決めたずれ幅のページに章名が無ければ、後ろ ``SEARCH_AHEAD`` ページまで探す。
  すき間の項目で、次のページに無く 1 つ前のページにだけあれば 1 つ前にする（目視 6/8）
- **柱（ランニングヘッダ）に章名が出る本では、章名のあるページは章の始まりを指さない**
  （#122）。章の終わりまで毎ページ出るうえ、章扉は飾り文字でテキストが無いことがあり、
  テキストで決めると必ず後ろへずれる。章名が ``RUNNING_HEAD_RUN`` ページ以上続けて
  見つかる項目は、ずれ幅の判断にも後ろへの補正にも使わず、位置の示すページに付ける
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
# ずれ幅の候補を、描画と PDF のページ数の差の外側へ広げる幅。区間ごとのずれは差の範囲を
# 前後に数ページはみ出す（ハリー・ポッターは差 +2 で、区間のずれは 0〜+2 に加えすき間で +1）
SHIFT_MARGIN = 3
# ずれ幅を切り替える費用。章名が見つからない項目 1 件を 1 とする。2.5 なら、本の末尾まで続く
# 区間は 3 項目以上、途中の区間（入って戻るので 2 回分）は 6 項目以上、新しいずれ幅で見つかる
# ときだけ切り替わる。1〜2 項目の一致（目次ページ・柱・本文中の言及）では切り替えない。
# 先頭の項目も、既定のずれ幅（表紙の分）からの切り替えとして数える
SWITCH_COST = 2.5
# 章名で探すときに使う長さ。長すぎると OCR の読み違いで当たらず、短すぎると本文に当たる
PROBE_LEN = 10
MIN_PROBE_LEN = 2
# 柱（ランニングヘッダ）とみなす、章名が続けて出るページ数。柱のある本では章名が章の終わりまで
# 毎ページ出るので、「章名があるページ」は章の始まりを指さない。実測（図解・気象学入門
# B00GHHYQNM）では章扉にテキストが無く、柱だけが当たって付け先が 1 ページ後ろにずれた
RUNNING_HEAD_RUN = 3

CONFIRMED = "confirmed"  # 区間のずれ幅を足したページ（estimated）に章名があった
MOVED = "moved"  # estimated に無く、後ろ（すき間の項目は 1 つ前も）で章名が見つかったので動かした
UNCONFIRMED = "unconfirmed"  # テキストはあるが章名が見つからず、estimated のまま
UNSEARCHED = "unsearched"  # テキストが無い・章名が短いので探していない。位置だけで決めた
RUNNING_HEAD = "running_head"  # 章名が柱に出ていて場所を決められない。位置だけで決めた


@dataclass
class TocEntry:
    """しおり 1 件。page は PDF の 0 始まりのページ番号。"""

    level: int
    title: str
    position: int
    page: int = 0
    estimated: int = 0  # 位置のページ + 区間のずれ幅（前の項目より前にはしない）
    shift: int = 0  # 描画のページ番号（すき間は次のページ）から PDF のページへのずれ幅
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


def map_to_pages(entries, page_ranges, pdf_pages, page_text=None, offset=0):
    """各しおりに PDF のページを割り当てる（entries を書き換えて返す）。

    前提: ``positions_in_order(entries)`` が真で、``page_ranges`` が空でない。
    呼び出し側で確かめること（崩れていると全部のしおりが誤ったページに付く）。

    Args:
        entries: ``flatten_toc`` の結果
        page_ranges: 描画の各ページの ``[startPositionId, endPositionId]``（昇順）
        pdf_pages: PDF のページ数
        page_text: ``page_text(i)`` で i ページ目のテキストを返す関数。None ならテキストで補正しない
        offset: 既定のずれ幅（PDF の先頭に足した表紙の数）。テキストで区間ごとに変わりうる
    """
    if not page_ranges:
        raise ValueError("描画のページ範囲がありません")
    if not entries:
        return entries
    last = max(pdf_pages - 1, 0)
    starts = [r[0] for r in page_ranges]

    bases, gaps, probes = [], [], []
    for entry in entries:
        index = max(bisect.bisect_right(starts, entry.position) - 1, 0)
        gap = entry.position > page_ranges[index][1] and index + 1 < len(page_ranges)
        bases.append(index + 1 if gap else index)
        gaps.append(gap)
        probe = title_probe(entry.title)
        usable = page_text is not None and len(probe) >= MIN_PROBE_LEN
        probes.append(probe if usable else None)

    normalized: dict[int, str] = {}

    def found(k, page):
        if probes[k] is None or not 0 <= page <= last:
            return False
        if page not in normalized:
            normalized[page] = _norm(page_text(page))
        return probes[k] in normalized[page]

    diff = pdf_pages - len(page_ranges)
    shifts = list(
        range(min(diff, 0, offset) - SHIFT_MARGIN, max(diff, 0, offset) + SHIFT_MARGIN + 1)
    )

    # 柱かどうかは本の組版で決まることで、ずれ幅の候補の広さとは関係が無い。``shifts`` だけを
    # 見ると、``SHIFT_MARGIN`` を変えたり表紙の無い本になったりしただけで柱を見落とす。
    # 後ろへの補正の範囲（``SEARCH_AHEAD``）+ 柱とみなす連なりの分は必ず見る（連続した範囲）
    head_window = range(min(shifts[0], -1), max(shifts[-1], SEARCH_AHEAD + RUNNING_HEAD_RUN) + 1)

    def running_head(k):
        """章名が**続けて** ``RUNNING_HEAD_RUN`` ページ以上出るなら、どのページも章の始まりを
        指していない (#122)。

        柱のある本では章名が章の終わりまで毎ページ出る。章扉にテキストが無いと、当たるのは
        柱だけになり、テキストで決めると必ず後ろへずれる。そういう項目はテキストを使わず、
        位置の示すページに付ける。飛び飛びの一致（目次ページ・本文中の言及）は柱ではない。
        """
        run = 0
        for s in head_window:
            run = run + 1 if found(k, bases[k] + s) else 0
            if run >= RUNNING_HEAD_RUN:
                return True
        return False

    heads = [probes[k] is not None and running_head(k) for k in range(len(entries))]
    # 柱の項目はどのずれ幅でも「見つからない」ので、その項目自身のずれ幅はテキストで選ばれない。
    # ただし**正の根拠を捨てる**ので、区間の境目の置き場所が変わり、隣の項目が動くことはある
    shift = _choose_shifts(
        len(entries), shifts, offset, lambda k, s: not heads[k] and found(k, bases[k] + s)
    )

    prev = 0
    for k, entry in enumerate(entries):
        # 補正の結果、位置の順では後ろの項目が前に来ることがある。そのときだけ前の項目に揃える
        estimated = min(max(bases[k] + shift[k], prev), last)
        entry.estimated = estimated
        entry.shift = shift[k]
        entry.page = estimated
        if probes[k] is None:
            entry.how = UNSEARCHED
        elif heads[k]:
            entry.how = RUNNING_HEAD
        elif found(k, estimated):
            entry.how = CONFIRMED
        else:
            entry.how = UNCONFIRMED
            for p in range(estimated + 1, min(estimated + SEARCH_AHEAD, last) + 1):
                if found(k, p):
                    entry.page, entry.how = p, MOVED
                    break
            else:
                if gaps[k] and estimated - 1 >= prev and found(k, estimated - 1):
                    entry.page, entry.how = estimated - 1, MOVED
        prev = entry.page
    return entries


def _choose_shifts(count, shifts, default, matches):
    """項目ごとのずれ幅を、区間ごとに一定になるように選ぶ（動的計画法）。

    費用は「章名が見つからない項目の数」+「ずれ幅を切り替えた回数 × SWITCH_COST」。
    同じ費用なら既定のずれ幅に近いほう、その次に後ろ（前への補正は誤りが多かった）。

    **章名が 1 つも見つからない区間は作らない** (#122)。区間を分けるのはテキストが
    そう言っているときだけで、証拠の無い項目は隣の区間に付く。これが無いと、本の先頭や
    区間の境目にある「目次」「表紙」のような項目だけが既定のずれ幅に取り残される
    （実測: われわれはなぜ嘘つきで…、コンピュータの構成と設計、ハリー・ポッター）。
    ただし**本全体が 1 つの区間なら証拠は要らない**。テキストが無ければ全項目が既定の
    ずれ幅になり、1〜2 項目の一致では区間を分けない（``SWITCH_COST``）という性質も変わらない。
    """
    tie = 1e-4  # 同点を決めるだけの大きさ。候補の幅（数十）を掛けても 1 に届かない
    inf = float("inf")
    n = len(shifts)

    def bias(s):
        return tie * (2 * abs(s - default) + (1 if s < default else 0))

    hit = [[bool(matches(k, s)) for s in shifts] for k in range(count)]
    local = [
        [(0.0 if hit[k][j] else 1.0) + bias(s) for j, s in enumerate(shifts)] for k in range(count)
    ]

    # 状態は（ずれ幅, その区間で章名が見つかったか）。見つかっていない区間からは切り替えられない
    seen_no, seen_yes = [inf] * n, [inf] * n
    for j, s in enumerate(shifts):
        start = local[0][j] + (0.0 if s == default else SWITCH_COST)
        (seen_yes if hit[0][j] else seen_no)[j] = start
    back: list[list[tuple[int, int]]] = []
    for k in range(1, count):
        best_j = min(range(n), key=lambda j: seen_yes[j])
        move = seen_yes[best_j] + SWITCH_COST
        next_no, next_yes = [inf] * n, [inf] * n
        # 届かない状態の値は使わない（経路をたどるのは、費用が有限だった状態だけ）
        row: list[tuple[int, int]] = [(0, 0)] * (2 * n)
        for j in range(n):
            if hit[k][j]:
                # 見つかったので、どこから来てもその区間は「見つかった」になる。同点は留まるほう
                for cost, prev in (
                    (seen_yes[j], (1, j)),
                    (seen_no[j], (0, j)),
                    (move, (1, best_j)),
                ):
                    if cost < next_yes[j]:
                        next_yes[j], row[n + j] = cost, prev
                next_yes[j] += local[k][j]
            else:
                if seen_yes[j] < inf:  # 見つかった区間に留まる
                    next_yes[j], row[n + j] = seen_yes[j] + local[k][j], (1, j)
                for cost, prev in ((seen_no[j], (0, j)), (move, (1, best_j))):
                    if cost < next_no[j]:
                        next_no[j], row[j] = cost, prev
                next_no[j] += local[k][j]
        back.append(row)
        seen_no, seen_yes = next_no, next_yes

    # 区間が 2 つ以上あるなら、どの区間にも章名が見つかっていること（= 最後が seen_yes）
    j = min(range(n), key=lambda x: seen_yes[x])
    plain = sum(local[k][shifts.index(default)] for k in range(count))  # 本全体で 1 区間
    if seen_yes[j] >= inf or plain <= seen_yes[j]:
        return [default] * count
    chosen, seen = [0] * count, 1
    for k in range(count - 1, -1, -1):
        chosen[k] = shifts[j]
        if k > 0:
            seen, j = back[k - 1][seen * n + j]
    return chosen


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
