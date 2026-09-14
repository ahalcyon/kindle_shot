"""本ごとに image_pdf / searchable_pdf を決める

**全冊を一律の形式で作らない**（AGENTS.md「本ごとに形式を分ける」）。
漫画・図版主体の本に OCR をかけると、吹き出しの縦書きを誤読したテキスト層が
載る。この出力の最優先は「コピーした文字列が原文どおりか」なので、
**誤ったテキスト層はテキスト層が無いより悪い**。

判定の基準は「OCR が意味のある文字列を出すか」で、実測できる。
手元の 246 冊では **1 ページあたりの OCR 文字数がきれいに 2 つに割れた** (#89):

    漫画側     4.6 〜  110.3 文字/ページ  153 冊  空ページ率 中央値 0.59
    文章側   249.2 〜 2723.2 文字/ページ   93 冊  空ページ率 中央値 0.01

110 と 249 の間には 1 冊も無い。既定の閾値 150 はこの谷の真ん中。
旧蔵書のテキスト層の有無という独立した根拠と突き合わせると、236 冊中 229 冊で
一致した。食い違う 7 冊はどれも境界の本（分冊版の漫画エッセイ、データブック）。

## 間違え方は対称ではない

- **漫画と読み違えて image_pdf にした** → テキスト層が無い本が出来る。
  直すには撮り直すしかない（1 冊 10 分）。**高い**
- **文章と読み違えて searchable_pdf にした** → OCR の時間を捨てる。
  出来た PDF は `core.text_layer` で剥がせば image_pdf と同じものになる
  （1 冊 15 秒、画像は 1 ピクセルも変わらない）。**安い**

したがって**確信があるときだけ image_pdf に倒し、迷ったら searchable_pdf**。
その方針で `decide` は根拠を次の順に見る。

1. 実測。同じ本を OCR 済みなら文字数/ページで決まる（最強）
2. 実測のシリーズ一致。巻数と出版社レーベルを落とした名前が一致し、
   その系列の判定が割れていないとき（`てーきゅう 3` から `12` へ）
3. ジャンル分けが「コミック」
4. 旧蔵書に同名の PDF があり、テキスト層が空のとき
5. どれにも当たらなければ searchable_pdf
"""

import collections
import json
import os
import re

from core.safe_names import book_path_name

IMAGE = "image_pdf"
SEARCHABLE = "searchable_pdf"

# 谷の真ん中。実測の漫画側の最大が 110.3、文章側の最小が 249.2
DEFAULT_THRESHOLD = 150.0

# 系列を根拠にするのに要る実測の冊数。1 冊では series_key の衝突と区別できない
MIN_SERIES_BOOKS = 2

# シリーズ名を作るときに落とすもの。巻数・レーベル・版の表記が違うだけの本を
# 同じ系列として扱う
_BRACKETS = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")
_DIGITS = re.compile(r"[0-9０-９]+")
_PUNCT = re.compile(r"[\s　・:：〜～\-–—,、。!！?？…]")
_VOLUME_WORDS = re.compile(r"(巻|話|第|分冊版|合本版|愛蔵版|新装版|新版|改訂版|上|中|下)")


def series_key(title):
    """巻数・レーベル・版の違いを落とした系列名。"""
    s = _BRACKETS.sub("", title)
    s = _DIGITS.sub("", s)
    s = _VOLUME_WORDS.sub("", s)
    return _PUNCT.sub("", s)


def load_books(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict) and "books" in data:
        data = data["books"]
    return data


def measured_labels(paths, threshold):
    """バッチログの ocr_validation から {タイトル: 形式} を作る。

    book_start と ocr_validation を順に対応させる（1 冊につき 1 組）。
    """
    labels = {}
    values = {}
    for path in paths:
        title = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                event = d.get("event")
                if event == "book_start":
                    title = d.get("title")
                elif event == "ocr_validation" and title:
                    pages = d.get("pages") or 0
                    if pages:
                        cpp = (d.get("chars") or 0) / pages
                        values[title] = cpp
                        labels[title] = IMAGE if cpp < threshold else SEARCHABLE
                    title = None
    return labels, values


