"""core/console.py のテスト（出力の文字コード。#125 に実測）"""

import os
import sys

import pytest

from core.console import setup_stdio


class _Stream:
    def __init__(self, tty):
        self.tty = tty
        self.calls = []

    def isatty(self):
        return self.tty

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_setup_stdio_replaces_characters_the_console_cannot_write(monkeypatch):
    """ファイル・パイプへの出力は UTF-8 にし、書けない字は置き換える。

    Windows の既定（cp932）のままだと、題名に「——」が入った本の進捗を 1 行出すだけで
    一括処理が止まる（実測: 蔵書 15 冊の書き換えが 13 冊目で落ちた）。
    """
    out, err = _Stream(tty=False), _Stream(tty=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    setup_stdio()
    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_setup_stdio_leaves_a_console_alone(monkeypatch):
    """コンソール直結なら触らない（WriteConsoleW でそのまま日本語が出る）。"""
    out = _Stream(tty=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _Stream(tty=True))
    setup_stdio()
    assert out.calls == []


def test_setup_stdio_survives_a_stream_that_cannot_be_reconfigured(monkeypatch):
    class _Broken(_Stream):
        def reconfigure(self, **kwargs):
            raise ValueError("設定できません")

    monkeypatch.setattr(sys, "stdout", _Broken(tty=False))
    monkeypatch.setattr(sys, "stderr", _Broken(tty=False))
    setup_stdio()  # 例外を出さない


def test_setup_stdio_lets_a_title_the_console_cannot_write_through(monkeypatch):
    """cp932 では書けない題名（「——」）が、setup_stdio のあとは書けること。

    引数のスナップショットではなく、実際に書けるかどうかを見る。
    """
    import io

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp932", errors="strict")
    stream.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    title = "「恐怖で買って、強欲で売る」短期売買法　——人間の行動学"
    with pytest.raises(UnicodeEncodeError):
        print(title, file=stream, flush=True)
    setup_stdio()
    print(title, file=stream, flush=True)  # 落ちない
    assert title.encode("utf-8") in raw.getvalue()


def test_every_script_sets_up_stdio():
    """一括処理のスクリプトは、表示の 1 行で止まらないよう setup_stdio を呼ぶ (#125)。

    実測: `scripts/strip_text_layer.py` は書式文字列そのものに「—」が入っていて、
    題名によらず Windows で出力をファイルに繋いだ瞬間に落ちる状態だった。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.join(root, "scripts")
    missing = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            source = f.read()
        if "def main(" in source and "setup_stdio()" not in source:
            missing.append(name)
    assert missing == []
