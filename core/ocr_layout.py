"""OCR のレイアウト情報（行ごとの座標・縦横・信頼度）

NDLOCR-Lite は行ごとに ``boundingBox`` / ``isVertical`` / ``confidence`` を
返すが、テキストだけを連結すると全部失われる。検索可能 PDF の不可視テキストを
文字の位置に重ねるには座標が要る（重ねないと、Ctrl+F は通っても範囲選択コピー
が見えている場所と対応せず、縦書きでは完全に破綻する）。

JSON の ``class_index`` は実測ではテキスト行が一律 1 で、本文 / 図版 / 柱 /
ノンブルといった細かい種別は XML の ``LINE TYPE`` 側にしか無い。種別が要る
用途（読み取り品質の検証・図版の分離）では XML を読む必要がある。
"""

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Line:
    """OCR が読んだ 1 行。座標は画像のピクセル、原点は左上。"""

    text: str
    left: int
    top: int
    right: int
    bottom: int
    vertical: bool = False
    confidence: float = 0.0

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    @property
    def font_size(self):
        """1 文字の一辺。縦書きは行の幅、横書きは行の高さ。"""
        return self.width if self.vertical else self.height

    def char_positions(self):
        """文字ごとの (文字, left, top) を、行の先頭から順に返す。

        NDLOCR-Lite は行単位の bbox しか返さないので、行の長辺を文字数で
        等分して割り当てる。実測では 1 文字あたりの送りが 19.8〜20.1px と
        ほぼ一定なので、この近似で選択位置は実用上ずれない。
        """
        n = len(self.text)
        if n == 0:
            return
        if self.vertical:
            step = self.height / n
            for i, ch in enumerate(self.text):
                yield ch, self.left, self.top + step * i
        else:
            step = self.width / n
            for i, ch in enumerate(self.text):
                yield ch, self.left + step * i, self.top


@dataclass(frozen=True)
class PageLayout:
    """1 ページ分の OCR 結果。

    座標が取れなかったページ（ページ毎起動のフォールバック、JSON が読めない）
    では lines が空になり、テキストだけを fallback_text に持つ。呼び出し側は
    ``lines`` の有無で「文字の位置に重ねられるか」を判断する。
    """

    filename: str
    width: int = 0
    height: int = 0
    lines: list[Line] = field(default_factory=list)
    fallback_text: str = ""

    @property
    def positioned(self):
        """文字の位置が分かっているか。"""
        return bool(self.lines)

    @property
    def text(self):
        """行を読み順に連結したテキスト。従来の (filename, text) 相当。"""
        if not self.lines:
            return self.fallback_text
        return "\n".join(line.text for line in self.lines)

    def as_pair(self):
        return (self.filename, self.text)


def map_text(layout, fn):
    """各行のテキストに fn を適用した PageLayout を返す（置換辞書用）。"""
    if not layout.lines:
        return replace(layout, fallback_text=fn(layout.fallback_text))
    return replace(layout, lines=[replace(line, text=fn(line.text)) for line in layout.lines])


def _bbox(points):
    """[[x, y], ...] を (left, top, right, bottom) にする。"""
    xs = [int(p[0]) for p in points]
    ys = [int(p[1]) for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def parse_ndl_json(data, filename):
    """NDLOCR-Lite の JSON を PageLayout にする。

    形は {"contents": [[{...行...}, ...], ...], "imginfo": {...}}。
    テキスト行 (isTextline == "true") だけを、返ってきた順（読み順）で拾う。
    """
    info = data.get("imginfo") or {}
    lines = []
    for page in data.get("contents") or []:
        for item in page or []:
            if item.get("isTextline") != "true":
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            box = item.get("boundingBox")
            if not box:
                continue
            left, top, right, bottom = _bbox(box)
            lines.append(
                Line(
                    text=text,
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    vertical=item.get("isVertical") == "true",
                    confidence=float(item.get("confidence") or 0.0),
                )
            )
    return PageLayout(
        filename=filename,
        width=int(info.get("img_width") or 0),
        height=int(info.get("img_height") or 0),
        lines=lines,
    )
