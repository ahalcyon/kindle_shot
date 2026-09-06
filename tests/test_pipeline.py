"""core/pipeline.py のテスト

CLI 経由の統合的な検証は test_cli_contract.py が担う。ここでは
GUI からも直接呼ばれる共有ヘルパーの単体挙動を検証する。
"""

import json
import os

import pytest
from conftest import make_page

from core.pipeline import (
    EXIT_BAD_ARGS,
    EXIT_ERROR,
    EXIT_NO_IMAGES,
    EXIT_OK,
    MANIFEST_NAME,
    check_input_folder,
    clear_output_images,
    read_shot_mode,
    relax_margins,
    remove_intermediates,
    run_trim,
)


def _collect_emit(events):
    def emit(event, human=None, **fields):
        events.append({"event": event, "human": human, **fields})

    return emit


# ------------------------------------------------------------
# relax_margins（トリム緩め方の唯一の式）
# ------------------------------------------------------------


def test_relax_margins_subtracts_safety():
    assert relax_margins((50, 50, 60, 60), safety=8) == (42, 42, 52, 52)


def test_relax_margins_clamps_to_zero():
    assert relax_margins((5, 0, 60, 60), safety=8) == (0, 0, 52, 52)


def test_relax_margins_applies_min_margins():
    # 検出値-8 と min_margins の大きい方
    assert relax_margins((50, 50, 60, 60), safety=8, min_margins=(0, 0, 80, 80)) == (42, 42, 80, 80)


def test_relax_margins_none_min_margins():
    assert relax_margins((50, 50, 60, 60), safety=0) == (50, 50, 60, 60)


# ------------------------------------------------------------
# check_input_folder / clear_output_images
# ------------------------------------------------------------


def test_check_input_folder_ok(pages_folder):
    events: list = []
    assert check_input_folder(str(pages_folder), _collect_emit(events)) is None
    assert events == []


def test_check_input_folder_missing(tmp_path):
    events: list = []
    code = check_input_folder(str(tmp_path / "nope"), _collect_emit(events))
    assert code == EXIT_BAD_ARGS
    assert events[0]["event"] == "error"