def book_pdf_path(folder, title):
    """蔵書フォルダの中で、その本の PDF があるべきパスを返す。

    **ファイル名からタイトルを逆引きしない (#95)。** 蔵書の名前は
    `book_path_name` を通っていて、出力先が深いと長いタイトルは
    `_<8桁hash>` で切り詰められる。ハッシュは元のタイトルから作るので
    逆引きできず、素朴に `ファイル名[:-4] == title` で比べると
    切り詰められた本が黙って外れる（剥がし漏れ／完成済みの撮り直し）。
    """
    return os.path.join(folder, book_path_name(title, folder) + ".pdf")


def text_layer_format(path):
    """PDF のテキスト層の有無から形式を推定する。読めなければ None。

    **「文字が無い」と「読めなかった」を混ぜない。** 中断したバッチの
    書きかけが蔵書フォルダに残ることがあり、それを「テキスト層なし」の
    根拠にすると、その本が撮り直しの要る image_pdf 側（高いほうの
    間違い）へ倒れる。
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        n = len(reader.pages)
        picks = [k for k in (0, n // 4, n // 2, (3 * n) // 4, n - 1) if 0 <= k < n]
        if not picks:
            return None
        chars = sum(len((reader.pages[k].extract_text() or "").strip()) for k in picks)
    except Exception:  # noqa: BLE001 - 読めない本は判定材料にしないだけ
        return None
    return IMAGE if chars == 0 else SEARCHABLE


def library_labels(folder, titles):
    """旧蔵書のテキスト層の有無から {タイトル: 形式} を作る。

    titles から**正引き**で探す。フォルダを走査してファイル名を
    タイトル扱いすると、切り詰められた本を取り違える (#95)。
    """
    labels = {}
    for title in titles:
        path = book_pdf_path(folder, title)
        if not os.path.isfile(path):
            continue
        fmt = text_layer_format(path)
        if fmt is not None:
            labels[title] = fmt
    return labels


def genre_labels(path):
    """buckets.json（ジャンル分け）から {タイトル: ジャンル}。"""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    out = {}
    for genre, items in data.items():
        for item in items:
            if isinstance(item, dict) and item.get("title"):
                out[item["title"]] = genre
    return out


def series_labels(labels):
    """系列名ごとの判定。割れている系列は使わない（Noneにする）。"""
    counts: dict = collections.defaultdict(collections.Counter)
    for title, fmt in labels.items():
        counts[series_key(title)][fmt] += 1
    # **1 冊だけの系列は根拠にしない。** len(c) == 1 は「判定が 1 種類」であって
    # 「複数冊が一致した」ではない。series_key は全角含む数字・括弧内・
    # 巻/話/第/上/中/下 などを落とすので 1 文字違いで衝突する
    # （「下町ロケット」と「町ロケット」、「空の中」と「空の上」が同じキーになる）。
    # 割れの検出は実測済みのタイトル同士でしか効かないため、未実測の本が
    # 衝突して入ってくる経路は素通りする。しかもこれは撮り直しの要る高い側の
    # 間違い。2 冊以上が一致したときだけ採るのが安い保険になる。
    return {
        k: (next(iter(c)) if len(c) == 1 and sum(c.values()) >= MIN_SERIES_BOOKS else None)
        for k, c in counts.items()
    }


def decide(title, *, measured, series, genre, library):
    """1 冊の形式と、その根拠を返す。"""
    if title in measured:
        return measured[title], "measured"
    key = series_key(title)
    if series.get(key):
        return series[key], "series"
    if genre.get(title) == "コミック":
        return IMAGE, "genre-comic"
    if library.get(title) == IMAGE:
        return IMAGE, "library-image"
    # 迷ったら searchable。読み違えても後から剥がせる
    return SEARCHABLE, "default"
