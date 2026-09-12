"""本の表紙を Amazon の商品ページから取って、1 ページ目にする (#27)。

**リーダーのページ送りには表紙が入っていない。** 撮れる 1 ページ目は扉
（タイトルページ）で、表紙とは別物（実測: 本物の表紙にある「光文社」「株式会社
コンカー 代表取締役社長」が、撮れた 1 ページ目には無い）。

表紙の入手先は 2 つあり、解像度が桁違いに違う。

    ライブラリの img[id="cover-<ASIN>"]      340x500 が上限
    商品ページの data-old-hires              1021x1500（実測）

ページの枠は 1600x1200 なので、前者は 3.8 倍に引き伸ばすことになり**ぼやける**。
後者なら縮小して収まるので劣化しない。だから商品ページから取る。

**引き伸ばしはしない。** 元画像が枠より小さい本では、拡大せず等倍で中央に置く。
ぼやけた表紙を足すのは、表紙が無いより悪い。
"""

import io
import re
import urllib.request

from PIL import Image

PRODUCT_URL = "https://www.amazon.co.jp/dp/{asin}"

# 商品ページを普通のブラウザとして取りに行く。User-Agent が無いと弾かれる
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30

# 主画像の高解像度版。Amazon は小さい画像の src とは別に、この属性で大きいほうを
# 指している。**画像 ID が違う**ので、src の URL のサイズ修飾子を書き換えても
# 大きくならない（実測: 31kuSxEiAzL は ._SL2000_ でも 340x500 のまま）
_HIRES_RE = re.compile(r'data-old-hires="(https://[^"]+?\.jpg)"')
# 予備。URL -> [高さ, 幅] の対応表から一番大きいものを採る
_DYNAMIC_RE = re.compile(r'data-a-dynamic-image="([^"]+)"')
_ENTRY_RE = re.compile(r'(https://[^"&]+?\.jpg)[^\[]*\[\s*(\d+)\s*,\s*(\d+)\s*\]')


def cover_url_from_html(html):
    """商品ページの HTML から、一番大きい表紙画像の URL を返す。無ければ None。"""
    found = _HIRES_RE.search(html)
    if found:
        return found.group(1)
    best = None
    best_area = 0
    for block in _DYNAMIC_RE.findall(html):
        for url, a, b in _ENTRY_RE.findall(block.replace("&quot;", '"')):
            area = int(a) * int(b)
            if area > best_area:
                best, best_area = url, area
    return best


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def fetch_cover(asin, *, get=_get):
    """ASIN の表紙画像のバイト列を返す。取れなければ None。

    取れないことは失敗ではない。表紙が無いまま本文だけの PDF になるだけで、
    本の中身は 1 ページも欠けない。
    """
    if not asin:
        return None
    try:
        html = _get_text(get(PRODUCT_URL.format(asin=asin)))
        url = cover_url_from_html(html)
        if not url:
            return None
        return get(url)
    except Exception:  # noqa: BLE001 - 取れなければ表紙なしで続ける
        return None


def _get_text(data):
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


def fit_cover(data, size, background=(255, 255, 255)):
    """表紙を枠に収めた PNG のバイト列を返す。収まらなければ縮小する。

    **拡大はしない。** 元画像が枠より小さければ等倍で中央に置く。引き伸ばした
    表紙は、本文ページが等倍で入っている PDF の中で 1 ページだけぼやける。
    """
    width, height = size
    with Image.open(io.BytesIO(data)) as image:
        cover = image.convert("RGB")
        # 1.0 で頭打ちにするのが「拡大しない」の実体。ここを外すと小さい表紙が
        # 引き伸ばされる
        scale = min(width / cover.width, height / cover.height, 1.0)
        new_size = (max(1, round(cover.width * scale)), max(1, round(cover.height * scale)))
        if new_size != cover.size:
            cover = cover.resize(new_size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), background)
        canvas.paste(cover, ((width - cover.width) // 2, (height - cover.height) // 2))
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        return buffer.getvalue()
