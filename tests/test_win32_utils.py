"""前面化の結果を確かめて返す (#74)。

以前の activate_window は SetForegroundWindow のあと固定で 1 秒寝て返るだけで、
「戻った時点で前面にある」は検証されていなかった。
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 API")
# Windows 以外では ctypes.windll が無く import できないので、collect の時点で skip にする
win = pytest.importorskip("core.win32_utils")

from core.capture_engine import CaptureEngine  # noqa: E402
from core.capture_profiles import get_profile  # noqa: E402


def test_waits_only_until_the_window_is_in_front():
    """既に前面なら待たない。途中で前面になればそこで返る。"""
    sleeps: list[float] = []
    assert win.wait_for_foreground(lambda: True, sleep=sleeps.append)
    assert sleeps == []
    seen = iter([False, False, True])
    assert win.wait_for_foreground(lambda: next(seen), interval=0.1, sleep=sleeps.append)
    assert sleeps == [0.1, 0.1]


def test_gives_up_after_the_timeout():
    sleeps: list[float] = []
    assert not win.wait_for_foreground(
        lambda: False, timeout=0.5, interval=0.1, sleep=sleeps.append
    )
    assert len(sleeps) == 5
    # 回数で数える: 1.0 / 0.1 は float だと 10.000000000000002 で 11 回になる
    sleeps.clear()
    assert not win.wait_for_foreground(
        lambda: False, timeout=1.0, interval=0.1, sleep=sleeps.append
    )
    assert len(sleeps) == 10
    # timeout 0 でも 1 回は見る
    seen: list[int] = []

    def once():
        seen.append(1)
        return True

    assert win.wait_for_foreground(once, timeout=0, sleep=sleeps.append)
    assert seen == [1]


def test_engine_records_what_was_in_front_when_activation_fails(monkeypatch):
    """前面にできなかったら、何が前面にいたかを activate_failed で残す（#74 の原因を追う手がかり）。"""
    monkeypatch.setattr(
        "core.capture_engine.activate_window_report", lambda *a, **kw: (False, "別のウィンドウ")
    )
    engine = CaptureEngine(get_profile("kindle_cloud"))
    events = []
    assert engine.activate_target_window(1234, emit=lambda e, **kw: events.append((e, kw))) is False
    assert events[0][0] == "activate_failed"
    assert events[0][1]["foreground"] == "別のウィンドウ"


def test_engine_is_quiet_when_activation_succeeds(monkeypatch):
    monkeypatch.setattr("core.capture_engine.activate_window_report", lambda *a, **kw: (True, ""))
    engine = CaptureEngine(get_profile("kindle_cloud"))
    events = []
    assert engine.activate_target_window(1234, emit=lambda e, **kw: events.append(e)) is True
    assert events == []
