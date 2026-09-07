"""batch コマンドの契約テスト（複数冊の一括実行）

Win32 実機依存の run_book はスタブに差し替え、
- load_batch_file: JSON の検証（未知キー・重複タイトル・asin/url 必須・形式）
- run_batch: スキップ再開・失敗続行・stop-on-error・本ごとの設定上書き
- cli batch: JSON Lines イベントと終了コード
を固定する。
"""

import json
import os

import cli
from core import pipeline

# ------------------------------------------------------------
# ヘルパー
# ------------------------------------------------------------


def collect_emit():
    """emit と、記録先の events リストを返す。"""
    events = []

    def emit(event, human=None, **fields):
        events.append({"event": event, **fields})

    return emit, events


def by_name(events, name):
    return [e for e in events if e["event"] == name]


def make_signin_failing_run_book(calls, fail_titles=()):
    """指定タイトルで signin_required を出して失敗する run_book の代役。"""

    def fake(**kwargs):
        calls.append(kwargs)
        title = kwargs["title"]
        if title in fail_titles:
            kwargs["emit"]("signin_required", human="ログアウトされています")
            return pipeline.EXIT_WINDOW_NOT_FOUND
        out = kwargs["output"]
        os.makedirs(out, exist_ok=True)
        with open(
            os.path.join(out, pipeline._ensure_ext(title, ".pdf")), "w", encoding="utf-8"
        ) as f:
            f.write("x")
        return pipeline.EXIT_OK

    return fake


def make_fake_run_book(calls, fail_titles=()):
    """run_book の代役。呼び出しを記録し、出力ファイルを作って EXIT_OK を返す。

    fail_titles に含まれるタイトルはウィンドウ未検出として失敗させる。
    """

    def fake(**kwargs):
        calls.append(kwargs)
        title = kwargs["title"]
        if title in fail_titles:
            return pipeline.EXIT_WINDOW_NOT_FOUND
        out = kwargs["output"]
        fmt = kwargs.get("fmt", "searchable_pdf")
        ext = ".md" if fmt == "markdown" else ".pdf"
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, pipeline._ensure_ext(title, ext)), "w", encoding="utf-8") as f:
            f.write("x")
        return pipeline.EXIT_OK

    return fake


