"""core/pipeline.py のテスト

CLI 経由の統合的な検証は test_cli_contract.py が担う。ここでは
GUI からも直接呼ばれる共有ヘルパーの単体挙動を検証する。
"""

from conftest import make_page

from core.pipeline import (
    EXIT_BAD_ARGS,
    EXIT_NO_IMAGES,
    EXIT_OK,
    MANIFEST_NAME,
    check_input_folder,
    clear_output_images,
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
    """フォルダが無くても落ちない。"""
    assert remove_intermediates(str(tmp_path / "no"), str(tmp_path / "none")) == 0
