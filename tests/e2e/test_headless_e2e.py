"""headless 経路の E2E テスト（Amazon に依存しない）

#19 で run / batch の既定が headless になったが、この経路は CI で 1 行も
守られていなかった。実機で踏んだ不具合（位置同期モーダルがキーを吸う、
縦書きで送り向きが逆、読みかけの本が途中から撮れる）はいずれもここにあり、
回帰テストの価値が高い。

**headless の Playwright は CI でも動く。** 試せない理由は画面ではなく
Amazon にログインできないことだけなので、Kindle Cloud Reader の DOM を
模したページをローカルに配信してその依存を外す。

偽リーダーは実機調査で分かった構造を再現している:
    .footer-label.position    読書位置（"27/40ページ ● 68%"）
    ion-alert + ion-backdrop  位置同期モーダル（出ている間はキーを吸う）
    .top-chrome / ion-footer / #kr-scrubber-bar / .kr-chevron-container-*
    縦書き（右→左）: ArrowLeft が前進、ArrowRight が後退
    既定で読みかけ（27 ページ目）から開く
"""

import contextlib
import functools
import http.server
import json
import os
import socket
import subprocess
import sys
import threading

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(REPO_ROOT, "cli.py")
FAKE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_reader")

CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def playwright_available():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not playwright_available(), reason="Playwright が未インストール（headless 経路は試せない）"
    ),
]


@pytest.fixture(scope="module")
def fake_server():
    """偽リーダーを配信する。base URL を返す。"""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass  # テスト出力にアクセスログを混ぜない

    handler = functools.partial(QuietHandler, directory=FAKE_DIR)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def run_cli(*argv, timeout=300):
    """cli.py をサブプロセスで実行し、(exit code, events, CompletedProcess) を返す。"""
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
    return f"returncode={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"


def by_name(events, name):
    return [e for e in events if e.get("event") == name]


def reader_url(base, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{base}/reader.html?{query}" if query else f"{base}/reader.html"


def read_manifest(out, title="hl"):
    with open(os.path.join(out, title, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------
# headless キャプチャ
# ------------------------------------------------------------


def test_captures_from_the_first_page(fake_server, tmp_path):
    """読みかけの本でも先頭から撮る。

    read.amazon.co.jp は前回の読書位置で開く（実機で位置 27 を確認）。
    巻き戻さないと途中から末尾までだけ撮れ、しかも正常終了してしまう。
    偽リーダーも既定で 27 ページ目から開く。
    """
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server),
        "--title",
        "hl",
        "--out",
        out,
        "--max-pages",
        "4",
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)

    rewound = by_name(events, "rewound")
    assert rewound, diag(proc)
    assert rewound[0]["ok"] is True
    assert rewound[0]["position"] == 1

    manifest = read_manifest(out)
    assert manifest["total_pages"] == 4
    assert manifest["stopped_reason"] == "max_pages"


def test_detects_forward_key_for_vertical_writing(fake_server, tmp_path):
    """縦書き（ArrowLeft が前進）を実測で判定する。

    決め打ちだと逆順に撮れてしまい、しかも正常終了する。
    """
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server, forward="ArrowLeft"),
        "--title",
        "hl",
        "--out",
        out,
        "--max-pages",
        "3",
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)
    detected = by_name(events, "page_turn_detected")
    assert detected, diag(proc)
    assert detected[0]["page_turn"] == "left"
    assert read_manifest(out)["page_turn_source"] == "detected"


def test_detects_forward_key_for_horizontal_writing(fake_server, tmp_path):
    """横書き（ArrowRight が前進）でも正しく判定する。"""
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server, forward="ArrowRight"),
        "--title",
        "hl",
        "--out",
        out,
        "--max-pages",
        "3",
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)
    assert by_name(events, "page_turn_detected")[0]["page_turn"] == "right"