def write_books(tmp_path, data, name="books.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------
# load_batch_file: 検証
# ------------------------------------------------------------


def test_load_valid_array_resolves_titles(tmp_path):
    path = write_books(
        tmp_path,
        [
            {"asin": "B0ABC", "title": "吾輩は猫である"},
            {"asin": "B0DEF"},  # title 省略 → asin を代用
        ],
    )
    emit, events = collect_emit()
    books, code = pipeline.load_batch_file(path, emit)
    assert code is None
    assert [b["title"] for b in books] == ["吾輩は猫である", "B0DEF"]
    assert not by_name(events, "error")


def test_load_books_wrapper_form(tmp_path):
    path = write_books(tmp_path, {"books": [{"asin": "B0ABC", "title": "T"}]})
    books, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code is None
    assert len(books) == 1


def test_load_missing_file():
    _, code = pipeline.load_batch_file("does_not_exist.json", lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    emit, events = collect_emit()
    books, code = pipeline.load_batch_file(str(path), emit)
    assert books is None
    assert code == cli.EXIT_BAD_ARGS
    assert by_name(events, "error")


def test_load_empty_list(tmp_path):
    path = write_books(tmp_path, [])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_unknown_key_is_rejected(tmp_path):
    path = write_books(tmp_path, [{"asin": "B0ABC", "max_page": 100}])
    emit, events = collect_emit()
    _, code = pipeline.load_batch_file(path, emit)
    assert code == cli.EXIT_BAD_ARGS
    assert any("max_page" in e["message"] for e in by_name(events, "error"))


def test_load_requires_asin_or_url(tmp_path):
    path = write_books(tmp_path, [{"title": "タイトルのみ"}])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_url_without_title_is_rejected(tmp_path):
    path = write_books(tmp_path, [{"url": "https://example.com/book"}])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_duplicate_titles_rejected(tmp_path):
    path = write_books(
        tmp_path,
        [
            {"asin": "B0ABC", "title": "同名"},
            {"asin": "B0DEF", "title": "同名"},
        ],
    )
    emit, events = collect_emit()
    _, code = pipeline.load_batch_file(path, emit)
    assert code == cli.EXIT_BAD_ARGS
    assert any("重複" in e["message"] for e in by_name(events, "error"))


def test_load_bad_format_value(tmp_path):
    path = write_books(tmp_path, [{"asin": "B0ABC", "format": "docx"}])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_min_margins_accepts_array_and_string(tmp_path):
    path = write_books(
        tmp_path,
        [
            {"asin": "B0A", "title": "配列", "min_margins": [0, 0, 80, 80]},
            {"asin": "B0B", "title": "文字列", "min_margins": "0,0,80,80"},
        ],
    )
    books, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code is None
    assert books[0]["min_margins"] == (0, 0, 80, 80)
    assert books[1]["min_margins"] == (0, 0, 80, 80)


def test_load_ui_bands_accepts_bool(tmp_path):
    path = write_books(tmp_path, [{"asin": "B0A", "ui_bands": False}])
    books, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code is None
    assert books[0]["ui_bands"] is False


def test_load_ui_bands_rejects_non_bool(tmp_path):
    path = write_books(tmp_path, [{"asin": "B0A", "ui_bands": "no"}])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_bool_not_accepted_as_int(tmp_path):
    # max_pages に true を渡す → bool は整数扱いしない
    path = write_books(tmp_path, [{"asin": "B0A", "max_pages": True}])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_page_turn_accepts_pagedown(tmp_path):
    # right/left 以外のページ送りキー（ブラウザ型ビューアの上下めくり）も通る
    path = write_books(tmp_path, [{"asin": "B0A", "page_turn": "pagedown"}])
    books, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code is None
    assert books[0]["page_turn"] == "pagedown"


def test_load_page_turn_rejects_unknown_key(tmp_path):
    path = write_books(tmp_path, [{"asin": "B0A", "page_turn": "enter"}])
    _, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code == cli.EXIT_BAD_ARGS


def test_load_split_words_accepted(tmp_path):
    path = write_books(tmp_path, [{"asin": "B0A", "split_words": 450000}])
    books, code = pipeline.load_batch_file(path, lambda *a, **k: None)
    assert code is None
    assert books[0]["split_words"] == 450000


# ------------------------------------------------------------
# run_batch: 実行制御（run_book はスタブ）
# ------------------------------------------------------------


def test_run_batch_happy_path(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    out = tmp_path / "out"
    books = [{"asin": "B0A", "title": "本1"}, {"asin": "B0B", "title": "本2"}]

    emit, events = collect_emit()
    code = pipeline.run_batch(books, output=str(out), defaults={"fmt": "searchable_pdf"}, emit=emit)
    assert code == cli.EXIT_OK
    assert len(calls) == 2

    start = by_name(events, "batch_start")[0]
    assert start["total_books"] == 2
    assert len(by_name(events, "book_start")) == 2
    results = by_name(events, "book_result")
    assert all(r["ok"] for r in results)

    summary = by_name(events, "batch_summary")[0]
    assert summary["ok"] is True
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0
    assert summary["skipped"] == 0


def test_run_batch_skips_completed_books(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    out = tmp_path / "out"
    out.mkdir()
    # 本1 は既に出力済み（.pdf）→ スキップされるはず
    (out / "本1.pdf").write_text("done", encoding="utf-8")
    books = [{"asin": "B0A", "title": "本1"}, {"asin": "B0B", "title": "本2"}]

    emit, events = collect_emit()
    code = pipeline.run_batch(books, output=str(out), defaults={"fmt": "searchable_pdf"}, emit=emit)
    assert code == cli.EXIT_OK
    # run_book は 本2 についてのみ呼ばれる
    assert [c["title"] for c in calls] == ["本2"]
    assert by_name(events, "book_skipped")[0]["title"] == "本1"
    summary = by_name(events, "batch_summary")[0]
    assert summary["skipped"] == 1
    assert summary["succeeded"] == 1


def test_run_batch_overwrite_reprocesses_completed(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    out = tmp_path / "out"
    out.mkdir()
    (out / "本1.pdf").write_text("done", encoding="utf-8")
    books = [{"asin": "B0A", "title": "本1"}]

    emit, events = collect_emit()
    code = pipeline.run_batch(
        books, output=str(out), overwrite=True, defaults={"fmt": "searchable_pdf"}, emit=emit
    )
    assert code == cli.EXIT_OK
    assert [c["title"] for c in calls] == ["本1"]  # スキップされず再処理
    assert not by_name(events, "book_skipped")


def test_run_batch_continues_on_failure(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls, fail_titles={"本2"}))
    out = tmp_path / "out"
    books = [
        {"asin": "B0A", "title": "本1"},
        {"asin": "B0B", "title": "本2"},
        {"asin": "B0C", "title": "本3"},
    ]

    emit, events = collect_emit()
    code = pipeline.run_batch(books, output=str(out), defaults={"fmt": "searchable_pdf"}, emit=emit)
    assert code == cli.EXIT_ERROR  # 1冊失敗 → 非0
    assert len(calls) == 3  # 失敗後も続行

    summary = by_name(events, "batch_summary")[0]
    assert summary["succeeded"] == 2
    assert summary["failed"] == 1
    assert summary["unprocessed"] == 0
    failed = [r for r in summary["results"] if not r["ok"]]
    assert failed[0]["title"] == "本2"
    assert failed[0]["exit_code"] == cli.EXIT_WINDOW_NOT_FOUND


def test_run_batch_stop_on_error(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls, fail_titles={"本2"}))
    out = tmp_path / "out"
    books = [
        {"asin": "B0A", "title": "本1"},
        {"asin": "B0B", "title": "本2"},
        {"asin": "B0C", "title": "本3"},
    ]

    emit, events = collect_emit()
    code = pipeline.run_batch(
        books, output=str(out), stop_on_error=True, defaults={"fmt": "searchable_pdf"}, emit=emit
    )
    assert code == cli.EXIT_ERROR
    assert [c["title"] for c in calls] == ["本1", "本2"]  # 本3 は未処理

    summary = by_name(events, "batch_summary")[0]
    assert summary["failed"] == 1
    assert summary["unprocessed"] == 1


def test_run_batch_per_book_overrides_defaults(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    out = tmp_path / "out"
    # 全体既定は searchable_pdf、本2 だけ markdown に上書き
    books = [
        {"asin": "B0A", "title": "本1"},
        {"asin": "B0B", "title": "本2", "fmt": "markdown", "page_turn": "left"},
    ]

    pipeline.run_batch(
        books,
        output=str(out),
        defaults={"fmt": "searchable_pdf", "page_turn": None},
        emit=lambda *a, **k: None,
    )
    assert calls[0]["fmt"] == "searchable_pdf"
    assert calls[1]["fmt"] == "markdown"
    assert calls[1]["page_turn"] == "left"
    # 本2 の出力は .md
    assert (out / "本2.md").exists()


def test_run_batch_skip_uses_per_book_format_extension(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    out = tmp_path / "out"
    out.mkdir()
    # markdown 指定の本は .md が既にあればスキップされる
    (out / "本M.md").write_text("done", encoding="utf-8")
    books = [{"asin": "B0M", "title": "本M", "fmt": "markdown"}]

    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(out), emit=emit)
    assert calls == []
    assert by_name(events, "book_skipped")[0]["title"] == "本M"


def test_run_batch_skip_recognizes_split_markdown_output(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    out = tmp_path / "out"
    out.mkdir()
    # split_words による分割出力（<title>_1.md）でも完成済みとしてスキップされる
    (out / "本S_1.md").write_text("done", encoding="utf-8")
    books = [{"asin": "B0S", "title": "本S", "fmt": "markdown", "split_words": 450000}]

    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(out), emit=emit)
    assert calls == []
    skipped = by_name(events, "book_skipped")[0]
    assert skipped["title"] == "本S"
    assert skipped["output"].endswith("本S_1.md")


# ------------------------------------------------------------
# cli batch: 統合（--json）
# ------------------------------------------------------------


def test_cli_batch_json(tmp_path, monkeypatch, isolated_config, capsys):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    path = write_books(
        tmp_path,
        [
            {"asin": "B0A", "title": "本1"},
            {"asin": "B0B", "title": "本2"},
        ],
    )
    out = tmp_path / "out"

    code = cli.main(["batch", "--books", path, "--out", str(out), "--json"])
    assert code == cli.EXIT_OK

    lines = capsys.readouterr().out.splitlines()
    events = [json.loads(x) for x in lines if x.strip()]
    assert by_name(events, "batch_start")[0]["total_books"] == 2
    assert by_name(events, "batch_summary")[0]["succeeded"] == 2
    # CLI の --format 既定（searchable_pdf）が全本に渡る
    assert all(c["fmt"] == "searchable_pdf" for c in calls)


def test_cli_batch_ui_bands_default_and_flag(tmp_path, monkeypatch, isolated_config, capsys):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    path = write_books(tmp_path, [{"asin": "B0A", "title": "本1"}])

    code = cli.main(
        [
            "batch",
            "--books",
            path,
            "--out",
            str(tmp_path / "out_default"),
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    assert calls[-1]["ui_bands"] is True

    code = cli.main(
        [
            "batch",
            "--books",
            path,
            "--out",
            str(tmp_path / "out_disabled"),
            "--no-ui-bands",
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    assert calls[-1]["ui_bands"] is False
    capsys.readouterr()


def test_cli_batch_bad_file_returns_bad_args(tmp_path, isolated_config, capsys):
    code = cli.main(
        ["batch", "--books", str(tmp_path / "nope.json"), "--out", str(tmp_path / "out"), "--json"]
    )
    assert code == cli.EXIT_BAD_ARGS
    events = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert by_name(events, "error")


# ------------------------------------------------------------
# ログアウト検出でのバッチ中断
# ------------------------------------------------------------


def test_batch_aborts_when_signed_out(tmp_path, monkeypatch):
    """ログアウトを検出したら残りの本を試さずに中断する。

    以後の全冊が確実に同じ理由で失敗するため、走り続けると Amazon へ
    失敗ログインを冊数分投げることになり、アカウントロックを招く。
    """
    calls: list = []
    monkeypatch.setattr(
        pipeline, "run_book", make_signin_failing_run_book(calls, fail_titles=("B",))
    )
    books = [
        {"title": "A", "asin": "B01"},
        {"title": "B", "asin": "B02"},
        {"title": "C", "asin": "B03"},
    ]
    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)

    # A は成功、B で中断するので C は試さない
    assert [c["title"] for c in calls] == ["A", "B"]
    assert any("ログアウト" in e.get("message", "") for e in by_name(events, "error"))
    summary = by_name(events, "batch_summary")[0]
    assert summary["unprocessed"] == 1


def test_batch_continues_on_ordinary_failure(tmp_path, monkeypatch):
    """ログアウト以外の失敗では従来どおり次の本へ進む。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls, fail_titles=("B",)))
    books = [
        {"title": "A", "asin": "B01"},
        {"title": "B", "asin": "B02"},
        {"title": "C", "asin": "B03"},
    ]
    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)
    assert [c["title"] for c in calls] == ["A", "B", "C"]


# ------------------------------------------------------------
# headless の既定（プロファイルで決まる）
# ------------------------------------------------------------


def capture_headless_flag(monkeypatch):
    """run_book が headless 分岐に入ったかを記録する。"""
    seen = {}

    def fake_headless_capture(*args, **kwargs):
        seen["headless"] = True
        return pipeline.EXIT_ERROR

    def fake_open_book(*args, **kwargs):
        seen["headless"] = False
        return pipeline.EXIT_ERROR

    monkeypatch.setattr("core.headless_capture.run_headless_capture", fake_headless_capture)
    monkeypatch.setattr("core.reader_navigator.open_book", fake_open_book)
    return seen


def test_cloud_reader_defaults_to_headless(tmp_path, monkeypatch):
    """kindle_cloud では headless が既定。

    画面もセッションも不要で通知の写り込みも無いため、画面キャプチャ経路の
    上位互換になっている。わざわざ劣る方を既定にしない。
    """
    seen = capture_headless_flag(monkeypatch)
    pipeline.run_book(title="t", output=str(tmp_path), profile_key="kindle_cloud", asin="B0X")
    assert seen["headless"] is True


def test_other_profiles_stay_on_screen_capture(tmp_path, monkeypatch):
    """headless は read.amazon.co.jp 専用実装なので他ビューアでは使わない。"""
    seen = capture_headless_flag(monkeypatch)
    pipeline.run_book(title="t", output=str(tmp_path), profile_key="kobo_web", url="https://x")
    assert seen["headless"] is False


def test_explicit_no_headless_overrides_the_default(tmp_path, monkeypatch):
    seen = capture_headless_flag(monkeypatch)
    pipeline.run_book(
        title="t", output=str(tmp_path), profile_key="kindle_cloud", asin="B0X", headless=False
    )
    assert seen["headless"] is False


def test_explicit_headless_overrides_the_default(tmp_path, monkeypatch):
    seen = capture_headless_flag(monkeypatch)
    pipeline.run_book(
        title="t", output=str(tmp_path), profile_key="kobo_web", url="https://x", headless=True
    )
    assert seen["headless"] is True


# ------------------------------------------------------------
# Cloud Reader 非対応の本 (#42)
# ------------------------------------------------------------


def make_unsupported_run_book(calls, unsupported_titles=(), fail_titles=()):
    """指定タイトルを Cloud Reader 非対応として失敗させる run_book の代役。"""

    def fake(**kwargs):
        calls.append(kwargs)
        title = kwargs["title"]
        if title in unsupported_titles:
            kwargs["emit"]("book_unsupported", human="対応していません")
            return pipeline.EXIT_UNSUPPORTED_BOOK
        if title in fail_titles:
            return pipeline.EXIT_ERROR
        out = kwargs["output"]
        os.makedirs(out, exist_ok=True)
        with open(
            os.path.join(out, pipeline._ensure_ext(title, ".pdf")), "w", encoding="utf-8"
        ) as f:
            f.write("x")
        return pipeline.EXIT_OK

    return fake


def test_unsupported_book_does_not_stop_the_batch(tmp_path, monkeypatch):
    """非対応はこの本に固有なので、残りの本は最後まで処理する。"""
    calls: list = []
    monkeypatch.setattr(
        pipeline, "run_book", make_unsupported_run_book(calls, unsupported_titles=("B",))
    )
    books = [
        {"title": "A", "asin": "B01"},
        {"title": "B", "asin": "B02"},
        {"title": "C", "asin": "B03"},
    ]
    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)
    assert [c["title"] for c in calls] == ["A", "B", "C"]


def test_unsupported_book_is_counted_apart_from_failures(tmp_path, monkeypatch):
    """非対応は失敗に混ぜない。混ぜると終了コードが常に非0になって使えなくなる。"""
    calls: list = []
    monkeypatch.setattr(
        pipeline, "run_book", make_unsupported_run_book(calls, unsupported_titles=("B",))
    )
    books = [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}]
    events = []

    def emit(event, human=None, **fields):
        events.append({"event": event, "human": human, **fields})

    code = pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)

    summary = by_name(events, "batch_summary")[0]
    assert summary["unsupported"] == 1
    assert summary["failed"] == 0
    assert summary["succeeded"] == 1
    assert summary["ok"] is True
    assert code == pipeline.EXIT_OK
    # 人間向けの一覧にも、失敗ではなく非対応として出る
    assert "非対応" in summary["human"]
    assert "B02" in summary["human"]


def test_real_failure_still_fails_the_batch(tmp_path, monkeypatch):
    """非対応と本当の失敗が混ざっても、失敗のほうは終了コードに出る。"""
    calls: list = []
    monkeypatch.setattr(
        pipeline,
        "run_book",
        make_unsupported_run_book(calls, unsupported_titles=("B",), fail_titles=("C",)),
    )
    books = [
        {"title": "A", "asin": "B01"},
        {"title": "B", "asin": "B02"},
        {"title": "C", "asin": "B03"},
    ]
    emit, events = collect_emit()
    code = pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)

    summary = by_name(events, "batch_summary")[0]
    assert summary["unsupported"] == 1
    assert summary["failed"] == 1
    assert code == pipeline.EXIT_ERROR


def test_stop_on_error_does_not_trigger_on_unsupported(tmp_path, monkeypatch):
    """--stop-on-error でも非対応では止めない。

    蔵書 405 冊中 63 冊が非対応なので、ここで止めると最初の 1 冊に当たった
    時点で残り全部が未処理になる。
    """
    calls: list = []
    monkeypatch.setattr(
        pipeline, "run_book", make_unsupported_run_book(calls, unsupported_titles=("A",))
    )
    books = [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}]
    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(tmp_path / "out"), stop_on_error=True, emit=emit)
    assert [c["title"] for c in calls] == ["A", "B"]


def test_systemic_unsupported_is_not_reported_as_success(tmp_path, monkeypatch):
    """全部が非対応なら、本ごとの理由ではなく環境の障害を疑う。

    ブラウザや UA が弾かれると全冊が同じダイアログで止まりうる。非対応は
    終了コードに出ないので、1 冊ずつ片付けると蔵書すべてを黙って取りこぼす。
    """
    calls: list = []
    monkeypatch.setattr(
        pipeline,
        "run_book",
        make_unsupported_run_book(calls, unsupported_titles=("A", "B", "C")),
    )
    books = [
        {"title": "A", "asin": "B01"},
        {"title": "B", "asin": "B02"},
        {"title": "C", "asin": "B03"},
    ]
    events = []

    def emit(event, human=None, **fields):
        events.append({"event": event, "human": human, **fields})

    code = pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)

    summary = by_name(events, "batch_summary")[0]
    assert summary["systemic_unsupported"] is True
    assert summary["ok"] is False
    assert code == pipeline.EXIT_ERROR
    assert "1 冊残らず非対応" in summary["human"]
    # books.json から外せ、という案内は出さない（外すべきではないため）
    assert "books.json" not in summary["human"]


def test_one_unsupported_book_is_not_treated_as_systemic(tmp_path, monkeypatch):
    """普通に非対応が混ざっただけなら従来どおり成功扱い。"""
    calls: list = []
    monkeypatch.setattr(
        pipeline, "run_book", make_unsupported_run_book(calls, unsupported_titles=("B",))
    )
    books = [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}]
    events = []

    def emit(event, human=None, **fields):
        events.append({"event": event, "human": human, **fields})

    code = pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)
    summary = by_name(events, "batch_summary")[0]
    assert summary["systemic_unsupported"] is False
    assert code == pipeline.EXIT_OK
    assert "books.json" in summary["human"]


def test_book_result_agrees_with_the_summary(tmp_path, monkeypatch):
    """1冊ずつのイベントとサマリで判定が食い違わないようにする。

    405 冊のログを流し見るときに見るのは book_result なので、非対応を NG と
    書くとサマリの「失敗 0」と矛盾する。
    """
    calls: list = []
    monkeypatch.setattr(
        pipeline, "run_book", make_unsupported_run_book(calls, unsupported_titles=("B",))
    )
    books = [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}]
    events = []

    def emit(event, human=None, **fields):
        events.append({"event": event, "human": human, **fields})

    pipeline.run_batch(books, output=str(tmp_path / "out"), emit=emit)

    a, b = by_name(events, "book_result")
    assert a["unsupported"] is False
    assert b["unsupported"] is True
    assert "NG" not in b["human"]
    assert "非対応" in b["human"]


def test_every_result_carries_the_unsupported_key(tmp_path, monkeypatch):
    """results は batch_summary の公開ペイロード。行ごとに形が変わらないようにする。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_unsupported_run_book(calls))
    out = tmp_path / "out"
    books = [{"title": "A", "asin": "B01"}]
    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(out), emit=emit)
    # 2 回目は完成済みとしてスキップされる経路を通る
    emit, events = collect_emit()
    pipeline.run_batch(books, output=str(out), emit=emit)

    results = by_name(events, "batch_summary")[0]["results"]
    assert results[0]["skipped"] is True
    assert all("unsupported" in r for r in results)


def test_known_unsupported_book_is_skipped_on_rerun(tmp_path, monkeypatch):
    """一度非対応と分かった本は開き直さない。

    蔵書 405 冊のうち 63 冊が非対応で、毎回 20 秒ずつ開き直すと 1 回の再実行で
    21 分を捨てる。判定は前回のキャプチャが manifest に残した stopped_reason。
    """
    out = tmp_path / "out"
    save_dir = out / "A"
    os.makedirs(save_dir)
    with open(save_dir / pipeline.MANIFEST_NAME, "w", encoding="utf-8") as f:
        json.dump({"stopped_reason": pipeline.UNSUPPORTED_STOPPED_REASON}, f)

    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_unsupported_run_book(calls))
    emit, events = collect_emit()
    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}],
        output=str(out),
        emit=emit,
    )

    # A は開かない。B だけ実行する
    assert [c["title"] for c in calls] == ["B"]
    assert by_name(events, "book_skipped")[0]["reason"] == "unsupported"
    summary = by_name(events, "batch_summary")[0]
    # 非対応として 1 度だけ数える（スキップと二重に数えない）
    assert summary["unsupported"] == 1
    assert summary["skipped"] == 0
    assert summary["succeeded"] == 1
    assert code == pipeline.EXIT_OK


def test_overwrite_reopens_a_known_unsupported_book(tmp_path, monkeypatch):
    """--overwrite なら、非対応の判定が変わっていないか確かめ直せる。"""
    out = tmp_path / "out"
    save_dir = out / "A"
    os.makedirs(save_dir)
    with open(save_dir / pipeline.MANIFEST_NAME, "w", encoding="utf-8") as f:
        json.dump({"stopped_reason": pipeline.UNSUPPORTED_STOPPED_REASON}, f)

    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_unsupported_run_book(calls))
    emit, events = collect_emit()
    pipeline.run_batch([{"title": "A", "asin": "B01"}], output=str(out), overwrite=True, emit=emit)
    assert [c["title"] for c in calls] == ["A"]


