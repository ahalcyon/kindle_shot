"""CLI をサブプロセスで通しで動かす E2E テスト

既存の tests/test_cli_*.py が cli.main() をインプロセスで呼ぶ「契約テスト」なのに対し、
ここでは実際に `python cli.py ...` を起動し、外部PDF → ページ画像 → トリミング →
PDF の一連の流れを本物のプロセス境界越しに検証する。インプロセス実行では検出できない
次の層をカバーする:

- エントリポイント (cli.py の import 順・DPI 初期化・argparse) が実際に起動すること
- 終了コードがプロセスの exit status として正しく返ること
- パイプ出力時に日本語が UTF-8 で壊れずに出ること (_setup_stdio の実挙動)
- 各コマンドの出力が次のコマンドの入力としてそのまま繋がること
- 生成された PDF が pypdfium2 で読み戻せる実ファイルであること

OCR エンジン (NDLOCR-Lite) とキャプチャ (Win32) は CI に存在しないため、
それらを必要としない経路 (image_pdf) を本流とし、OCR 経路は「利用不可を正しく
報告すること」だけを確認する。

実行:
    pytest -m e2e          # E2E だけ
    pytest -m "not e2e"    # ユニットテストだけ
"""

import json
import os
import subprocess
import sys

import pypdfium2 as pdfium
import pytest
from PIL import Image

pytestmark = pytest.mark.e2e

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(REPO_ROOT, "cli.py")

# 日本語ファイル名でも壊れないことを含めて確認するためのタイトル
BOOK_TITLE = "テスト本"


