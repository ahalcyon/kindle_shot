"""#9 スパイク: headless ブラウザで Cloud Reader のページを取得できるか確かめる

検証したいのは 1 点だけ:
**ログイン済みの状態で、本文が headless でレンダリングされ、ページ送りできるか。**

画面を撮る現行方式と違い page.screenshot() はブラウザプロセス内で描画するため、
ディスプレイオフ・画面ロック・セッション切断でも動く。成立すれば #7（通知の
写り込み）も構造的に解消する。

セッションは Playwright 専用のプロファイルに保持する。初回だけブラウザが
表示されるのでサインインする（認証情報をこのスクリプトは受け取らない）。
2 回目以降は headless で無人実行できる。

使い方:
    python scripts/headless_spike.py --asin B0XXXXXXXX
    python scripts/headless_spike.py --asin B0XXXXXXXX --login   # 明示的にログイン
"""

import argparse
import hashlib
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Chrome 本体のプロファイルとは別に持つ（本物を触らない）
PROFILE_DIR = os.path.join(REPO_ROOT, ".playwright-profile")
LIBRARY_URL = "https://read.amazon.co.jp/kindle-library"


def book_url(asin):
    return f"https://read.amazon.co.jp/?asin={asin}"


def is_signed_in(page):
    """サインインページへ飛ばされていないか。"""
    return "/ap/signin" not in page.url


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def load_dotenv(path=None):
    """リポジトリ直下の .env を環境変数へ読み込む（既存の値は上書きしない）。

    .env は .gitignore 済み。値はここでも呼び出し側でも出力しない。
    """
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


def realistic_user_agent(playwright):
    """実際のエンジンと矛盾しない UA を組み立てる。

    headless の既定 UA には HeadlessChrome が入り、そのままだと
    自動化として検出されうる。文字列を決め打ちするとエンジンの
    バージョンとずれて逆に不自然になるため、起動中のブラウザ自身の
    UA から Headless を外して使う。
    """
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        ua = page.evaluate("navigator.userAgent")
    finally:
        browser.close()
    return ua.replace("HeadlessChrome", "Chrome")


def sign_in(page):
    """DOM に直接入力してサインインする。

    打鍵（キーストローク送出）と違い、入力先の要素を指定するため
    フォーカスが移っても別のウィンドウへ文字が漏れることがない。
    資格情報は環境変数からのみ読み、ファイルにも出力にも残さない。

    Returns:
        (ok, message)
    """
    email = os.environ.get("KINDLE_SHOT_AMAZON_EMAIL", "")
    password = os.environ.get("KINDLE_SHOT_AMAZON_PASSWORD", "")
    if not email or not password:
        # 資格情報が無ければ、表示中のブラウザで人が入力するのを待つ。
        # プロファイルにセッションが残るので、これは初回だけで済む。
        print("ブラウザでサインインしてください（最大 300 秒待ちます）...")
        try:
            page.wait_for_url(lambda u: "/ap/signin" not in u, timeout=300000)
        except Exception:
            return False, "サインインを確認できませんでした"
        return True, "手動サインインを確認しました"

    def fill_first(selectors, value):
        for sel in selectors:
            if page.locator(sel).count():
                page.fill(sel, value)
                return sel
        return None

    if not fill_first(["#ap_email", "#ap_email_login", "input[type=email]"], email):
        return False, "メールアドレス欄が見つかりません"
    for sel in ("#continue", "input#continue", "#continue-announce"):
        if page.locator(sel).count():
            page.click(sel)
            break
    page.wait_for_timeout(4000)

    if not fill_first(["#ap_password", "input[type=password]"], password):
        return False, "パスワード欄が見つかりません"
    for sel in ("#signInSubmit", "input#signInSubmit"):
        if page.locator(sel).count():
            page.click(sel)
            break
    page.wait_for_timeout(6000)

    # 2 段階認証 / CAPTCHA は自動では越えない
    for sel in ("#auth-mfa-otpcode", "#auth-captcha-guess", "input[name=otpCode]"):
        if page.locator(sel).count():
            return False, f"追加認証が要求されました ({sel})"
    if "/ap/signin" in page.url:
        return False, "サインインが完了しませんでした（資格情報の誤りの可能性）"
    return True, "サインインしました"


def run(asin, pages, headless, force_login, out_dir):
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    results: dict = {}

    with sync_playwright() as p:
        need_login = force_login or not os.path.exists(PROFILE_DIR)
        user_agent = realistic_user_agent(p)
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            # 初回サインインは人が操作するので表示する
            headless=headless and not need_login,
            viewport={"width": 1600, "height": 1200},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=user_agent,
            # navigator.webdriver を立てない
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # ログアウト時のライブラリ URL は /ap/signin へ飛ばず、
            # マーケティング用のランディングページを返す（実測）。
            # 本の URL なら確実にサインインへリダイレクトされるので、
            # そちらでログイン状態を判定する。
            page.goto(book_url(asin), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

            if not is_signed_in(page):
                if headless and not need_login:
                    results["error"] = "セッション切れ。--login を付けて再実行してください"
                    return results
                ok, message = sign_in(page)
                results["signin"] = message
                if not ok:
                    results["error"] = message
                    return results
                page.goto(book_url(asin), wait_until="domcontentloaded", timeout=60000)

            page.wait_for_timeout(12000)
            results["user_agent"] = user_agent[:90]
            results["webdriver"] = page.evaluate("navigator.webdriver")
            results["reader_url"] = page.url[:100]
            results["reader_title"] = page.title()

            shots = []
            for i in range(1, pages + 1):
                path = os.path.join(out_dir, f"{i:03d}.png")
                page.screenshot(path=path)
                with open(path, "rb") as f:
                    shots.append((os.path.basename(path), digest(f.read())))
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(2500)

            results["shots"] = shots
            results["distinct"] = len({d for _, d in shots})
            results["out_dir"] = out_dir
        finally:
            ctx.close()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog="headless_spike")
    parser.add_argument("--asin", required=True)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, ".spike-out"))
    parser.add_argument("--login", action="store_true", help="ブラウザを表示してログインする")
    parser.add_argument("--headed", action="store_true", help="headless を使わない（比較用）")
    args = parser.parse_args(argv)
    load_dotenv()

    results = run(
        args.asin, args.pages, headless=not args.headed, force_login=args.login, out_dir=args.out
    )
    for key, value in results.items():
        print(f"{key}: {value}")
    return 1 if "error" in results else 0


if __name__ == "__main__":
    sys.exit(main())