def test_dismisses_the_position_sync_modal(fake_server, tmp_path):
    """位置同期モーダルを閉じる。

    実機ではこのモーダルが全面のバックドロップを伴い、出ている間は
    クリックもキー入力も一切通らなかった（1 ページで止まる原因）。
    偽リーダーも同じ挙動を再現している。
    """
    out = str(tmp_path / "out")
    code, _, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server, modal="1"),
        "--title",
        "hl",
        "--out",
        out,
        "--max-pages",
        "3",
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)
    assert read_manifest(out)["total_pages"] == 3


def test_reaches_the_end_of_the_book(fake_server, tmp_path):
    """最終ページまで撮ったら end_of_book で終わる。"""
    out = str(tmp_path / "out")
    code, _, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server, total=5, start=5),
        "--title",
        "hl",
        "--out",
        out,
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)
    manifest = read_manifest(out)
    assert manifest["stopped_reason"] == "end_of_book"
    assert manifest["total_pages"] == 5


def test_captured_pages_are_all_different(fake_server, tmp_path):
    """同じページを撮り続けていないこと。"""
    out = str(tmp_path / "out")
    code, _, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server),
        "--title",
        "hl",
        "--out",
        out,
        "--max-pages",
        "4",
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)
    folder = os.path.join(out, "hl")
    shots = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".png"):
            with open(os.path.join(folder, name), "rb") as f:
                shots.append(f.read())
    assert len(shots) == 4
    assert len(set(shots)) == 4


def test_wrong_direction_at_the_start_is_reported(fake_server, tmp_path):
    """向きを明示して間違えたら no_change で落とす（黙って 1 ページで終えない）。"""
    out = str(tmp_path / "out")
    code, _, proc = run_cli(
        "headless",
        "--url",
        reader_url(fake_server, start=1, forward="ArrowLeft"),
        "--title",
        "hl",
        "--out",
        out,
        "--page-turn",
        "right",
        "--max-pages",
        "3",
        "--load-wait",
        "2",
    )
    assert code != 0, diag(proc)
    assert read_manifest(out)["stopped_reason"] == "no_change"


def test_run_headless_produces_a_pdf(fake_server, tmp_path):
    """run --headless が capture から convert まで通って PDF を作る。"""
    out = str(tmp_path / "out")
    code, events, proc = run_cli(
        "run",
        "--url",
        reader_url(fake_server),
        "--title",
        "hl",
        "--out",
        out,
        "--format",
        "image_pdf",
        "--headless",
        "--max-pages",
        "3",
        "--load-wait",
        "2",
    )
    assert code == 0, diag(proc)
    # headless は本を開く処理がキャプチャに含まれるので open のステップが無い
    summary = by_name(events, "run_summary")[0]
    assert "open" not in summary["steps"]
    assert os.path.exists(os.path.join(out, "hl.pdf"))


# ------------------------------------------------------------
# library
# ------------------------------------------------------------


def test_library_collects_every_book(fake_server, tmp_path):
    """スクロールで追加読み込みされる分まで全件取る。

    実機では初期表示 50 件で、main#library を送ると増えた（405 件で頭打ち）。
    ウィンドウのスクロールでは増えない。
    """
    out = str(tmp_path / "books.json")
    code, _, proc = run_cli(
        "library", "--url", f"{fake_server}/library.html?total=120&batch=50", "--out", str(out)
    )
    assert code == 0, diag(proc)
    with open(out, encoding="utf-8") as f:
        books = json.load(f)
    assert len(books) == 120
    assert set(books[0]) == {"title", "asin"}
    # 保存フォルダ名になるので版表記は落とす
    assert all("Edition)" not in b["title"] for b in books)


def test_library_output_feeds_batch(fake_server, tmp_path):
    """書き出した books.json が batch の検証をそのまま通る。"""
    out = str(tmp_path / "books.json")
    assert (
        run_cli("library", "--url", f"{fake_server}/library.html?total=7", "--out", str(out))[0]
        == 0
    )

    sys.path.insert(0, REPO_ROOT)
    from core import pipeline

    books, code = pipeline.load_batch_file(out, lambda *a, **k: None)
    assert code is None
    assert len(books) == 7
