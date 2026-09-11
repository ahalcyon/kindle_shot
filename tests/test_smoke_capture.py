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
    # run は既定で中間ファイルを消すが、スモークは manifest.json と
    # キャプチャ画像を読んで検証するので残させる
    assert "--keep-images" in argv


def test_build_run_argv_for_the_screen_path():
    """--screen は画面キャプチャ経路で走らせる (#50)。

    headless は core/capture_engine.py / core/capture_runner.py /
    core/reader_navigator.py を 1 行も実行しない。この 3 つの回帰を
    捕まえられるのはこちらの経路だけ。
    """
    argv = smoke_capture.build_run_argv("py.exe", "B0TEST", "/out", 3, screen=True)
    assert "--no-headless" in argv
    assert "--headless" not in argv
    # 読み込み待ちは既定 (45 秒) のまま。12 秒は headless 向けの値
    assert "--load-wait" not in argv
    assert "--json" in argv
    assert "--keep-images" in argv


def test_retry_runs_again_after_a_page_turn_failure(tmp_path, monkeypatch):
    """ページが進まなかった失敗は 1 回で確定させない。

    画面キャプチャ経路は実測で 4 回に 1 回ほど、1 ページ目から先へ送れずに
    止まる。1 回で確定させると push のゲートとして使えない。
    """
    calls = []

    def fake(asin, out, pages, python=None, echo=print, screen=False):
        calls.append(screen)
        return (["ページが進まなかった"], True) if len(calls) == 1 else ([], False)

    monkeypatch.setattr(smoke_capture, "run_smoke", fake)
    problems = smoke_capture.run_smoke_with_retry(
        "B0TEST", str(tmp_path), 3, echo=lambda *_: None, screen=True
    )
    assert problems == []
    assert calls == [True, True]


def test_a_deterministic_failure_is_not_retried(tmp_path, monkeypatch):
    """2 回目も同じように落ちる失敗でやり直さない。

    引数の誤りや cli.py の異常終了をやり直すと、--screen 1 本ぶん (30 秒) を
    捨てるだけで、本当に壊れているときの発覚が遅れる。
    """
    calls = []

    def fake(asin, out, pages, python=None, echo=print, screen=False):
        calls.append(1)
        return ["cli.py run が終了コード 2 で失敗"], False

    monkeypatch.setattr(smoke_capture, "run_smoke", fake)
    problems = smoke_capture.run_smoke_with_retry("B0TEST", str(tmp_path), 3, echo=lambda *_: None)
    assert problems == ["cli.py run が終了コード 2 で失敗"]
    assert len(calls) == 1


def test_retry_gives_up_after_the_second_failure(tmp_path, monkeypatch):
    """やり直しても駄目なら失敗として返す。握りつぶさない。"""
    msg = "stopped_reason が max_pages ではなく timeout"
    monkeypatch.setattr(smoke_capture, "run_smoke", lambda *a, **kw: ([msg], True))
    problems = smoke_capture.run_smoke_with_retry("B0TEST", str(tmp_path), 3, echo=lambda *_: None)
    assert problems == [msg]


def test_the_previous_output_is_cleared_before_every_attempt(tmp_path, monkeypatch):
    """毎回、前回の出力を消してから走らせる。

    残っていると「前回の結果」を検証して**誤って通る**。1 回目の前にも消すのは、
    --out に前回の出力が残っていると 1 回目が必ず落ち、やり直しの 1 回を
    そこで使い切ってしまうため。
    """
    out = str(tmp_path)
    os.makedirs(smoke_capture.capture_dir(out))
    os.makedirs(smoke_capture.trimmed_dir(out))
    for path in (
        os.path.join(smoke_capture.capture_dir(out), "manifest.json"),
        smoke_capture.output_pdf(out),
    ):
        with open(path, "w") as f:
            f.write("x")
    seen = []

    def fake(asin, out_, pages, python=None, echo=print, screen=False):
        # 3 つとも消えていること。1 つでも残ると前回の結果を検証しうる
        seen.append(
            [
                os.path.exists(smoke_capture.capture_dir(out_)),
                os.path.exists(smoke_capture.trimmed_dir(out_)),
                os.path.exists(smoke_capture.output_pdf(out_)),
            ]
        )
        return (["ページが進まなかった"], True) if len(seen) == 1 else ([], False)

    monkeypatch.setattr(smoke_capture, "run_smoke", fake)
    smoke_capture.run_smoke_with_retry("B0TEST", out, 3, echo=lambda *_: None)
    assert seen == [[False, False, False], [False, False, False]]


def test_a_timeout_manifest_is_marked_retryable():
    """やり直すかどうかを manifest の値で決める。表示文言に依存しない。

    以前は check_manifest が付ける説明の文字列と照合していた。文言を変えると
    テストは緑のまま、やり直しだけが黙って死ぬ。
    """
    assert smoke_capture.RETRYABLE_REASON == "timeout"
    problems = smoke_capture.check_manifest(
        {"total_pages": 1, "stopped_reason": smoke_capture.RETRYABLE_REASON}, 3
    )
    assert problems


def test_a_cleanup_that_fails_is_reported(tmp_path, monkeypatch):
    """消せなかったことを握り潰さない。

    握り潰すと次の実行が「保存先に既存の画像があります」で落ち、
    引数の問題に見えるエラーになる。
    """
    monkeypatch.setattr(smoke_capture, "clear_output", lambda out: False)
    monkeypatch.setattr(
        smoke_capture, "run_smoke", lambda *a, **kw: pytest.fail("走らせてはいけない")
    )
    problems = smoke_capture.run_smoke_with_retry("B0TEST", str(tmp_path), 3, echo=lambda *_: None)
    assert problems and "消せませんでした" in problems[0]


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

    def fake_run_smoke(asin, out, pages, python=None, echo=print, screen=False):
        captured["asin"] = asin
        return [], False

    monkeypatch.setattr(smoke_capture, "run_smoke", fake_run_smoke)
    assert smoke_capture.main([]) == smoke_capture.EXIT_OK
    assert captured["asin"] == "B0FROMGIT"


def test_main_rejects_single_page(capsys):
    """1 ページではページ送りを確認できないので受け付けない。"""
    assert smoke_capture.main(["--asin", "B0TEST", "--pages", "1"]) == smoke_capture.EXIT_BAD_ARGS
    assert "--pages" in capsys.readouterr().err