def test_check_input_folder_empty(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    events: list = []
    code = check_input_folder(str(empty), _collect_emit(events))
    assert code == EXIT_NO_IMAGES


def test_clear_output_images_blocks_without_overwrite(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_page(out / "old.png")
    events: list = []
    code = clear_output_images(str(out), False, _collect_emit(events))
    assert code == EXIT_BAD_ARGS
    assert "--overwrite" in events[0]["message"]
    assert (out / "old.png").exists()


def test_clear_output_images_removes_with_overwrite(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_page(out / "old.png")
    events: list = []
    code = clear_output_images(str(out), True, _collect_emit(events))
    assert code is None
    assert not (out / "old.png").exists()
    assert events[0]["event"] == "cleaned_output"
    assert events[0]["removed"] == 1


def test_clear_output_images_no_folder_is_ok(tmp_path):
    assert clear_output_images(str(tmp_path / "new"), False) is None


# ------------------------------------------------------------
# run_trim を GUI 相当の呼び方 (emit 省略・タプル引数) で実行
# ------------------------------------------------------------


def test_run_trim_with_explicit_margins_no_emit(pages_folder, tmp_path):
    out = tmp_path / "out"
    code = run_trim(str(pages_folder), str(out), margins=(10, 10, 10, 10), no_check=True)
    assert code == EXIT_OK
    assert sorted(p.name for p in out.glob("*.png")) == [
        "page_001.png",
        "page_002.png",
        "page_003.png",
    ]


def test_run_trim_auto_detect_events(pages_folder, tmp_path):
    events: list = []
    code = run_trim(
        str(pages_folder), str(tmp_path / "out"), margins=None, safety=8, emit=_collect_emit(events)
    )
    assert code == EXIT_OK
    detected = [e for e in events if e["event"] == "margins_detected"][0]
    assert detected["margins"] == [42, 42, 52, 52]
    assert detected["min_margins"] is None
    assert detected["report"]["outliers"] == []
    assert detected["variation_applied"] == [False, False, False, False]
    assert detected["report"]["variation"]["reason"] == "too_few_pages"
    assert "ページ数が 8 枚未満のため、ビューアのUI帯の自動検出は行いません" in detected["human"]
    # 切れるページがなければ passthrough イベントは出ない
    assert not [e for e in events if e["event"] == "passthrough_pages"]


def test_run_trim_reports_applied_and_rejected_variation_sides(tmp_path, monkeypatch):
    from core import boundary_detector

    folder = tmp_path / "pages"
    folder.mkdir()
    filenames = []
    for i in range(1, 9):
        filename = f"page_{i:03d}.png"
        make_page(folder / filename)
        filenames.append(filename)

    content = (202, 520, 202, 23)
    monkeypatch.setattr(
        boundary_detector,
        "folder_page_margins",
        lambda *_args, **_kwargs: [(name, content) for name in filenames],
    )
    monkeypatch.setattr(
        boundary_detector,
        "page_variation_margins",
        lambda *_args, **_kwargs: (
            (202, 606, 202, 721),
            {"sampled": filenames, "skipped": [], "size": [1600, 1200]},
        ),
    )

    events: list = []
    code = run_trim(
        str(folder),
        margins=None,
        safety=0,
        dry_run=True,
        emit=_collect_emit(events),
    )

    assert code == EXIT_OK
    detected = [e for e in events if e["event"] == "margins_detected"][0]
    assert detected["raw"] == [202, 606, 202, 23]
    assert detected["variation_applied"] == [False, True, False, False]
    assert detected["report"]["variation_applied"] == [False, True, False, False]
    assert "UI帯とみられる部分を除去しました (右=606)" in detected["human"]
    assert "下は検出値 721 が画面の 45% を超えるため採用せず" in detected["human"]


def test_run_trim_auto_passes_through_full_bleed_cover(cover_folder, tmp_path):
    """全面表示の表紙が混ざっても自動トリミングが成立し、表紙は無加工で出る。"""
    from PIL import Image

    out = tmp_path / "out"
    events: list = []
    code = run_trim(str(cover_folder), str(out), margins=None, safety=8, emit=_collect_emit(events))
    assert code == EXIT_OK

    detected = [e for e in events if e["event"] == "margins_detected"][0]
    assert detected["margins"] == [42, 42, 52, 52]
    assert detected["report"]["outliers"] == ["cover.png"]

    passthrough = [e for e in events if e["event"] == "passthrough_pages"][0]
    assert passthrough["pages"] == ["cover.png"]

    result = [e for e in events if e["event"] == "result"][0]
    assert result["passthrough"] == 1

    with Image.open(out / "page_001.png") as im:
        assert im.size == (200 - 84, 300 - 104)  # 本文はトリミングされる
    with Image.open(out / "cover.png") as im:
        assert im.size == (200, 300)  # 表紙は元サイズのまま


# ------------------------------------------------------------
# 中間ファイルの削除
# ------------------------------------------------------------


def make_book_output(tmp_path, title="本", pages=3):
    """run_book が作る構成を再現する。"""
    from PIL import Image

    save_dir = tmp_path / title
    trimmed_dir = tmp_path / f"{title}_trimmed"
    save_dir.mkdir()
    trimmed_dir.mkdir()
    for i in range(1, pages + 1):
        Image.new("RGB", (40, 60), "white").save(str(save_dir / f"{i:03d}.png"))
        Image.new("RGB", (30, 50), "white").save(str(trimmed_dir / f"{i:03d}.png"))
    (save_dir / MANIFEST_NAME).write_text('{"total_pages": 3}', encoding="utf-8")
    return save_dir, trimmed_dir


def test_leaves_output_files_alone(tmp_path):
    """出力 PDF は out 直下にあるので巻き込まない。"""
    save_dir, trimmed_dir = make_book_output(tmp_path)
    pdf = tmp_path / "本.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    remove_intermediates(str(save_dir), str(trimmed_dir))

    assert pdf.exists()


def test_reports_only_what_was_actually_deleted(tmp_path, monkeypatch):
    """消せなかったファイルは freed に数えず、例外も外へ出さない。

    Windows では PNG が一時的にロックされることがある（サムネイル生成・
    ウイルス対策・同期クライアント）。そこで例外が抜けると、PDF ができて
    いる本が batch では失敗として記録されてしまう。
    """
    save_dir, trimmed_dir = make_book_output(tmp_path)
    locked = str(save_dir / "001.png")
    real_remove = os.remove

    def flaky_remove(path):
        if str(path) == locked:
            raise PermissionError(13, "使用中")
        real_remove(path)

    total_size = sum(
        os.path.getsize(p) for d in (save_dir, trimmed_dir) for p in d.iterdir() if p.is_file()
    )
    monkeypatch.setattr(os, "remove", flaky_remove)
    events = []
    freed = remove_intermediates(
        str(save_dir), str(trimmed_dir), lambda e, human=None, **f: events.append({"event": e, **f})
    )

    assert os.path.exists(locked)  # 消せなかったものは残る
    assert freed == events[0]["bytes"]
    # 消せた 5 個（save_dir 2 枚 + trimmed 3 枚）+ manifest。
    # 消せなかった 1 枚は件数にもバイト数にも入れない
    assert events[0]["files"] == 6
    assert freed < total_size  # ロックされた 1 枚のぶんだけ少ない
    assert save_dir.exists()  # 中身が残っているので畳まない


def test_removes_images_and_manifest(tmp_path):
    """画像と manifest.json を消し、空になったフォルダを畳む。

    PDF が手元にある利用者にとって manifest.json は読む価値が無く、
    残すと本フォルダが 1KB の JSON 1 個のために生き残ってしまう。
    """
    save_dir, trimmed_dir = make_book_output(tmp_path)
    freed = remove_intermediates(str(save_dir), str(trimmed_dir))

    assert freed > 0
    assert not save_dir.exists()
    assert not trimmed_dir.exists()


def test_keeps_folder_that_still_has_files(tmp_path):
    """利用者が置いたファイルがあるフォルダは畳まない。"""
    save_dir, trimmed_dir = make_book_output(tmp_path)
    (save_dir / "メモ.txt").write_text("あとで読む", encoding="utf-8")

    remove_intermediates(str(save_dir), str(trimmed_dir))

    assert sorted(p.name for p in save_dir.iterdir()) == ["メモ.txt"]


def test_remove_intermediates_reports_freed_bytes(tmp_path):
    save_dir, trimmed_dir = make_book_output(tmp_path)
    events = []
    remove_intermediates(
        str(save_dir), str(trimmed_dir), lambda e, human=None, **f: events.append({"event": e, **f})
    )
    removed = [e for e in events if e["event"] == "intermediates_removed"]
    assert len(removed) == 1
    # manifest.json の分も含めて報告する
    assert removed[0]["bytes"] > 0


def test_remove_intermediates_is_safe_when_missing(tmp_path):
    """フォルダが無くても落ちない。何も消していないならイベントも出さない。"""
    events = []
    freed = remove_intermediates(
        str(tmp_path / "no"),
        str(tmp_path / "none"),
        lambda e, human=None, **f: events.append({"event": e, **f}),
    )
    assert freed == 0
    assert events == []


# ------------------------------------------------------------
# run_book が削除を呼ぶ条件
# ------------------------------------------------------------


@pytest.fixture
def spy_run_book(monkeypatch):
    """run_book の重い工程を潰し、remove_intermediates の呼ばれ方だけ見る。

    run_book はキャプチャ本体とスリープ抑止を関数内 import で取るので、
    pipeline ではなく取り込み元のモジュールに当てる。
    """
    from core import headless_capture, pipeline, win32_utils

    calls = []
    monkeypatch.setattr(pipeline, "remove_intermediates", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(win32_utils, "prevent_sleep", lambda **k: None)
    monkeypatch.setattr(win32_utils, "allow_sleep", lambda: None)
    monkeypatch.setattr(headless_capture, "run_headless_capture", lambda *a, **k: EXIT_OK)
    monkeypatch.setattr(pipeline, "run_validate", lambda *a, **k: EXIT_OK)
    monkeypatch.setattr(pipeline, "run_trim", lambda *a, **k: EXIT_OK)
    return calls


def call_run_book(tmp_path, convert_code, monkeypatch, **kwargs):
    from core import pipeline

    monkeypatch.setattr(pipeline, "run_convert", lambda *a, **k: convert_code)
    return pipeline.run_book(
        asin="B0TEST", title="本", output=str(tmp_path), fmt="image_pdf", **kwargs
    )


def test_run_book_cleans_up_only_on_success(tmp_path, monkeypatch, spy_run_book):
    """PDF ができなかった本の中間ファイルは残す（原因を追えなくなるため）。"""
    assert call_run_book(tmp_path, EXIT_ERROR, monkeypatch) == EXIT_ERROR
    assert spy_run_book == []

    assert call_run_book(tmp_path, EXIT_OK, monkeypatch) == EXIT_OK
    assert len(spy_run_book) == 1


def test_run_book_keep_images_skips_cleanup(tmp_path, monkeypatch, spy_run_book):
    assert call_run_book(tmp_path, EXIT_OK, monkeypatch, keep_images=True) == EXIT_OK
    assert spy_run_book == []


# ------------------------------------------------------------
# 撮影方式に応じたトリミングの既定
# ------------------------------------------------------------


def write_manifest(save_dir, **fields):
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / MANIFEST_NAME).write_text(json.dumps(fields), encoding="utf-8")


def test_read_shot_mode_reads_the_manifest(tmp_path):
    write_manifest(tmp_path / "本", shot_mode="element")
    assert read_shot_mode(str(tmp_path / "本")) == "element"


def test_read_shot_mode_survives_a_missing_or_broken_manifest(tmp_path):
    """manifest が無い/壊れていても落ちない（トリミング側で従来動作に倒す）。"""
    assert read_shot_mode(str(tmp_path / "no")) is None
    broken = tmp_path / "壊"
    broken.mkdir()
    (broken / MANIFEST_NAME).write_text("{", encoding="utf-8")
    assert read_shot_mode(str(broken)) is None


@pytest.fixture
def spy_trim(monkeypatch):
    """run_book が run_trim に渡した引数を捕まえる。"""
    from core import headless_capture, pipeline, win32_utils

    calls = []
    monkeypatch.setattr(pipeline, "remove_intermediates", lambda *a, **k: 0)
    monkeypatch.setattr(win32_utils, "prevent_sleep", lambda **k: None)
    monkeypatch.setattr(win32_utils, "allow_sleep", lambda: None)
    monkeypatch.setattr(pipeline, "run_validate", lambda *a, **k: EXIT_OK)
    monkeypatch.setattr(pipeline, "run_convert", lambda *a, **k: EXIT_OK)

    def record_trim(*_a, **kwargs):
        calls.append(kwargs)
        return EXIT_OK

    monkeypatch.setattr(pipeline, "run_trim", record_trim)
    return calls, headless_capture


def run_book_with_shot_mode(tmp_path, monkeypatch, spy, shot_mode):
    from core import pipeline

    calls, headless_capture = spy
    save_dir = tmp_path / "本"

    def fake_capture(*_a, **_k):
        write_manifest(save_dir, shot_mode=shot_mode)
        return EXIT_OK

    monkeypatch.setattr(headless_capture, "run_headless_capture", fake_capture)
    code = pipeline.run_book(
        asin="B0TEST", title="本", output=str(tmp_path), fmt="image_pdf", headless=True
    )
    assert code == EXIT_OK
    return calls[0]


def test_element_shot_is_not_trimmed(tmp_path, monkeypatch, spy_trim):
    """ページ画像の要素は UI も余白も入っていない。削ると本文を失う。

    実測では本文がページ画像の端まで来ているページが 28% ある
    （B0BVLM8RR2 の先頭 6 ページ、本文 133 行中 37 行）。
    """
    kwargs = run_book_with_shot_mode(tmp_path, monkeypatch, spy_trim, "element")

    assert kwargs["margins"] == (0, 0, 0, 0)
    assert kwargs["min_margins"] == (0, 0, 0, 0)
    assert kwargs["ui_bands"] is False


def test_viewport_shot_still_strips_the_reader_chrome(tmp_path, monkeypatch, spy_trim):
    """ビューポート全体を撮った本は従来どおり書名ヘッダーとフッターを削る。"""
    kwargs = run_book_with_shot_mode(tmp_path, monkeypatch, spy_trim, "viewport")

    assert kwargs["margins"] is None
    assert kwargs["min_margins"] == (0, 0, 80, 80)
    assert kwargs["ui_bands"] is True