def test_skipped_unsupported_books_do_not_look_systemic(tmp_path, monkeypatch):
    """前回の記録で読み飛ばした本は、今回の環境が壊れている証拠にならない。"""
    out = tmp_path / "out"
    for title in ("A", "B"):
        os.makedirs(out / title)
        with open(out / title / pipeline.MANIFEST_NAME, "w", encoding="utf-8") as f:
            json.dump({"stopped_reason": pipeline.UNSUPPORTED_STOPPED_REASON}, f)

    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_unsupported_run_book(calls))
    emit, events = collect_emit()
    code = pipeline.run_batch(
        [
            {"title": "A", "asin": "B01"},
            {"title": "B", "asin": "B02"},
            {"title": "C", "asin": "B03"},
        ],
        output=str(out),
        emit=emit,
    )
    summary = by_name(events, "batch_summary")[0]
    assert summary["unsupported"] == 2
    assert summary["systemic_unsupported"] is False
    assert code == pipeline.EXIT_OK


# ------------------------------------------------------------
# 空き容量ガード (#48)
# ------------------------------------------------------------


def fake_free(monkeypatch, *values):
    """smallest_free の戻り値を呼び出し順に差し替える。最後の値を以後も返す。"""
    assert values, "少なくとも 1 つの値が要る"
    seq = list(values)

    def fake(_paths):
        free = seq.pop(0) if len(seq) > 1 else seq[0]
        return ("D:", free)

    monkeypatch.setattr(pipeline, "smallest_free", fake)


