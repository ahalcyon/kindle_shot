"""Amazon のサインインページ検出

Cloud Reader のセッションが切れていると read.amazon.co.jp はサインインページへ
リダイレクトする。このときウィンドウタイトルに "Kindle" が入らないため、
reader_navigator の待機は「対象ウィンドウが見つかりません」で終わってしまい、
本当の原因（ログアウトされている）が分からない。ここではその状態を検出して
区別できるようにする。

自動ログインはここでは行わない。ウィンドウにキーストロークでパスワードを
送る方式は、打鍵中にフォーカスが移るとパスワードが別ウィンドウ（チャット欄・
エディタ等）へ平文で入力され、続く Enter で送信まで確定してしまう。
DOM を直接操作できる headless ブラウザ方式（#9）へ移行したうえで
`page.fill()` で入力するのが安全なため、そちらで扱う。
"""

# サインインページのウィンドウタイトルに含まれる手がかり（小文字で比較する）。
# 実測: セッション切れ時のタイトルは "Amazonサインイン"。
# 単独では他サイトのログイン画面に誤反応するため "amazon" との併用を必須にする。
_SIGNIN_HINTS = ("サインイン", "signin", "sign in", "sign-in", "ログイン")


def looks_like_signin(title):
    """ウィンドウタイトルが Amazon のサインインページらしいか。

    "amazon" を必須にしているのは、他サイトの「ログイン」タブを
    サインインページと誤認しないため。
    """
    text = (title or "").lower()
    if "amazon" not in text:
        return False
    return any(hint in text for hint in _SIGNIN_HINTS)


def find_signin_window(process_name=None, exclude_pid=None):
    """サインインページらしきウィンドウを探す。見つからなければ None。

    タイトルだけでなくプロセス名も照合する。find_window の process_name は
    フィルタではなくスコア加算なので、これを省くと「タイトルに amazon と
    ログインを含む」だけの無関係なウィンドウ（記事を開いたエディタ等）を
    拾ってしまう。

    注: ブラウザのウィンドウタイトルはアクティブなタブのものになるため、
    サインインページが非アクティブタブだと検出できない。URL を開いた直後は
    そのタブがアクティブになる前提で使う。
    """
    from core.win32_utils import find_window, get_window_process_name, get_window_title

    for keyword in ("サインイン", "Amazon"):
        hwnd = find_window(keyword, exclude_pid=exclude_pid, process_name=process_name)
        if hwnd is None:
            continue
        if not looks_like_signin(get_window_title(hwnd)):
            continue
        if process_name and get_window_process_name(hwnd).lower() != process_name.lower():
            continue
        return hwnd
    return None
