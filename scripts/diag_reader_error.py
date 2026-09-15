"""開いた瞬間にリーダーエラーになる本を、1 回の実行で診断する (#104)。

ハリー・ポッター全 7 巻（`B01B6RN6IS`）だけ、`?asin=` で開くと本文を 1 ページも
撮れないままリーダーがエラーを出す。利用者が別デバイスで 2 巻の冒頭を読んでおり、
**同期された読書位置で開くことが原因ではないか**という仮説が立っている
（`?asin=` は前回の読書位置で開く。`rewind_to_start` の docstring に実測あり）。

対策を入れる前に、**エラー画面で何が生きているか**を確かめる必要がある。
それによって「1 ページまで繰る」の実装が変わるため:

- ページが生きている（目次が開ける）なら、**ページ内の操作**で先頭へ飛べる
- ページが死んでいるなら、**開く URL の側**で先頭を指定するしかない

このスクリプトは**観測だけ**を行う。撮影も書き込みもしない。

    python scripts/diag_reader_error.py --asin B01B6RN6IS

比較のために、正常に撮れている本の ASIN も渡せる（`--compare`）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.headless_browser import open_reader
from core.headless_capture import (
    PAGE_IMAGE_SELECTOR,
    POSITION_SELECTOR,
    TOC_BUTTON_SELECTOR,
    TOC_ITEM_SELECTOR,
    TOC_OPEN_JS,
    alert_text,
    reader_error_text,
    unsupported_reason,
)

# 先頭を指定して開けるかどうかは分かっていない。当てずっぽうを 1 回で潰すため、
# ありそうな形をまとめて試す。**効いた形だけを採用する**（効かない形を
# 「たぶんこれ」で入れない）。
START_URL_CANDIDATES = (
    "https://read.amazon.co.jp/?asin={asin}&location=1",
    "https://read.amazon.co.jp/?asin={asin}#location=1",
    "https://read.amazon.co.jp/reader?asin={asin}&location=1",
    "https://read.amazon.co.jp/?asin={asin}&startPosition=0",
)
LIBRARY_URL = "https://read.amazon.co.jp/kindle-library"


def observe(page):
    """いま画面に何があるかを採る。壊れていても落ちないこと。"""

    def text(selector):
        try:
            loc = page.locator(selector)
            if loc.count() == 0:
                return None
            return " ".join((loc.first.inner_text() or "").split())[:200]
        except Exception as exc:
            return f"<error: {exc}>"[:200]

    def count(selector):
        try:
            return page.locator(selector).count()
        except Exception:
            return -1

    return {
        "url": page.url,
        "alert": " ".join((alert_text(page) or "").split())[:200] or None,
        "reader_error": reader_error_text(page),
        "unsupported": unsupported_reason(page),
        "position_label": text(POSITION_SELECTOR),
        "page_images": count(PAGE_IMAGE_SELECTOR),
        "toc_buttons": count(TOC_BUTTON_SELECTOR),
        "renderer": count("#kr-renderer"),
    }


def try_toc(page):
    """エラー画面から目次を開けるかを見る。開けたら項目数も採る。

    **開いたら閉じる。** 開きっぱなしにするとキー入力が死ぬ（#72 と同じ形）。
    """
    try:
        if not page.evaluate(TOC_OPEN_JS, TOC_BUTTON_SELECTOR):
            return {"opened": False, "reason": "目次ボタンが見つからない"}
        page.wait_for_selector(TOC_ITEM_SELECTOR, timeout=8000)
        items = page.locator(TOC_ITEM_SELECTOR).count()
        return {"opened": True, "items": items}
    except Exception as exc:
        return {"opened": False, "reason": str(exc)[:200]}
    finally:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
        except Exception:
            pass


def probe(url, *, profile_dir, headless, wait_ms, label, toc=False):
    out = {"label": label, "requested_url": url}
    with open_reader(url, profile_dir=profile_dir, headless=headless) as page:
        if page is None:
            out["error"] = "ページを開けなかった"
            return out
        page.wait_for_timeout(wait_ms)
        out.update(observe(page))
        if toc:
            out["toc"] = try_toc(page)
            out["after_toc"] = observe(page)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="開いた瞬間のリーダーエラーを診断する (#104)")
    ap.add_argument("--asin", default="B01B6RN6IS", help="調べる本の ASIN")
    ap.add_argument("--compare", help="正常に撮れている本の ASIN（対照）")
    ap.add_argument("--profile-dir", help="ブラウザプロファイル（省略時は既定）")
    ap.add_argument("--headed", action="store_true", help="画面を出して開く")
    ap.add_argument("--wait", type=float, default=12.0, help="開いてから観測するまでの秒数")
    ap.add_argument("--out", help="結果を書き出す JSON のパス")
    args = ap.parse_args(argv)

    wait_ms = int(args.wait * 1000)
    common = {
        "profile_dir": args.profile_dir,
        "headless": not args.headed,
        "wait_ms": wait_ms,
    }
    results = []

    # 1. いまの経路をそのまま。エラー画面で何が生きているかを見る
    results.append(
        probe(
            f"https://read.amazon.co.jp/?asin={args.asin}",
            label="現行 (?asin=)",
            toc=True,
            **common,
        )
    )

    # 2. 先頭を指定して開けるか。効く形が 1 つでもあれば実装はこれで決まる
    for template in START_URL_CANDIDATES:
        results.append(
            probe(template.format(asin=args.asin), label=f"位置指定: {template}", **common)
        )

    # 3. ライブラリ経由。エラー文言自身が「ライブラリから開き直せ」と言っている
    results.append(probe(LIBRARY_URL, label="ライブラリ", **common))

    # 4. 対照。同じ観測が正常な本でどう出るかが無いと、異常かどうか言えない
    if args.compare:
        results.append(
            probe(
                f"https://read.amazon.co.jp/?asin={args.compare}",
                label=f"対照 (正常な本 {args.compare})",
                toc=True,
                **common,
            )
        )

    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
        print(f"書き出した: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
