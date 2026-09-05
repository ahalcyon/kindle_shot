"""core/amazon_signin.py のテスト（セッション切れの検出）

誤検出すると「ログアウトされています」という誤ったエラーを出して
本当の原因（ブラウザ未起動・プロファイル設定違い）を隠してしまうため、
判定の正例と誤検出防止をここで固定する。
"""

import sys
import types
from typing import Any

import pytest

from core import amazon_signin
from core.amazon_signin import find_signin_window, looks_like_signin


@pytest.mark.parametrize(
    "title",
    [
        # 実測: セッション切れ時のタイトルは "Amazonサインイン"
        "Amazonサインイン - Google Chrome",
        "Amazon Sign-In - Google Chrome",
        "amazon sign in",
        "Amazonログイン",
    ],
)
def test_signin_titles_are_detected(title):
    assert looks_like_signin(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # 読書中・ライブラリ
        "Kindle - Google Chrome",
        "みんなのフィードバック大全 - Kindle",
        # amazon が付かない他サイトのログイン画面
        "GitHub にログイン - Google Chrome",
        "Sign in to your account",
        "ログイン | 楽天",
        "",
        None,
    ],
)
def test_non_signin_titles_are_rejected(title):
    assert looks_like_signin(title) is False


# ------------------------------------------------------------
# find_signin_window: タイトルとプロセスの両方を照合すること
# ------------------------------------------------------------


def install_fakes(monkeypatch, *, title, process, hwnd=12345):
    """core.win32_utils を偽物に差し替える。

    実モジュールは ctypes.windll と pyautogui に依存し Windows でしか
    import できないため、sys.modules へ差し込んで読み込ませない。
    find_signin_window は関数内 import なのでこれで差し替わる。
    """
    # ModuleType へ属性を生やすので型検査には Any として見せる
    fake: Any = types.ModuleType("core.win32_utils")
    fake.find_window = lambda keyword, exclude_pid=None, process_name=None: hwnd
    fake.get_window_title = lambda h: title
    fake.get_window_process_name = lambda h: process
    monkeypatch.setitem(sys.modules, "core.win32_utils", fake)
    return fake


def test_found_when_title_and_process_match(monkeypatch):
    install_fakes(monkeypatch, title="Amazonサインイン", process="chrome.exe")
    assert find_signin_window(process_name="chrome.exe") == 12345


def test_rejected_when_process_differs(monkeypatch):
    """タイトルが一致してもプロセスが違えば採用しない。

    find_window の process_name はフィルタではなくスコア加算なので、
    ここで照合しないと「Amazon のログイン方法」を開いたエディタ等を拾う。
    """
    install_fakes(monkeypatch, title="Amazonサインイン", process="Code.exe")
    assert find_signin_window(process_name="chrome.exe") is None


def test_rejected_when_title_is_not_signin(monkeypatch):
    install_fakes(monkeypatch, title="Kindle - Google Chrome", process="chrome.exe")
    assert find_signin_window(process_name="chrome.exe") is None


def test_returns_none_when_no_window(monkeypatch):
    install_fakes(monkeypatch, title="Amazonサインイン", process="chrome.exe", hwnd=None)
    assert find_signin_window(process_name="chrome.exe") is None


def test_process_check_skipped_when_not_specified(monkeypatch):
    """プロファイルにプロセス名が無ければタイトルだけで判断する。"""
    install_fakes(monkeypatch, title="Amazonサインイン", process="whatever.exe")
    assert find_signin_window(process_name=None) == 12345


def test_module_does_not_handle_credentials():
    """打鍵によるパスワード入力は持たない（headless 方式へ移すため）。"""
    for name in ("sign_in", "credentials_from_env", "has_credentials"):
        assert not hasattr(amazon_signin, name)
