"""close_book: 画面経路が自分で開いた Cloud Reader のタブを閉じる (#147)。

ウィンドウの検索・題名・全画面判定・キー送信をすべて差し替え、押したキーと
イベントだけを見る。
"""

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 API")
# Windows 以外では ctypes.windll が無く import できないので、collect の時点で skip にする
pytest.importorskip("core.win32_utils")
pytest.importorskip("pyautogui")

from core import reader_navigator  # noqa: E402
from core.capture_profiles import get_profile  # noqa: E402


class FakeEngine:
    """CaptureEngine の代役。見つかるウィンドウと前面化の成否は作るときに決める。"""

    hwnd = None
    in_front = True

    def __init__(self, profile, exclude_pid=None):
        self.profile = profile

    def find_target_window(self):
        return self.hwnd

    def activate_target_window(self, hwnd, emit=None):
        return self.in_front


def _stubs(monkeypatch, *, hwnd, titles, fullscreen, process="chrome.exe", in_front=True):
    """titles は get_window_title が順に返す題名（閉じる前・閉じた後）。"""
    keys = []
    events = []
    remaining = list(titles)
    FakeEngine.hwnd = hwnd
    FakeEngine.in_front = in_front
    monkeypatch.setattr(reader_navigator, "CaptureEngine", FakeEngine)
    monkeypatch.setattr("core.win32_utils.get_window_process_name", lambda h: process)
    monkeypatch.setattr("core.win32_utils.get_window_title", lambda h: remaining.pop(0))
    monkeypatch.setattr("core.win32_utils.is_window_fullscreen", lambda h: fullscreen)
    monkeypatch.setattr("pyautogui.press", lambda key: keys.append(key))
    monkeypatch.setattr("pyautogui.hotkey", lambda *key: keys.append("+".join(key)))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return keys, events, (lambda name, **kw: events.append((name, kw)))


def test_close_book_leaves_fullscreen_then_closes_the_tab(monkeypatch):
    keys, events, emit = _stubs(
        monkeypatch, hwnd=1, titles=["Kindle", "新しいタブ"], fullscreen=True
    )
    assert reader_navigator.close_book(get_profile("kindle_cloud"), emit=emit) is True
    assert keys == ["f11", "ctrl+w"]
    (closed,) = [kw for name, kw in events if name == "closed"]
    assert closed["ok"] is True and closed["title_before"] == "Kindle"


def test_close_book_skips_f11_when_not_fullscreen(monkeypatch):
    # ウィンドウごと閉じたときは題名が空になる。それも「閉じた」
    keys, events, emit = _stubs(monkeypatch, hwnd=1, titles=["Kindle", ""], fullscreen=False)
    assert reader_navigator.close_book(get_profile("kindle_cloud"), emit=emit) is True
    assert keys == ["ctrl+w"]


def test_close_book_never_sends_ctrl_w_twice(monkeypatch):
    """題名が変わらなくても押し直さない。次のタブは利用者のものかもしれない。"""
    keys, events, emit = _stubs(monkeypatch, hwnd=1, titles=["Kindle", "Kindle"], fullscreen=False)
    assert reader_navigator.close_book(get_profile("kindle_cloud"), emit=emit) is False
    assert keys == ["ctrl+w"]
    (closed,) = [kw for name, kw in events if name == "closed"]
    assert closed["ok"] is False and closed["reason"] == "title_unchanged"


def test_close_book_does_nothing_without_a_reader_window(monkeypatch):
    keys, events, emit = _stubs(monkeypatch, hwnd=None, titles=[], fullscreen=False)
    assert reader_navigator.close_book(get_profile("kindle_cloud"), emit=emit) is False
    assert keys == []
    (closed,) = [kw for name, kw in events if name == "closed"]
    assert closed["reason"] == "window_not_found"


def test_close_book_sends_nothing_when_the_reader_cannot_be_brought_to_front(monkeypatch):
    """前面化に失敗したら送らない。Ctrl+W はそのとき前面にいる別の窓を閉じてしまう。"""
    keys, events, emit = _stubs(
        monkeypatch, hwnd=1, titles=["Kindle", ""], fullscreen=True, in_front=False
    )
    assert reader_navigator.close_book(get_profile("kindle_cloud"), emit=emit) is False
    assert keys == []
    (closed,) = [kw for name, kw in events if name == "closed"]
    assert closed["reason"] == "activate_failed"


def test_close_book_ignores_a_window_of_another_process(monkeypatch):
    """題名に Kindle を含む別プロセスの窓（エディタなど）に Ctrl+W を送らない。"""
    keys, events, emit = _stubs(
        monkeypatch, hwnd=1, titles=["Kindle", ""], fullscreen=False, process="Code.exe"
    )
    assert reader_navigator.close_book(get_profile("kindle_cloud"), emit=emit) is False
    assert keys == []
