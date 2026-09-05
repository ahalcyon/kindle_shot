"""headless ブラウザ (Playwright) の起動とサインイン

core/headless_capture.py から使う。ブラウザの起動・セッション・ログインだけを
受け持ち、ページの取得ロジックは持たない。

資格情報はリポジトリ直下の .env（.gitignore 済み）か環境変数から読む。
値はイベントにもログにも出さない。

打鍵 (pyautogui) ではなく page.fill() で DOM の要素に直接入力するため、
入力中にフォーカスが移って別ウィンドウへ平文が漏れる経路が無い。
"""

import contextlib
import os

from core.pipeline import emit_error, null_emit

ENV_EMAIL = "KINDLE_SHOT_AMAZON_EMAIL"
ENV_PASSWORD = "KINDLE_SHOT_AMAZON_PASSWORD"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROFILE_DIR = os.path.join(REPO_ROOT, ".playwright-profile")

SIGNIN_MARKER = "/ap/signin"

# Amazon のサインインフォーム。実装差に備えて候補を順に試す。
EMAIL_SELECTORS = ("#ap_email", "#ap_email_login", "input[type=email]")
CONTINUE_SELECTORS = ("#continue", "input#continue", "#continue-announce")
PASSWORD_SELECTORS = ("#ap_password", "input[type=password]")
SUBMIT_SELECTORS = ("#signInSubmit", "input#signInSubmit")
# 自動では越えない追加認証
CHALLENGE_SELECTORS = ("#auth-mfa-otpcode", "#auth-captcha-guess", "input[name=otpCode]")


def load_dotenv(path=None):
    """.env を環境変数へ読み込む（既存の値は上書きしない）。"""
    path = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    return True


def credentials():
    """(email, password) を返す。揃っていなければ (None, None)。"""
    load_dotenv()
    email = (os.environ.get(ENV_EMAIL) or "").strip()
    password = os.environ.get(ENV_PASSWORD) or ""
    if not email or not password:
        return None, None
    return email, password


def realistic_user_agent(playwright):
    """実際のエンジンと矛盾しない UA を組み立てる。

    headless の既定 UA には HeadlessChrome が入り、そのままだと自動化として
    検出されうる。文字列を決め打ちするとエンジンのバージョンとずれて逆に
    不自然になるため、起動中のブラウザ自身の UA から Headless を外して使う。
    """
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        return page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    finally:
        browser.close()


def _fill_first(page, selectors, value):
    for selector in selectors:
        if page.locator(selector).count():
            page.fill(selector, value)
            return True
    return False


def _click_first(page, selectors):
    for selector in selectors:
        if page.locator(selector).count():
            page.click(selector)
            return True
    return False


def sign_in(page, emit=null_emit):
    """サインインページに資格情報を入力する。(ok, message) を返す。"""
    email, password = credentials()
    if not email:
        return False, (
            "セッションが切れており、資格情報も設定されていません。"
            f"{ENV_EMAIL} と {ENV_PASSWORD} を .env か環境変数に設定してください"
        )

    emit("signin", human="サインインしています")
    if not _fill_first(page, EMAIL_SELECTORS, email):
        return False, "メールアドレス欄が見つかりません"
    _click_first(page, CONTINUE_SELECTORS)
    page.wait_for_timeout(4000)

    # パスワードは emit にもログにも出さない
    if not _fill_first(page, PASSWORD_SELECTORS, password):
        return False, "パスワード欄が見つかりません"
    _click_first(page, SUBMIT_SELECTORS)
    page.wait_for_timeout(6000)

    for selector in CHALLENGE_SELECTORS:
        if page.locator(selector).count():
            return False, f"追加認証が要求されました（2 段階認証 / CAPTCHA）: {selector}"
    if SIGNIN_MARKER in page.url:
        return False, "サインインが完了しませんでした（資格情報の誤りの可能性）"
    return True, "サインインしました"


@contextlib.contextmanager
def open_reader(url, *, profile_dir=None, headless=True, viewport=None, emit=null_emit):
    """本の URL を開いた page を返す。失敗時は None を返す。

    セッションは profile_dir に永続化されるので、通常は 2 回目以降
    ログインが発生しない。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        emit_error(
            emit,
            "Playwright がインストールされていません: pip install playwright && playwright install chromium",
        )
        yield None
        return

    profile_dir = profile_dir or DEFAULT_PROFILE_DIR
    viewport = viewport or {"width": 1600, "height": 1200}

    with sync_playwright() as playwright:
        user_agent = realistic_user_agent(playwright)
        context = playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=headless,
            viewport=viewport,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=user_agent,
            # navigator.webdriver を立てない
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

            if SIGNIN_MARKER in page.url:
                ok, message = sign_in(page, emit)
                emit("signin", human=message, message=message)
                if not ok:
                    emit_error(emit, message)
                    yield None
                    return
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)

            emit("opened", human=f"開きました: {page.title()}", url=page.url[:120])
            yield page
        finally:
            context.close()
