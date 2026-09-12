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

# 普通のブラウザとして取りに行く。既定の User-Agent (Python-urllib) でも 200 が
# 返ることは確認したが、弾かれる相手もありうるので予防として送る
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 1 回のソケット操作の上限。無人で 342 冊回すので、上限の無い待ちは置かない。
# 30 秒は「商品ページが普通に取れる時間（実測 3 秒）の 10 倍」。応答が細切れに
# 届く相手には総時間の上限にならない点は承知のうえ
REQUEST_TIMEOUT = 30

# 主画像の高解像度版。Amazon は小さい画像の src とは別に、この属性で大きいほうを
# 指している。**画像 ID が違う**ので、src の URL のサイズ修飾子を書き換えても
# 大きくならない（実測: 31kuSxEiAzL は ._SL2000_ でも 340x500 のまま）
_HIRES_RE = re.compile(r'data-old-hires="(https://[^"]+?\.jpg)"')
# 主画像の要素が持つ「URL -> [高さ, 幅]」の対応表。**この要素の中だけ**を見る
_LANDING_RE = re.compile(
    r'id="landingImage"[^>]*?data-a-dynamic-image="([^"]+)"'
    r'|data-a-dynamic-image="([^"]+)"[^>]*?id="landingImage"'
)
_ENTRY_RE = re.compile(r'(https://[^"&]+?\.jpg)[^\[]*\[\s*(\d+)\s*,\s*(\d+)\s*\]')


def cover_url_from_html(html):
    """商品ページの HTML から表紙画像の URL を返す。無ければ None。

    まず data-old-hires。無ければ**主画像 (landingImage) の中だけ**で一番大きい
    ものを採る。

    **ページ全体から面積最大を採ってはいけない。** 商品ページにはおすすめ商品の
    ぶんも含めて data-a-dynamic-image が 7 個あり（実測）、うち 6 個は他商品の
    420x420 だった。この本の主画像が 522x355 しかない本では他商品が勝つ。
    黙って別の本の表紙が 1 ページ目に入るくらいなら、表紙なしのほうがよい。

    主画像に限れば安全で、実測で取りこぼしも減る。マンガの商品ページには
    data-old-hires が無く landingImage だけがある（未来日記 (10)(12) で確認）。
    """
    found = _HIRES_RE.search(html)
    if found:
        return found.group(1)
    landing = _LANDING_RE.search(html)
    if not landing:
        return None
    best = None
    best_area = 0
    block = landing.group(1) or landing.group(2) or ""
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


# 取得した表紙と本文 1 ページ目が同じ絵かを判定する閾値 (#27)。
#
# **リーダーのページ送りに表紙が入っている本がある。** マンガはたいていそうで、
# しかもフル解像度（1600x1200）。そこへ 353x500 のストア画像を足すと、同じ絵が
# 小さく劣化して 2 枚並ぶ。実測（未来日記・ヨコハマ買い出し紀行・てーきゅう）。
#
# 実測値:
#     同じ表紙                     1.32 / 1.48 / 2.79
#     表紙と「同じデザインの扉」   16.09      ← 一番紛らわしい本
#     表紙と本文                   50 以上
#
# 6 は、真の重複（最大 2.79）の 2 倍、紛らわしい例（16.09）の 1/2.7。
# **迷ったら足す側に倒す。** 冗長な 1 ページより、表紙が無いほうが困る。
SAME_PICTURE_DIFF = 6.0


def _fingerprint(image):
    """余白を落として 32x32 のグレースケールにする。

    余白を落とすのは、取得した表紙が枠の中で小さく置かれるため。落とさないと
    同じ絵でも「片方だけ白が多い」で別物に見える。
    """
    import numpy as np
    from PIL import ImageChops

    picture = image.convert("RGB")
    ground = Image.new("RGB", picture.size, picture.getpixel((0, 0)))
    box = ImageChops.difference(picture, ground).getbbox()
    if box:
        picture = picture.crop(box)
    return np.asarray(picture.convert("L").resize((32, 32)), dtype=np.float32)


def same_picture(left, right, *, threshold=SAME_PICTURE_DIFF):
    """2 枚が同じ絵か。判定できなければ False（＝表紙を足す側に倒す）。"""
    import numpy as np

    try:
        return float(np.abs(_fingerprint(left) - _fingerprint(right)).mean()) < threshold
    except Exception:  # noqa: BLE001 - 比べられないなら足す
        return False