def run_cli(*argv, timeout=300):
    """cli.py をサブプロセスで実行し、(exit code, events, stdout) を返す。

    常に --json を付けるので stdout は JSON Lines になる。
    """
    proc = subprocess.run(
        [sys.executable, CLI, *argv, "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=timeout,
        # cli._setup_stdio() がパイプ出力を UTF-8 に固定するので UTF-8 で読む
        encoding="utf-8",
        errors="replace",
        # 親の環境変数の PYTHONIOENCODING 等に結果が左右されないようにする
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc.returncode, events, proc.stdout


def by_name(events, name):
    return [e for e in events if e["event"] == name]


def result_of(events):
    results = by_name(events, "result")
    assert len(results) == 1, f"result イベントが 1 件ではない: {events}"
    return results[0]


@pytest.fixture
def source_pdf(tmp_path):
    """余白のある本文3ページ相当の合成PDF。

    600x800 の白紙に、左右 90 / 上下 120 の余白を空けて黒帯を描く。
    trim --auto がページ共通の余白として検出できる構成にしてある。
    """
    path = tmp_path / "source.pdf"
    pages = []
    for i in range(3):
        im = Image.new("RGB", (600, 800), "white")
        # ページごとに帯の高さを変えて、白紙・重複ページ扱いにならないようにする
        im.paste("black", (90, 120, 510, 680 - i * 40))
        pages.append(im)
    pages[0].save(str(path), save_all=True, append_images=pages[1:])
    return path


def pdf_page_count(path):
    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


# ------------------------------------------------------------
# 本流: 外部PDF → 画像 → トリミング → PDF
# ------------------------------------------------------------


def test_pdf_to_trim_to_pdf_roundtrip(source_pdf, tmp_path):
    """外部PDFを読み込んでトリミングし、PDFとして書き戻すまでが通しで動く。"""
    pages_dir = tmp_path / "pages"
    trimmed_dir = tmp_path / "trimmed"
    out_dir = tmp_path / "out"

    # 1) PDF → ページ画像
    code, events, _ = run_cli(
        "pdf", "--in", str(source_pdf), "--out", str(pages_dir), "--dpi", "100"
    )
    assert code == 0, events
    assert result_of(events)["pages"] == 3
    assert sorted(p.name for p in pages_dir.iterdir()) == ["001.png", "002.png", "003.png"]

    # 2) 余白の自動トリミング。前段の出力フォルダをそのまま入力に渡す。
    code, events, _ = run_cli("trim", "--in", str(pages_dir), "--out", str(trimmed_dir), "--auto")
    assert code == 0, events
    detected = by_name(events, "margins_detected")[0]
    # dpi=100 で 600x800px の元PDF (72dpi 基準) は約 833x1111px にレンダリングされる。
    # 実寸に依存しないよう、四辺すべてで余白が削られたことだけを確認する。
    assert all(m > 0 for m in detected["margins"]), detected

    trimmed = sorted(p.name for p in trimmed_dir.iterdir())
    assert trimmed == ["001.png", "002.png", "003.png"]
    with Image.open(pages_dir / "001.png") as before, Image.open(trimmed_dir / "001.png") as after:
        assert after.size[0] < before.size[0]
        assert after.size[1] < before.size[1]

    # 3) トリミング済み画像 → PDF (OCR 不要の image_pdf)
    code, events, _ = run_cli(
        "convert",
        "--in",
        str(trimmed_dir),
        "--out",
        str(out_dir),
        "--format",
        "image_pdf",
        "--name",
        BOOK_TITLE,
    )
    assert code == 0, events
    res = result_of(events)
    assert res["ok"] is True
    assert res["format"] == "image_pdf"

    output_pdf = out_dir / f"{BOOK_TITLE}.pdf"
    assert output_pdf.exists(), f"出力PDFが無い: {sorted(p.name for p in out_dir.iterdir())}"
    # 日本語ファイル名が JSON イベントでも壊れずに返ること
    assert res["output"] == str(output_pdf)
    # 独立したリーダーで読み戻せる、ページ数の合った PDF であること
    assert pdf_page_count(output_pdf) == 3


def test_validate_accepts_the_pipeline_output(source_pdf, tmp_path):
    """パイプラインが吐いた画像フォルダが validate をそのまま通る。"""
    pages_dir = tmp_path / "pages"
    code, _, _ = run_cli("pdf", "--in", str(source_pdf), "--out", str(pages_dir), "--dpi", "100")
    assert code == 0

    code, events, _ = run_cli("validate", "--in", str(pages_dir), "--expect-pages", "3", "--strict")
    assert code == 0, events
    res = result_of(events)
    assert res["ok"] is True
    assert res["pages"] == 3
    assert res["errors"] == []


# ------------------------------------------------------------
# 終了コードの契約（プロセスの exit status として返ること）
# ------------------------------------------------------------


def test_validate_reports_page_shortfall_as_exit_code(source_pdf, tmp_path):
    """期待ページ数に足りなければ EXIT_VALIDATION(7) で終わる。"""
    pages_dir = tmp_path / "pages"
    assert run_cli("pdf", "--in", str(source_pdf), "--out", str(pages_dir), "--dpi", "100")[0] == 0

    code, events, _ = run_cli("validate", "--in", str(pages_dir), "--expect-pages", "10")
    assert code == 7
    assert result_of(events)["ok"] is False


def test_empty_input_folder_exits_no_images(tmp_path):
    """画像が1枚も無いフォルダは EXIT_NO_IMAGES(5) で終わる。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    code, events, _ = run_cli("trim", "--in", str(empty), "--out", str(tmp_path / "o"), "--auto")
    assert code == 5
    assert by_name(events, "error")


def test_bad_margins_argument_exits_bad_args(tmp_path):
    """--margins の書式が不正なら EXIT_BAD_ARGS(2) で終わる。"""
    folder = tmp_path / "pages"
    folder.mkdir()
    Image.new("RGB", (200, 300), "white").save(str(folder / "001.png"))

    code, events, _ = run_cli(
        "trim", "--in", str(folder), "--out", str(tmp_path / "o"), "--margins", "1,2,3"
    )
    assert code == 2
    assert by_name(events, "error")


def test_unknown_command_exits_argparse_usage_error():
    """未知のサブコマンドは argparse の使用法エラー(2)になる。"""
    proc = subprocess.run(
        [sys.executable, CLI, "no-such-command"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()


# ------------------------------------------------------------
# OCR を要する経路（CI にエンジンが無いことを前提に、報告の仕方を固定する）
# ------------------------------------------------------------


def ocr_available():
    proc = subprocess.run(
        [sys.executable, "-c", "from core import ocr_engine; print(ocr_engine.is_available()[0])"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return proc.stdout.strip() == "True"


def test_markdown_conversion_reports_missing_ocr_engine(source_pdf, tmp_path):
    """OCR エンジンが無い環境では EXIT_OCR_UNAVAILABLE(4) と error イベントで報告する。"""
    if ocr_available():
        pytest.skip("OCR エンジンが利用可能な環境では成立しない")

    pages_dir = tmp_path / "pages"
    assert run_cli("pdf", "--in", str(source_pdf), "--out", str(pages_dir), "--dpi", "100")[0] == 0

    code, events, _ = run_cli(
        "convert",
        "--in",
        str(pages_dir),
        "--out",
        str(tmp_path / "out"),
        "--format",
        "markdown",
    )
    assert code == 4
    assert by_name(events, "error"), events


# ------------------------------------------------------------
# 情報系コマンド（エントリポイントの起動確認）
# ------------------------------------------------------------


def test_profiles_lists_builtin_profiles():
    """profiles がプロファイルを列挙し、日本語が壊れずに出力される。"""
    code, events, stdout = run_cli("profiles")
    assert code == 0
    keys = [e["key"] for e in by_name(events, "profile")]
    assert "kindle" in keys
    # UTF-8 で読めている＝置換文字 (U+FFFD) が混ざっていない
    assert "�" not in stdout


def test_help_exits_zero():
    """--help がエントリポイントの import を通って正常終了する。"""
    proc = subprocess.run(
        [sys.executable, CLI, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0
    assert "trim" in proc.stdout