def test_low_disk_stops_before_touching_any_book(tmp_path, monkeypatch):
    """空きが下限を割っていたら 1 冊も開かずに止まる。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    fake_free(monkeypatch, 1 * 1024**3)
    emit, events = collect_emit()

    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}],
        output=str(tmp_path / "out"),
        min_free_bytes=5 * 1024**3,
        emit=emit,
    )

    assert code == pipeline.EXIT_LOW_DISK
    assert calls == []
    low = by_name(events, "low_disk")[0]
    assert low["free_bytes"] == 1 * 1024**3
    assert low["unprocessed"] == 2


def test_low_disk_stops_between_books(tmp_path, monkeypatch):
    """1 冊目の途中では止めない。本の切れ目で止めて成果を確定させる。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    fake_free(monkeypatch, 9 * 1024**3, 9 * 1024**3, 1 * 1024**3)
    emit, events = collect_emit()

    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}],
        output=str(tmp_path / "out"),
        min_free_bytes=5 * 1024**3,
        emit=emit,
    )

    assert code == pipeline.EXIT_LOW_DISK
    assert [c["title"] for c in calls] == ["A"]
    summary = by_name(events, "batch_summary")[0]
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    assert summary["unprocessed"] == 1
    assert summary["low_disk"] is True
    # 失敗 0・未処理 1 だけでは「全部終わった」と読めてしまう
    assert summary["ok"] is False


