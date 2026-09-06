"""実 Amazon に対する headless 経路の E2E テスト

#19 で run / batch の既定が headless になった。この経路は Kindle Cloud Reader の
DOM に依存しているため、**Amazon 側の構造が変われば壊れる**。偽のページで
代替すると、まさにその変化を検知できない。したがってここでは本物に当てる。

セッションは使い回さず、毎回サインインから通す。ランナーが変われば
セッションは無く、期限切れも起きるため、自動ログインこそ守るべき経路になる。

必要な設定（いずれか欠けたら skip する）:
    KINDLE_SHOT_AMAZON_EMAIL     Amazon のメールアドレス
    KINDLE_SHOT_AMAZON_PASSWORD  パスワード
    KINDLE_SHOT_E2E_ASIN         アカウントが所有する本の ASIN

self-hosted runner での実行を想定している。GitHub ホストのランナーからでも
サインインページには到達できる（実測: status 200 / CAPTCHA 無し）が、
新しい IP からの初回ログインは追加認証が入りうる。
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from core.headless_browser import load_dotenv  # noqa: E402

# ローカルでは .env、CI では Secrets 由来の環境変数から読む。
# load_dotenv は setdefault なので、環境変数があればそちらが優先される。
load_dotenv()

CLI = os.path.join(REPO_ROOT, "cli.py")
PROFILE_DIR = os.path.join(REPO_ROOT, ".playwright-profile")

ASIN = os.environ.get("KINDLE_SHOT_E2E_ASIN", "")
HAS_CREDENTIALS = bool(
    os.environ.get("KINDLE_SHOT_AMAZON_EMAIL") and os.environ.get("KINDLE_SHOT_AMAZON_PASSWORD")
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (ASIN and HAS_CREDENTIALS),
        reason="KINDLE_SHOT_AMAZON_EMAIL / _PASSWORD / KINDLE_SHOT_E2E_ASIN が未設定",
    ),
]

CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


# 毎回ログインし直すと Amazon がパスキー登録を促すダイアログを出し、
# 実行のたびに人の操作を求めてくる（実際に踏んだ）。加えて短時間の
# 連続ログインはアカウントロックの誘因にもなる。
# 既定ではセッションを使い回し、ログイン経路の検証は明示的に行う。
FRESH_LOGIN = os.environ.get("KINDLE_SHOT_E2E_FRESH_LOGIN") == "1"


@pytest.fixture(scope="module", autouse=True)
def session_policy():
    """KINDLE_SHOT_E2E_FRESH_LOGIN=1 のときだけセッションを消す。"""
    if FRESH_LOGIN:
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    yield


def run_cli(*argv, timeout=900):
    proc = subprocess.run(
        [sys.executable, CLI, *argv, "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=CHILD_ENV,
    )
    events = []
    for line in proc.stdout.splitlines():
        if line.strip():
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
    return proc.returncode, events, proc


def diag(proc):
    """失敗時の調査材料。パスワードは argv にも出力にも含まれない。"""
    return f"returncode={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"


def by_name(events, name):
    return [e for e in events if e.get("event") == name]


def read_manifest(out, title):
    with open(os.path.join(out, title, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------
# 自動サインイン + キャプチャ
# ------------------------------------------------------------


@pytest.mark.skipif(not FRESH_LOGIN, reason="KINDLE_SHOT_E2E_FRESH_LOGIN=1 のときだけ実行する")
def test_signs_in_and_captures(tmp_path):
    """セッションが無い状態から、サインインしてページを取得する。

    これが通らなくなったら、Amazon のサインインフォームか
    Cloud Reader の DOM が変わっている。

    毎回走らせるとパスキー登録のダイアログを誘発し、連続ログインは
    アカウントロックの誘因にもなるため、明示的に有効化したときだけ実行する。
    """
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "headless",
        "--asin",
        ASIN,
        "--title",
        "e2e",
        "--out",
        out,
        "--max-pages",
        "3",
        "--load-wait",
        "20",
    )
    assert code == 0, diag(proc)

    # セッションを消して始めたので必ずサインインを通る
    assert by_name(events, "signin"), diag(proc)

    manifest = read_manifest(out, "e2e")
    assert manifest["total_pages"] == 3
    assert manifest["stopped_reason"] == "max_pages"


def test_rewinds_to_the_first_page(tmp_path):
    """先頭ページまで巻き戻してから撮る。

    read.amazon.co.jp は前回の読書位置で開く。巻き戻さないと
    読みかけの本が途中から末尾までだけ撮れ、しかも正常終了してしまう。
    読書位置の数値が読めなくなったらここで落ちる。
    """
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "headless",
        "--asin",
        ASIN,
        "--title",
        "e2e",
        "--out",
        out,
        "--max-pages",
        "2",
        "--load-wait",
        "20",
    )
    assert code == 0, diag(proc)
    rewound = by_name(events, "rewound")
    assert rewound, diag(proc)
    assert rewound[0]["ok"] is True
    assert rewound[0]["position"] == 1


def test_detects_the_page_turn_direction(tmp_path):
    """ページ送りの向きを実測で判定する。

    縦書きでは right が前のページに戻る。判定が効かなくなると
    逆順の本が正常終了で完成するため、無人運用では致命的。
    """
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "headless",
        "--asin",
        ASIN,
        "--title",
        "e2e",
        "--out",
        out,
        "--max-pages",
        "2",
        "--load-wait",
        "20",
    )
    assert code == 0, diag(proc)
    detected = by_name(events, "page_turn_detected")
    assert detected, diag(proc)
    assert detected[0]["page_turn"] in ("left", "right")
    assert read_manifest(out, "e2e")["page_turn_source"] == "detected"


def test_captured_pages_are_all_different(tmp_path):
    """同じページを撮り続けていないこと（ページ送りが効いている）。"""
    out = str(tmp_path / "out")
    code, _, proc = run_cli(
        "headless",
        "--asin",
        ASIN,
        "--title",
        "e2e",
        "--out",
        out,
        "--max-pages",
        "3",
        "--load-wait",
        "20",
    )
    assert code == 0, diag(proc)
    folder = os.path.join(out, "e2e")
    shots = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".png"):
            with open(os.path.join(folder, name), "rb") as f:
                shots.append(f.read())
    assert len(shots) == 3
    assert len(set(shots)) == 3


def test_run_produces_a_pdf(tmp_path):
    """run が capture から convert まで通って PDF を作る。"""
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "run",
        "--asin",
        ASIN,
        "--title",
        "e2e",
        "--out",
        out,
        "--format",
        "image_pdf",
        "--max-pages",
        "2",
        "--load-wait",
        "20",
    )
    assert code == 0, diag(proc)
    # headless は本を開く処理がキャプチャに含まれるので open のステップが無い
    assert "open" not in by_name(events, "run_summary")[0]["steps"]
    assert os.path.exists(os.path.join(out, "e2e.pdf"))


# ------------------------------------------------------------
# library
# ------------------------------------------------------------


def test_library_dump_feeds_batch(tmp_path):
    """蔵書一覧を取得し、その出力が batch の検証を通る。

    ライブラリの DOM（coverContainer-<ASIN> と main#library のスクロール）が
    変わったらここで落ちる。
    """
    out = str(tmp_path / "books.json")
    code, _, proc = run_cli("library", "--out", str(out))
    assert code == 0, diag(proc)

    with open(out, encoding="utf-8") as f:
        books = json.load(f)
    assert len(books) > 0
    assert set(books[0]) == {"title", "asin"}
    assert len({b["asin"] for b in books}) == len(books)

    sys.path.insert(0, REPO_ROOT)
    from core import pipeline

    loaded, code = pipeline.load_batch_file(str(out), lambda *a, **k: None)
    assert code is None
    assert len(loaded) == len(books)
