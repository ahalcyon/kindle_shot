"""core/console.py のテスト（出力の文字コード。#122 のコメントに実測）"""

import sys

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
