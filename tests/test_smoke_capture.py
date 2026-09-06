"""scripts/smoke_capture.py のテスト（実機スモークの判定ロジック）

実機実行そのものは動かせないため、副作用のない検証関数だけを固定する。
「同じページが並んでいたら失敗にする」「stopped_reason が max_pages 以外なら
失敗にする」という判定は、スモークが素通りしないための要なのでここで守る。
"""

import importlib.util
import os

import pytest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "smoke_capture.py")
_spec = importlib.util.spec_from_file_location("smoke_capture", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"読み込めない: {_SCRIPT}"
smoke_capture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke_capture)


def manifest(total_pages=3, stopped_reason="max_pages"):
    return {"total_pages": total_pages, "stopped_reason": stopped_reason}


# ------------------------------------------------------------
# check_manifest
# ------------------------------------------------------------


def test_manifest_ok():
    assert smoke_capture.check_manifest(manifest(), 3) == []


def test_manifest_page_count_mismatch():
    problems = smoke_capture.check_manifest(manifest(total_pages=1), 3)
    assert len(problems) == 1
    assert "total_pages" in problems[0]


def test_manifest_timeout_explains_the_likely_cause():
    """本が開けていないケースを、原因の手がかり付きで報告する。"""
    problems = smoke_capture.check_manifest(manifest(stopped_reason="timeout"), 3)
    assert len(problems) == 1
    assert "max_pages" in problems[0]
    assert "本が開けていない" in problems[0]


def test_manifest_reports_both_problems():
    problems = smoke_capture.check_manifest(manifest(total_pages=1, stopped_reason="timeout"), 3)
    assert len(problems) == 2


def test_manifest_missing_fields():
    problems = smoke_capture.check_manifest({}, 3)
    assert len(problems) == 2


# ------------------------------------------------------------
# check_pages_differ
# ------------------------------------------------------------


@pytest.fixture
def pages(tmp_path):
    def make(contents):
        paths = []
        for i, body in enumerate(contents, 1):
            p = tmp_path / f"{i:03d}.png"
            p.write_bytes(body)
            paths.append(str(p))
        return paths

    return make


def test_pages_differ_ok(pages):
    assert smoke_capture.check_pages_differ(pages([b"a", b"b", b"c"])) == []


def test_pages_differ_detects_identical_pages(pages):
    """ページ送りが効いていないと同じ画像が並ぶ。これを失敗にする。"""
    problems = smoke_capture.check_pages_differ(pages([b"same", b"same", b"c"]))
    assert len(problems) == 1
    assert "001.png" in problems[0] and "002.png" in problems[0]
    assert "ページ送りが効いていない" in problems[0]


def test_pages_differ_all_identical(pages):
    problems = smoke_capture.check_pages_differ(pages([b"x", b"x", b"x"]))
    assert len(problems) == 1


def test_pages_differ_empty():
    problems = smoke_capture.check_pages_differ([])
    assert len(problems) == 1
    assert "1 枚も無い" in problems[0]


# ------------------------------------------------------------
# argv / パス
# ------------------------------------------------------------


def test_build_run_argv():
    argv = smoke_capture.build_run_argv("py.exe", "B0TEST", "/out", 3)
    assert argv[0] == "py.exe"
    assert argv[2] == "run"
    # --max-pages を必ず付ける（付け忘れると最終ページまで走り続ける）
    assert "--max-pages" in argv and argv[argv.index("--max-pages") + 1] == "3"
    assert argv[argv.index("--asin") + 1] == "B0TEST"
    # OCR エンジンが無い環境でも変換まで通せる形式を使う
    assert argv[argv.index("--format") + 1] == "image_pdf"
    # 画面を奪わないよう必ず headless で走らせる
    assert "--headless" in argv
    assert "--screen" not in argv
    assert "--json" in argv


def test_paths_follow_run_book_layout():
    """run_book が <out>/<title> と <out>/<title>_trimmed を使う構成に合わせる。"""
    assert smoke_capture.capture_dir("/out") == os.path.join("/out", "smoke")
    assert smoke_capture.trimmed_dir("/out") == os.path.join("/out", "smoke_trimmed")
    assert smoke_capture.output_pdf("/out") == os.path.join("/out", "smoke.pdf")


# ------------------------------------------------------------
# main の引数検証
# ------------------------------------------------------------


def test_main_requires_asin(monkeypatch, capsys):
    """ASIN が無ければ何も実行せず終わる。

    git config を読みに行くので、開発者の設定に左右されないよう差し替える
    （差し替えないと本物のキャプチャが起動してしまう）。
    """
    monkeypatch.setattr(smoke_capture, "smoke_asin_from_git_config", lambda: "")
    assert smoke_capture.main([]) == smoke_capture.EXIT_BAD_ARGS
    err = capsys.readouterr().err
    assert "ASIN" in err
    assert "kindleshot.smokeAsin" in err


def test_asin_comes_from_git_config(monkeypatch):
    """--asin を省いたら git config kindleshot.smokeAsin を使う。"""
    monkeypatch.setattr(smoke_capture, "smoke_asin_from_git_config", lambda: "B0FROMGIT")
    captured = {}

    def fake_run_smoke(asin, out, pages, python=None):
        captured["asin"] = asin
        return []

    monkeypatch.setattr(smoke_capture, "run_smoke", fake_run_smoke)
    assert smoke_capture.main([]) == smoke_capture.EXIT_OK
    assert captured["asin"] == "B0FROMGIT"


def test_main_rejects_single_page(capsys):
    """1 ページではページ送りを確認できないので受け付けない。"""
    assert smoke_capture.main(["--asin", "B0TEST", "--pages", "1"]) == smoke_capture.EXIT_BAD_ARGS
    assert "--pages" in capsys.readouterr().err