def test_low_disk_does_not_block_skipping_finished_books(tmp_path, monkeypatch):
    """完成済みの本を読み飛ばすだけならディスクを消費しない。"""
    out = tmp_path / "out"
    os.makedirs(out)
    (out / "A.pdf").write_text("x", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    fake_free(monkeypatch, 1 * 1024**3)
    emit, events = collect_emit()

    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}], output=str(out), min_free_bytes=5 * 1024**3, emit=emit
    )

    assert code == pipeline.EXIT_OK
    assert calls == []
    assert not by_name(events, "low_disk")


def test_zero_min_free_disables_the_guard(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    fake_free(monkeypatch, 0)
    emit, events = collect_emit()

    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}], output=str(tmp_path / "out"), min_free_bytes=0, emit=emit
    )

    assert code == pipeline.EXIT_OK
    assert [c["title"] for c in calls] == ["A"]


def test_unmeasurable_disk_does_not_stop_the_batch(tmp_path, monkeypatch):
    """空きを測れない環境で一律に止めると、余裕があっても 1 冊も処理できない。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    monkeypatch.setattr(pipeline, "smallest_free", lambda _paths: (None, None))
    emit, events = collect_emit()

    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}], output=str(tmp_path / "out"), emit=emit
    )

    assert code == pipeline.EXIT_OK
    assert [c["title"] for c in calls] == ["A"]


def test_free_bytes_walks_up_to_an_existing_directory(tmp_path):
    """バッチ開始時点では出力先がまだ無い。作る前でも測れる必要がある。"""
    missing = tmp_path / "not" / "created" / "yet"
    assert not missing.exists()
    free = pipeline.free_bytes(str(missing))
    assert free is not None and free > 0


def test_failure_is_not_swallowed_by_the_disk_guard(tmp_path, monkeypatch):
    """失敗した本の残骸こそがディスクを食う。失敗が終了コードから消えてはいけない。

    run_book は成功した本しか remove_intermediates を呼ばないので、ガードが
    発動する場面では失敗も出ている公算が高い。
    """
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls, fail_titles=("A",)))
    fake_free(monkeypatch, 9 * 1024**3, 9 * 1024**3, 1 * 1024**3)
    emit, events = collect_emit()

    code = pipeline.run_batch(
        [{"title": "A", "asin": "B01"}, {"title": "B", "asin": "B02"}],
        output=str(tmp_path / "out"),
        min_free_bytes=5 * 1024**3,
        emit=emit,
    )

    assert code == pipeline.EXIT_ERROR
    summary = by_name(events, "batch_summary")[0]
    assert summary["failed"] == 1
    assert summary["low_disk"] is True
    assert summary["ok"] is False


def test_disk_guard_reports_that_it_is_active(tmp_path, monkeypatch):
    """ガードが効いているかを開始時に示す。全冊スキップの実行でも出る。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    fake_free(monkeypatch, 9 * 1024**3)
    emit, events = collect_emit()

    pipeline.run_batch(
        [{"title": "A", "asin": "B01"}],
        output=str(tmp_path / "out"),
        min_free_bytes=5 * 1024**3,
        emit=emit,
    )

    guard = by_name(events, "disk_guard")[0]
    assert guard["enabled"] is True
    assert guard["free_bytes"] == 9 * 1024**3


