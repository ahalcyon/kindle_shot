"""Win32 API ユーティリティ

ウィンドウの検索・活性化、ダイアログ表示など Windows 固有の操作を提供する。
"""

import os
import threading
import time
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    byref,
    c_bool,
    c_int,
    c_void_p,
    cast,
    create_unicode_buffer,
    pointer,
    windll,
)
from ctypes.wintypes import DWORD, RECT

import pyautogui as pag

# GUI が core.win32_utils から import しているので、ここで再エクスポートする（#52）
from core.safe_names import sanitize_folder_name  # noqa: F401

pag.FAILSAFE = False


def _get_process_name(pid):
    """プロセスIDからプロセスのexe名を取得する。失敗時は空文字を返す。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        OpenProcess = windll.kernel32.OpenProcess
        CloseHandle = windll.kernel32.CloseHandle
        QueryFullProcessImageNameW = windll.kernel32.QueryFullProcessImageNameW

        handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = create_unicode_buffer(1024)
            size = DWORD(1024)
            if QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
                # フルパスからファイル名のみ抽出
                return os.path.basename(buf.value)
        finally:
            CloseHandle(handle)
    except Exception:
        pass
    return ""


def find_window(title_keyword, exclude_pid=None, process_name=None):
    """指定キーワードを含むウィンドウタイトルのハンドルを返す。見つからなければ None。

    Args:
        title_keyword: ウィンドウタイトルに含まれるキーワード (大文字小文字を区別しない)
        exclude_pid: 除外するプロセスID (自アプリのウィンドウを除外するために使用)
        process_name: プロセス名フィルタ (例: "Kindle.exe"、一致時にスコア加算)
    """
    EnumWindows = windll.user32.EnumWindows
    GetWindowText = windll.user32.GetWindowTextW
    GetWindowTextLength = windll.user32.GetWindowTextLengthW
    IsWindowVisible = windll.user32.IsWindowVisible
    GetWindowThreadProcessId = windll.user32.GetWindowThreadProcessId
    WNDENUMPROC = WINFUNCTYPE(c_bool, POINTER(c_int), POINTER(c_int))

    candidates = []  # (score, hwnd, title, exe_name)
    keyword_lower = title_keyword.lower()
    process_name_lower = process_name.lower() if process_name else ""

    def EnumWindowsProc(hwnd, lParam):
        if not IsWindowVisible(hwnd):
            return True

        pid = DWORD()
        GetWindowThreadProcessId(hwnd, byref(pid))

        if exclude_pid is not None and pid.value == exclude_pid:
            return True

        length = GetWindowTextLength(hwnd)
        if length == 0:
            return True
        buff = create_unicode_buffer(length + 1)
        GetWindowText(hwnd, buff, length + 1)
        title = buff.value
        title_lower = title.lower()

        if keyword_lower not in title_lower:
            return True

        # スコアリング: Kindle本体のウィンドウを優先
        score = 1
        if title_lower.endswith(keyword_lower):
            score = 10  # "書名 - Kindle" パターン
        elif title_lower == keyword_lower:
            score = 10  # "Kindle" 単体
        elif title_lower.startswith(keyword_lower):
            score = 8
        elif f" {keyword_lower}" in title_lower or f"- {keyword_lower}" in title_lower:
            score = 5  # 単語境界

        # プロセス名フィルタ: 一致すればスコア大幅加算
        exe_name = ""
        if process_name_lower:
            exe_name = _get_process_name(pid.value)
            if exe_name.lower() == process_name_lower:
                score += 20

        candidates.append((score, hwnd, title, exe_name))
        return True

    EnumWindows(WNDENUMPROC(EnumWindowsProc), 0)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0]
    return best[1]


def get_window_title(hwnd):
    """ウィンドウハンドルからタイトルを取得する。"""
    GetWindowText = windll.user32.GetWindowTextW
    GetWindowTextLength = windll.user32.GetWindowTextLengthW
    length = GetWindowTextLength(hwnd)
    if length == 0:
        return ""
    buff = create_unicode_buffer(length + 1)
    GetWindowText(hwnd, buff, length + 1)
    return buff.value


def foreground_window_title():
    """いま前面にあるウィンドウのタイトル。前面化に失敗したとき何がいたかを残す (#74)。"""
    try:
        return get_window_title(windll.user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001 - 記録のためだけなので落とさない
        return ""


# 前面化の確認に待つ上限。SetForegroundWindow は非同期に効くことがあり、
# 直後の GetForegroundWindow はまだ前のウィンドウを返す
FOREGROUND_TIMEOUT = 2.0
FOREGROUND_POLL = 0.1


def wait_for_foreground(
    is_foreground, *, timeout=FOREGROUND_TIMEOUT, interval=FOREGROUND_POLL, sleep=time.sleep
):
    """is_foreground() が True になるまで interval ごとに見る。timeout までに True にならなければ False。

    固定スリープの代わり。前面になった時点で返るので、既に前面のときは待たない。
    """
    waited = 0.0
    while True:
        if is_foreground():
            return True
        if waited >= timeout:
            return False
        sleep(interval)
        waited += interval


def hwnd_value(hwnd):
    """ウィンドウハンドルを比較可能な整数に正規化する。

    find_window は EnumWindows のコールバック宣言の都合で ctypes の
    ポインタ (LP_c_long) を返すのに対し、Win32 API を直に呼ぶと int が
    返る。型が違うと == / != が常に不一致になり、ハンドルの比較が
    黙って壊れる（実測で確認）。比較する前に必ずここを通すこと。
    """
    if hwnd is None:
        return None
    if isinstance(hwnd, int):
        return hwnd
    return cast(hwnd, c_void_p).value


def get_window_rect(hwnd):
    """ウィンドウの矩形座標 (left, top, right, bottom) を返す。"""
    GetWindowRect = windll.user32.GetWindowRect
    rect = RECT()
    GetWindowRect(hwnd, pointer(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_monitor_rects():
    """全モニタの矩形を仮想スクリーン座標で返す。失敗時は空リストを返す。"""
    from ctypes import POINTER, WINFUNCTYPE
    from ctypes.wintypes import BOOL, HDC, HMONITOR, LPARAM

    monitor_rects = []
    callback_failed = False

    def callback(_hmonitor, _hdc, rect_ptr, _lparam):
        nonlocal callback_failed
        try:
            rect = rect_ptr.contents
            monitor_rects.append((rect.left, rect.top, rect.right, rect.bottom))
        except Exception:
            callback_failed = True
            return False
        return True

    try:
        monitor_enum_proc = WINFUNCTYPE(BOOL, HMONITOR, HDC, POINTER(RECT), LPARAM)(callback)
        enumerated = windll.user32.EnumDisplayMonitors(None, None, monitor_enum_proc, 0)
        if not enumerated or callback_failed:
            return []
    except Exception:
        return []
    return monitor_rects


def is_window_fullscreen(hwnd):
    """ウィンドウが F11 全画面等でモニタ全面を占めているかを返す。

    矩形だけで判定すると、タスクバー自動非表示のモニタでは最大化ウィンドウも
    モニタ全面を覆う（不可視ボーダー分むしろ大きい）ため全画面と誤判定する。
    全画面ウィンドウはタイトルバー（WS_CAPTION）を持たないことも条件にする。
    """
    from ctypes import Structure, sizeof

    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000
    style = windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
    if (style & WS_CAPTION) == WS_CAPTION:
        return False

    class MONITORINFO(Structure):
        _fields_ = [
            ("cbSize", DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", DWORD),
        ]

    MONITOR_DEFAULTTONEAREST = 2
    hmon = windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = sizeof(MONITORINFO)
    if not windll.user32.GetMonitorInfoW(hmon, byref(info)):
        return False
    left, top, right, bottom = get_window_rect(hwnd)
    m = info.rcMonitor
    return left <= m.left and top <= m.top and right >= m.right and bottom >= m.bottom


def get_window_process_name(hwnd):
    """ウィンドウハンドルからプロセスのexe名を取得する。"""
    GetWindowThreadProcessId = windll.user32.GetWindowThreadProcessId
    pid = DWORD()
    GetWindowThreadProcessId(hwnd, byref(pid))
    return _get_process_name(pid.value)


def activate_window(hwnd, click_position="center", use_bring_to_top=False):
    """ウィンドウを前面に出してクリックする。**前面になったかを確かめて** True / False を返す (#74)。

    click_position:
        'center': ウィンドウ中央をクリック
        'top_left': 左上付近をクリック
        'none': クリックしない (Kindle Cloud Reader 等、クリックがリーダーUIの
                表示をトグルしてキャプチャに写り込むアプリ向け。前面化だけで
                キー入力は届くことを確認済み)

    以前は前面化の結果を確かめずに固定で 1 秒寝て返していた。「戻った時点で前面にある」は
    主張されているだけで検証されておらず、画面キャプチャが 4 回に 1 回 1 ページ目で止まる
    不安定さの原因候補になっていた (#74)。前面になるまで最大 FOREGROUND_TIMEOUT 秒待ち、
    ならなければ False を返す（呼ぶ側が何が前面にいたかを記録する）。クリックは従来どおり行う。
    """
    IsIconic = windll.user32.IsIconic
    ShowWindow = windll.user32.ShowWindow
    SetForegroundWindow = windll.user32.SetForegroundWindow
    GetForegroundWindow = windll.user32.GetForegroundWindow
    GetWindowRect = windll.user32.GetWindowRect
    GetWindowThreadProcessId = windll.user32.GetWindowThreadProcessId
    GetCurrentThreadId = windll.kernel32.GetCurrentThreadId
    AttachThreadInput = windll.user32.AttachThreadInput
    SetWindowPos = windll.user32.SetWindowPos

    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    # 最小化されている場合は復元
    if IsIconic(hwnd):
        ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)

    # AttachThreadInput トリックで前面化の権限を取得
    foreground_hwnd = GetForegroundWindow()
    fore_tid = DWORD()
    GetWindowThreadProcessId(foreground_hwnd, byref(fore_tid))
    current_tid = GetCurrentThreadId()

    attached = False
    if fore_tid.value != current_tid:
        attached = bool(AttachThreadInput(current_tid, fore_tid.value, True))

    try:
        if use_bring_to_top:
            BringWindowToTop = windll.user32.BringWindowToTop
            BringWindowToTop(hwnd)

        SetForegroundWindow(hwnd)

        # フォールバック: TOPMOST → NOTOPMOST で確実に前面化
        time.sleep(0.1)
        if hwnd_value(GetForegroundWindow()) != hwnd_value(hwnd):
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    finally:
        if attached:
            AttachThreadInput(current_tid, fore_tid.value, False)

    in_front = wait_for_foreground(lambda: hwnd_value(GetForegroundWindow()) == hwnd_value(hwnd))

    if click_position == "none":
        # 前面になってからも従来どおり 1 秒置く（アプリ経路の検索窓入力がこの間を当てにしている
        # 可能性があるので、確認を足しただけで待ちは減らさない）
        time.sleep(1)
        return in_front

    rect = RECT()
    GetWindowRect(hwnd, pointer(rect))

    if click_position == "center":
        x = rect.left + (rect.right - rect.left) // 2
        y = rect.top + (rect.bottom - rect.top) // 2
    else:
        x = rect.left + 60
        y = rect.top + 10

    pag.moveTo(x, y)
    pag.click()
    time.sleep(1)
    return in_front


# SetThreadExecutionState のフラグ
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


# prevent_sleep のネストカウンタ。SetThreadExecutionState はスレッド単位の
# 状態なので、カウンタもスレッドローカルに持つ（抑止と解除は同一スレッドで
# 対にして呼ぶこと。スレッド終了時は OS 側で自動解除される）
_sleep_block = threading.local()


def prevent_sleep(*, keep_display=True):
    """スリープ・画面消灯を抑止する（呼び出したスレッドで有効）。

    長時間の無人キャプチャ中に画面が消えると真っ黒画像になり、さらに
    復帰時のサインインでセッションがロックされると以降の open / capture が
    全滅するため、処理の間は呼び続ける。ネスト可能（run_book 全体 → その中の
    open / capture のように重ねて呼べる）。終了時は必ず対で allow_sleep() を
    呼ぶこと。実際の解除は最後の allow_sleep() で行われる。

    keep_display=False にすると画面消灯だけは許す。headless キャプチャは
    画面を使わないため、消灯まで抑止すると「ディスプレイを切って無人実行」
    という目的と逆行する。
    """
    count = getattr(_sleep_block, "count", 0)
    _sleep_block.count = count + 1
    if count == 0:
        flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
        if keep_display:
            flags |= _ES_DISPLAY_REQUIRED
        windll.kernel32.SetThreadExecutionState(flags)


def allow_sleep():
    """prevent_sleep() の抑止を解除する（ネストの最後の解除で実際に解除）。"""
    count = max(0, getattr(_sleep_block, "count", 0) - 1)
    _sleep_block.count = count
    if count == 0:
        windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)


# sanitize_folder_name の実体は core/safe_names に移した（上で再エクスポート）。
# CLI / バッチ経路からも使う必要があるが、このモジュールは ctypes.windll と
# pyautogui を module レベルで import するため Linux から import できない（#52）。
