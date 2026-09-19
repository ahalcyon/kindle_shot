"""前面化の結果を確かめて返す (#74)。

以前の activate_window は SetForegroundWindow のあと固定で 1 秒寝て返るだけで、
「戻った時点で前面にある」は検証されていなかった。
"""

import sys

import pytest

from core.capture_engine import CaptureEngine
from core.capture_profiles import get_profile

win = pytest.importorskip("core.win32_utils") if sys.platform == "win32" else None
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 API")


def test_waits_only_until_the_window_is_in_front():
    """既に前面なら待たない。途中で前面になればそこで返る。"""
    sleeps = []
    assert win.wait_for_foreground(lambda: True, sleep=sleeps.append)
    assert sleeps == []
    seen = iter([False, False, True])
    assert win.wait_for_foreground(lambda: next(seen), interval=0.1, sleep=sleeps.append)
    assert sleeps == [0.1, 0.1]


def test_gives_up_after_the_timeout():
    sleeps = []
    assert not win.wait_for_foreground(
        lambda: False, timeout=0.5, interval=0.1, sleep=sleeps.append
    )
    assert len(sleeps) == 5


def test_engine_records_what_was_in_front_when_activation_fails(monkeypatch):
    """前面にできなかったら、何が前面にいたかを activate_failed で残す（#74 の原因を追う手がかり）。"""
    monkeypatch.setattr("core.capture_engine.activate_window", lambda *a, **kw: False)
    monkeypatch.setattr("core.win32_utils.foreground_window_title", lambda: "別のウィンドウ")
    engine = CaptureEngine(get_profile("kindle_cloud"))
    events = []
    assert engine.activate_target_window(1234, emit=lambda e, **kw: events.append((e, kw))) is False
    assert events[0][0] == "activate_failed"
    assert events[0][1]["foreground"] == "別のウィンドウ"


def test_engine_is_quiet_when_activation_succeeds(monkeypatch):
    monkeypatch.setattr("core.capture_engine.activate_window", lambda *a, **kw: True)
    engine = CaptureEngine(get_profile("kindle_cloud"))
    events = []
    assert engine.activate_target_window(1234, emit=lambda e, **kw: events.append(e)) is True
    assert events == []