def test_disk_guard_says_when_it_cannot_measure(tmp_path, monkeypatch):
    """測れないまま数十時間走らせないよう、無効であることを明示する。"""
    calls: list = []
    monkeypatch.setattr(pipeline, "run_book", make_fake_run_book(calls))
    monkeypatch.setattr(pipeline, "smallest_free", lambda _paths: (None, None))
    emit, events = collect_emit()

    pipeline.run_batch([{"title": "A", "asin": "B01"}], output=str(tmp_path / "out"), emit=emit)

    assert by_name(events, "disk_guard")[0]["enabled"] is False


def test_smallest_free_picks_the_tightest_volume(monkeypatch):
    """出力先に余裕があっても %TEMP% が枯れれば OCR が倒れる。"""
    sizes = {"out": 900, "temp": 3}
    monkeypatch.setattr(pipeline, "free_bytes", lambda path: sizes.get(path))
    assert pipeline.smallest_free(["out", "temp"]) == ("temp", 3)


def test_smallest_free_returns_none_when_nothing_is_measurable(monkeypatch):
    monkeypatch.setattr(pipeline, "free_bytes", lambda _path: None)
    assert pipeline.smallest_free(["a", "b"]) == (None, None)


def test_free_bytes_returns_none_on_oserror(monkeypatch, tmp_path):
    """到達できない UNC や未マップのドライブ。止める理由にはしない。"""

    def boom(_path):
        raise OSError("unreachable")

    monkeypatch.setattr(pipeline.shutil, "disk_usage", boom)
    assert pipeline.free_bytes(str(tmp_path)) is None
