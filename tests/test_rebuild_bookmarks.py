"""scripts/rebuild_bookmarks.py のテスト（#114）"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import rebuild_bookmarks as rb  # noqa: E402

from core.kindle_toc import TocEntry  # noqa: E402

STARTS = [0, 10, 20, 30, 40, 50]


def test_choose_offset_prefers_the_shift_that_confirms_most_titles():
    # 途中に 2 枚差し込んだ本。後半の章は 2 ずらしたときにテキストで確かめられる
    texts = ["", "", "", "", "", "", "はじまり", "つづき", "", ""]
    entries = [TocEntry(1, "第1章 はじまり", 45), TocEntry(1, "第2章 つづき", 50)]
    got = rb.choose_offset(entries, STARTS, 10, lambda i: texts[i], range(0, 3))
    assert got == 2
    # 試しただけで entries は書き換えない
    assert all(e.page == 0 for e in entries)


def test_choose_offset_without_text_keeps_zero():
    entries = [TocEntry(1, "第1章 はじまり", 45)]
    assert rb.choose_offset(entries, STARTS, 10, None, range(0, 3)) == 0
