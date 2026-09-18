"""コンソールへの出力の文字コードを揃える。

Windows では、出力をファイルやパイプに繋ぐと Python は既定の文字コード（cp932）で
書く。本の題名には cp932 に無い文字（「——」U+2014 など）が入っていることがあり、
**進捗を 1 行出すだけで一括処理が止まる**（実測: 蔵書 15 冊の書き換えが 13 冊目で
UnicodeEncodeError で落ちた）。書式文字列に「—」が入っていて、題名によらず必ず落ちる
ものもあった（`scripts/strip_text_layer.py`）。

効いているのは **cp932 から UTF-8 への切り替え**で、UTF-8 ならほとんどの字がそのまま
書ける。``errors="replace"`` は保険（Windows のファイル名由来の単独サロゲートなど）。

**一括処理のスクリプトは `main()` の 1 行目でこれを呼ぶこと**（`tests/test_console.py`
が全スクリプトを見て守らせている）。

コンソール直結 (tty) のときは触らない。WriteConsoleW 経由で日本語がそのまま出せる。
"""

from __future__ import annotations

import sys


def setup_stdio():
    """パイプ・ファイルへの出力を UTF-8（書けない字は置き換え）に固定する。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 出力の設定に失敗しても処理は続ける
            pass
