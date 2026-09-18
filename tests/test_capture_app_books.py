"""scripts/capture_app_books.py のテスト（Kindle アプリで本を開いて撮る。#74）

画面を触る部分は実機でしか確かめられないので、ここでは「どの本を撮るか」と
「開いた本が合っているか」の判定だけを見る。**別の本を蔵書に入れない**のが要。
"""

import csv
import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import capture_app_books as cab  # noqa: E402

from core.book_format import book_pdf_path  # noqa: E402


def _library(folder, titles):
    os.makedirs(folder, exist_ok=True)
    for title in titles:
        with open(book_pdf_path(str(folder), title), "wb") as f:
            f.write(b"%PDF-1.4\n")  # 中身は見ない（撮影そのものは差し替える）
    return str(folder)


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_title_matches_ignores_marks_and_case():
    assert cab.title_matches(
        "学びを結果に変えるアウトプット大全", "学びを結果に変えるアウトプット大全"
    )
    assert cab.title_matches("Linear Algebra Done Right", "linear algebra done right")


def test_title_matches_refuses_a_title_read_only_in_part():
    """題名の一部しか読めないときは撮らない（安全側）。

    照合は**先頭に戻して表紙を読んでから**行う。表紙には題名が丸ごと入っているので、
    一部しか読めないのは OCR が失敗しているか、別の本を開いている。
    """
    assert not cab.title_matches(
        "しっかり学ぶ数理最適化　モデルからアルゴリズムまで (ＫＳ情報科学専門書)",
        "しっかり学ぶ数理最適化",
    )
    assert cab.title_matches(
        "しっかり学ぶ数理最適化　モデルからアルゴリズムまで (ＫＳ情報科学専門書)",
        "ＫＳ情報科学専門書 しっかり学ぶ数理最適化 モデルからアルゴリズムまで 梅谷俊治 講談社",
    )


def test_title_matches_finds_the_title_in_a_cover_page():
    """表紙には書名ヘッダーが出ないので、ページ全体の OCR から探す（実測）。"""
    cover = "Undergraduate Texts in Mathematics UTM Emily Clader Dustin Ross Beginning in Algebraic Geometry OPEN ACCESS Springer"
    assert cab.title_matches(
        "Beginning in Algebraic Geometry (Undergraduate Texts in Mathematics)", cover
    )


def test_title_matches_rejects_a_different_book():
    """**別の本を開いたまま撮らない。** 検索が違う本を出すことがある。"""
    assert not cab.title_matches("ベイズ深層学習", "強化学習")
    assert not cab.title_matches("入門　現代の量子力学", "集合・位相・圏")
    assert not cab.title_matches("ベイズ深層学習", "")


def test_title_matches_rejects_a_book_that_only_starts_the_same():
    """先頭が同じだけの別の本を同じ本と見ない。

    対象は英語の数学書・技術書が多く、先頭が同じ題名が普通にある。
    先頭 8 文字で見ていたときは、この 2 組がどちらも同じ本と判定されていた。
    """
    assert not cab.title_matches(
        "Linear Algebra Done Right (Undergraduate Texts in Mathematics)",
        "Linear Algebra and Its Applications Fifth Edition",
    )
    assert not cab.title_matches(
        "Introduction to Smooth Manifolds", "Introduction to Algebraic Geometry"
    )


def test_title_matches_rejects_another_book_of_the_same_series():
    """括弧書きのシリーズ名は同じシリーズの別の本の表紙にもある。それだけで通してはいけない。

    レビューで実測: 括弧書きを照合に入れていたとき、この 2 組がどちらも「同じ本」になった。
    """
    assert not cab.title_matches(
        "Linear Algebra Done Right (Undergraduate Texts in Mathematics)",
        "Undergraduate Texts in Mathematics UTM Terence Tao Analysis I Springer",
    )
    assert not cab.title_matches(
        "Introduction to Smooth Manifolds (Graduate Texts in Mathematics)",
        "Graduate Texts in Mathematics Serge Lang Algebra Springer",
    )
    # 括弧書きを落としても、本題名が丸ごと読めていれば通る
    assert cab.title_matches(
        "Beginning in Algebraic Geometry (Undergraduate Texts in Mathematics)",
        "UTM Emily Clader Dustin Ross Beginning in Algebraic Geometry Springer",
    )


def test_title_matches_survives_the_noise_around_a_cover():
    """表紙のページ全体を読むので、著者名・出版社名・帯の文句が混ざる。"""
    assert cab.title_matches(
        "Linear Algebra Done Right (Undergraduate Texts in Mathematics)",
        "Undergraduate Texts in Mathematics UTM Sheldon Axler Linear Algebra Done Right "
        "Fourth Edition OPEN ACCESS Springer",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell のクリップボード")
def test_clipboard_round_trips_a_japanese_title():
    """PowerShell の出力は cp932 で出る。UTF-8 で読むと日本語の題名が全部「入れられない」になった
    （実測: 本番 2 冊目「入門　現代の量子力学…」で発覚し、日本語の本が全滅した）。"""
    title = "入門　現代の量子力学　量子情報・量子測定を中心として (ＫＳ物理専門書)"
    assert cab._to_clipboard(title)
    assert cab._norm(cab._clipboard()) == cab._norm(title)


def test_place_waits_until_the_window_stays_where_it_was_put(monkeypatch):
    """起動し直したアプリは遅れて最後の本を開き直し、最大化し直す（実測）。置いた直後に
    確かめただけでは、そのあと最大化されて検索窓が窓の外になる。"""
    monkeypatch.setattr(cab, "_move_window", lambda hwnd, w, h: None)
    monkeypatch.setattr(cab.time, "sleep", lambda s: None)
    placed = (cab.WINDOW_LEFT, 0, cab.WINDOW_LEFT + cab.WIDTH, cab.HEIGHT)
    maximized = (-2568, -8, 8, 1400)
    rects = iter([placed, maximized, placed, placed])
    monkeypatch.setattr("core.win32_utils.get_window_rect", lambda hwnd: next(rects))
    cab._place(object())  # 最大化を挟んでも、2 回続けて置いた通りになるまで待つ
    with pytest.raises(StopIteration):
        next(rects)  # 4 回とも見ている（1 回一致で返る実装ではない）
    monkeypatch.setattr("core.win32_utils.get_window_rect", lambda hwnd: maximized)
    with pytest.raises(RuntimeError):
        cab._place(object(), tries=3)


def test_bar_title_rejects_another_volume_and_accepts_a_truncated_title():
    """バーの題名は省略されるが、省略される前に見えている巻数の違いで弾く（前方一致）。"""
    assert cab.bar_title_matches(
        "ゼロから作るDeep Learning ❷ ―自然言語処理編", "ゼロから作るDeep Learning ❷ ―自然言語…"
    )
    assert not cab.bar_title_matches(
        "ゼロから作るDeep Learning ❷ ―自然言語処理編", "ゼロから作るDeep Learning ❶ ―Python…"
    )
    assert not cab.bar_title_matches(
        "Introduction to Algorithms, fourth edition", "Introduction to Algorithms, third edition"
    )
    assert cab.bar_title_matches(
        "学びを結果に変えるアウトプット大全", "学びを結果に変えるアウトプット大全"
    )
    assert not cab.bar_title_matches("学びを結果に変えるアウトプット大全", "学び…")  # 短すぎる


def _verify_stubs(monkeypatch, *, cover, bar, shown_after_click=True, hidden=True):
    """verify_title の画面まわりを差し替える。cover / bar は OCR が返す文字。"""
    monkeypatch.setattr(cab, "_shot", lambda hwnd, box=None: box)
    monkeypatch.setattr(cab, "_ocr", lambda image: bar if image else cover)  # box 付きはバー
    monkeypatch.setattr(cab, "_click", lambda hwnd, x, y, wait=0: None)
    monkeypatch.setattr(cab, "reader_chrome_shown", lambda image, **kw: shown_after_click)
    monkeypatch.setattr(cab, "hide_reader_chrome", lambda hwnd, **kw: hidden)
    monkeypatch.setattr(cab, "_keep_shot", lambda hwnd, name: None)


def test_verify_title_falls_back_to_the_reader_bar(monkeypatch):
    """表紙の題名が飾り文字で読めない本（実測: アウトプット大全）は、読書 UI のバーで確かめる。"""
    title = "学びを結果に変えるアウトプット大全"
    _verify_stubs(monkeypatch, cover="OUTPUT THE POWER OF OUTPUT", bar=title)
    assert cab.verify_title(object(), title, emit=lambda *a: None)


def test_verify_title_refuses_when_the_ui_cannot_be_shown_or_hidden(monkeypatch):
    """バーを読むために出した UI が見えない／消せないなら撮らない（UI が写った本を完成にしない）。"""
    title = "学びを結果に変えるアウトプット大全"
    _verify_stubs(monkeypatch, cover="OUTPUT", bar=title, shown_after_click=False)
    assert not cab.verify_title(object(), title, emit=lambda *a: None)
    _verify_stubs(monkeypatch, cover="OUTPUT", bar=title, hidden=False)
    assert not cab.verify_title(object(), title, emit=lambda *a: None)


def test_is_cover_tells_a_book_from_the_background():
    """検索結果がちょうど 1 冊かを、この判定で見る（別の本を撮らないため）。"""
    from PIL import Image

    plain = Image.new("RGB", (40, 40), (250, 250, 250))
    assert not cab.is_cover(plain)

    cover = Image.new("RGB", (40, 40), (250, 250, 250))
    for x in range(40):
        for y in range(20):
            cover.putpixel((x, y), (10, 40, 180))  # 表紙は色がばらつく
    assert cab.is_cover(cover)


def _library_screen(blue_top, blue_bottom):
    """ライブラリ画面の代わり。左の列に「全て」の青い行だけを描く。"""
    from PIL import Image

    image = Image.new("RGB", (1200, 600), (255, 255, 255))
    for x in range(20, 232):
        for y in range(blue_top, blue_bottom + 1):
            image.putpixel((x, y), (0, 90, 200))
    return image


def test_library_anchor_follows_the_blue_row():
    """「全て」の行の位置は日によって違う（実測: 243 と 188）。青い行を探して追う。"""
    assert cab.find_library_anchor(_library_screen(169, 204)) == 186
    assert cab.find_library_anchor(_library_screen(225, 260)) == 242
    assert cab.is_library(_library_screen(169, 204))


def test_library_anchor_is_none_off_the_library():
    from PIL import Image

    assert cab.find_library_anchor(Image.new("RGB", (1200, 600), (255, 255, 255))) is None
    # 1〜2 行だけ青い（本文中の下線リンクなど）のを「全て」の行と見ない
    assert cab.find_library_anchor(_library_screen(200, 203)) is None
    assert not cab.is_library(Image.new("RGB", (1200, 600), (255, 255, 255)))
    # 本のページの青い領域（青い表紙・図版）を「全て」の行と見ない。行の上下は白い余白のはず
    page = Image.new("RGB", (1200, 600), (0, 90, 200))
    assert cab.find_library_anchor(page) is None
    figure = _library_screen(169, 204)
    for x in range(0, 1200):
        for y in range(140, 169):
            figure.putpixel((x, y), (120, 120, 120))  # 青い帯の上が白くない（図版の一部）
    assert cab.find_library_anchor(figure) is None
    # 章見出しの青い箱（実測: x=115〜270、高さ 42px、上下は白）を「全て」の行と見ない。
    # 実測でこれを取り違え、本を開いたままライブラリにいると判定した
    chapter = Image.new("RGB", (1200, 600), (255, 255, 255))
    for x in range(115, 271):
        for y in range(118, 160):
            chapter.putpixel((x, y), (30, 140, 220))
    assert cab.find_library_anchor(chapter) is None
    # 欄の幅いっぱいでも、高さが違えば「全て」の行ではない
    assert cab.find_library_anchor(_library_screen(169, 240)) is None
    # 左端だけ欠ける箱（x=40〜232）は「左端まで青い」だけで弾く
    left_short = Image.new("RGB", (1200, 600), (255, 255, 255))
    for x in range(40, 233):
        for y in range(169, 205):
            left_short.putpixel((x, y), (0, 90, 200))
    assert cab.find_library_anchor(left_short) is None
    # 欄の外まで続く帯（x=20〜300）は「欄の外が白い」だけで弾く
    wide = Image.new("RGB", (1200, 600), (255, 255, 255))
    for x in range(20, 301):
        for y in range(169, 205):
            wide.putpixel((x, y), (0, 90, 200))
    assert cab.find_library_anchor(wide) is None


def _arrow(page, top=0):
    for x in range(30, 48):
        for y in range(24 + top, 28 + top):
            page.putpixel((x, y), (30, 30, 30))  # ← の矢印


def test_reader_chrome_is_seen_by_the_back_arrow():
    """読書 UI はページにかぶさるので、出たまま撮ると上下が隠れる。出ているかを左上で見る。"""
    from PIL import Image

    page = Image.new("RGB", (1200, 1390), (255, 255, 255))
    assert not cab.reader_chrome_shown(page)
    _arrow(page)
    assert cab.reader_chrome_shown(page)
    # 幅いっぱいが黒い表紙は、矢印の位置に暗い画素があっても UI ではない（帯が暗い）。
    # 実測: これを「出ている」と見て消せず、その本を撮れなかった
    dark_cover = Image.new("RGB", (1200, 1390), (5, 5, 5))
    assert not cab.reader_chrome_shown(dark_cover)
    for x in range(0, 1200):
        for y in range(0, 48):
            dark_cover.putpixel((x, y), (255, 255, 255))  # 黒い表紙の上に白い UI のバーが出た
    _arrow(dark_cover)
    assert cab.reader_chrome_shown(dark_cover)


def _with_title_bar(height=48):
    from PIL import Image

    page = Image.new("RGB", (1200, 1390), (255, 255, 255))
    for x in range(1200):
        page.putpixel((x, 0), (75, 75, 75))  # 前面のウィンドウの枠線（実測値）
        for y in range(1, height):
            page.putpixel((x, y), (247, 249, 250))  # 薄い灰色の帯（実測値）
    _arrow(page)  # タイトルバーにも ← がある
    return page


def test_title_bar_is_measured_and_not_taken_for_the_reader_ui():
    """アプリはタイトルバーを出す表示と出さない表示を行き来する（実測: 同じ日に両方）。

    タイトルバーの ← を読書 UI と取り違えると「消せない」で全冊止まる（実測）。
    """
    from PIL import Image

    plain = Image.new("RGB", (1200, 1390), (255, 255, 255))
    assert cab.title_bar_height(plain) == 0
    for x in range(1200):
        plain.putpixel((x, 0), (75, 75, 75))  # 枠線だけの上端をバーと見ない
    assert cab.title_bar_height(plain) == 0
    page = _with_title_bar(48)
    assert cab.title_bar_height(page) == 48
    assert cab.reader_chrome_shown(page, top=0)  # ずらさないと取り違える
    assert not cab.reader_chrome_shown(page, top=48)
    _arrow(page, top=48)  # 読書 UI が出た
    assert cab.reader_chrome_shown(page, top=48)


def test_title_bar_outside_the_measured_range_is_not_trusted(monkeypatch):
    """淡い色のページや表示テーマで上限まで灰色が続くと 100 が返る。それを削ると全ページの上が欠ける。"""
    pale = _with_title_bar(100)
    assert cab.title_bar_height(pale) == 100
    monkeypatch.setattr(cab, "_shot", lambda hwnd, box=None: pale)
    assert cab.reader_top(object(), emit=lambda *a: None) is None
    monkeypatch.setattr(cab, "_shot", lambda hwnd, box=None: _with_title_bar(48))
    assert cab.reader_top(object(), emit=lambda *a: None) == 48


def test_cuts_the_title_bar_only_when_it_is_there(tmp_path, monkeypatch):
    books, library, state = _prepare(tmp_path, [{"title": "本G", "asin": "A7"}], [])
    _stub_screen(monkeypatch)
    monkeypatch.setattr(cab, "reader_top", lambda hwnd, **kw: 48)
    monkeypatch.setattr(cab, "title_bar_height", lambda image, **kw: 48)
    seen = {}

    def fake_run_book(**kw):
        seen["min_margins"] = kw["min_margins"]
        kw["emit"]("result", total_pages=120, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 0
    assert seen["min_margins"] == (0, 0, 50, 0)


def test_pending_skips_books_that_are_already_there(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    books = [
        {"title": "撮った本", "asin": "A1"},
        {"title": "まだの本", "asin": "A2"},
        {"title": "済みの本", "asin": "A3"},
    ]
    with open(book_pdf_path(str(library), "撮った本"), "wb") as f:
        f.write(b"%PDF")
    got = cab.pending(books, {"A3"}, str(library))
    assert [b["title"] for b in got] == ["まだの本"]


def test_load_state_retries_a_failed_book(tmp_path):
    """失敗した本は次にもう一度試す（完了だけを済みにする）。"""
    path = tmp_path / "state.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cab.COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({"asin": "A1", "title": "済", "status": "完了"})
        writer.writerow({"asin": "A2", "title": "落ちた", "status": "失敗"})
        writer.writerow({"asin": "A3", "title": "開けない", "status": "開けない"})
    assert cab.load_state(str(path)) == {"A1"}


def test_dry_run_lists_without_touching_the_screen(tmp_path, capsys):
    books = [{"title": "まだの本", "asin": "A2", "format": "searchable_pdf"}]
    books_path = tmp_path / "books.json"
    with open(books_path, "w", encoding="utf-8") as f:
        json.dump(books, f, ensure_ascii=False)
    library = tmp_path / "lib"
    library.mkdir()
    code = cab.main(
        [
            "--books",
            str(books_path),
            "--library",
            str(library),
            "--state",
            str(tmp_path / "s.csv"),
            "--dry-run",
        ]
    )
    assert code == 0
    assert "まだの本" in capsys.readouterr().out


def _prepare(tmp_path, entries, titles):
    books_path = tmp_path / "books.json"
    with open(books_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    library = _library(tmp_path / "lib", titles)
    return str(books_path), library, str(tmp_path / "state.csv")


def _stub_screen(monkeypatch, *, opened=True, hidden=True, rewound=True, verified=True):
    monkeypatch.setattr(cab, "_app", lambda: object())
    monkeypatch.setattr(cab, "_place", lambda hwnd: None)
    monkeypatch.setattr(cab, "restart_app", lambda **kw: None)
    monkeypatch.setattr("core.win32_utils.get_window_rect", lambda hwnd: (0, 0, 1200, 1390))
    monkeypatch.setattr("core.config.load_config", lambda: {})
    monkeypatch.setattr(cab, "open_book", lambda hwnd, title, **kw: opened)
    monkeypatch.setattr(cab, "reader_top", lambda hwnd, **kw: 0)
    monkeypatch.setattr(cab, "hide_reader_chrome", lambda hwnd, **kw: hidden)
    # 撮り終えたあとの測り直し（表示が変わっていない）
    monkeypatch.setattr(cab, "title_bar_height", lambda image, **kw: 0)
    monkeypatch.setattr(cab, "reader_chrome_shown", lambda image, **kw: False)
    monkeypatch.setattr(cab, "_shot", lambda hwnd, box=None: None)
    monkeypatch.setattr(cab, "rewind_to_start", lambda hwnd, **kw: rewound)
    monkeypatch.setattr(cab, "verify_title", lambda hwnd, title, **kw: verified)


def test_restarts_the_app_first_and_after_a_book_could_not_be_opened(tmp_path, monkeypatch):
    """F11 全画面を経たアプリは検索窓に文字が入らなくなり、操作では戻らない（実測）。"""
    entries = [
        {"title": "本X", "asin": "X1"},
        {"title": "本Y", "asin": "X2"},
        {"title": "本Z", "asin": "X3"},
    ]
    books, library, state = _prepare(tmp_path, entries, [])
    _stub_screen(monkeypatch)
    restarts: list[int] = []
    monkeypatch.setattr(cab, "restart_app", lambda **kw: restarts.append(len(restarts)))
    opened = iter([False, True, True])
    monkeypatch.setattr(cab, "open_book", lambda hwnd, title, **kw: next(opened))

    def fake_run_book(**kw):
        kw["emit"]("result", total_pages=120, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    cab.main(["--books", books, "--library", library, "--state", state])
    # 最初に 1 回、開けなかった本のあとに 1 回。開けた本のあとは起動し直さない
    assert len(restarts) == 2


def test_does_not_capture_with_the_reader_ui_showing(tmp_path, monkeypatch):
    """UI が出たままだと上下が隠れた本になる。消せなければ撮らない。"""
    books, library, state = _prepare(tmp_path, [{"title": "本E", "asin": "A5"}], [])
    _stub_screen(monkeypatch, hidden=False)
    code = cab.main(["--books", books, "--library", library, "--state", state])
    assert code == 1
    assert _rows(state)[0]["status"] == "読書 UI を消せない"


def test_counts_every_status_and_fails_when_a_book_is_not_captured(tmp_path, monkeypatch, capsys):
    """撮れなかった本があるのに 0 を返すと、無人実行で異常に気づけない。"""
    books, library, state = _prepare(tmp_path, [{"title": "本A", "asin": "A1"}], [])
    _stub_screen(monkeypatch, opened=False)
    code = cab.main(["--books", books, "--library", library, "--state", state])
    assert code == 1
    assert _rows(state)[0]["status"] == "開けない"
    assert "開けない" in capsys.readouterr().out


def test_does_not_finish_a_book_with_too_few_pages(tmp_path, monkeypatch):
    """撮れていない本を完成扱いにしない（次からずっと飛ばされる）。"""
    books, library, state = _prepare(tmp_path, [{"title": "本B", "asin": "A2"}], [])
    _stub_screen(monkeypatch)

    def fake_run_book(**kw):
        kw["emit"]("result", total_pages=3, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    code = cab.main(["--books", books, "--library", library, "--state", state])
    row = _rows(state)[0]
    assert row["status"] == "ページが少なすぎる" and row["pages"] == "3"
    assert code == 1


def test_records_a_finished_book_with_its_stopped_reason(tmp_path, monkeypatch):
    books, library, state = _prepare(tmp_path, [{"title": "本C", "asin": "A3"}], [])
    _stub_screen(monkeypatch)

    def fake_run_book(**kw):
        assert kw["page_turn"] == "right"  # アプリは縦書きでも → が次ページ
        assert kw["overwrite"] is True  # 失敗した本をやり直せるように
        # asin を渡すと run_book が URL で開いて F11 を押し、アプリが全画面になる（実測）
        assert kw["asin"] is None and kw["no_cover"] is True
        assert kw["min_margins"] is None  # 読書 UI を消して撮る。固定で削ると中身を切る
        # 前面化のクリックが本文に当たると表示が変わる（実測: 拡大された断片が入った）
        assert kw["config"]["capture"]["profiles"]["kindle"]["click_position"] == "none"
        kw["emit"]("result", total_pages=120, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 0
    row = _rows(state)[0]
    assert (row["status"], row["pages"], row["stopped_reason"]) == (cab.DONE, "120", "no_change")
    assert cab.load_state(state) == {"A3"}


def test_does_not_finish_a_book_when_the_window_moved_while_capturing(tmp_path, monkeypatch):
    """撮っている間にウィンドウが最大化されると、拡大された断片がページになる（実測）。"""
    books, library, state = _prepare(tmp_path, [{"title": "本F", "asin": "A6"}], [])
    _stub_screen(monkeypatch)
    rects = iter([(0, 0, 1200, 1390), (-8, -8, 2560, 1400)])
    monkeypatch.setattr("core.win32_utils.get_window_rect", lambda hwnd: next(rects))

    def fake_run_book(**kw):
        kw["emit"]("result", total_pages=120, stopped_reason="no_change")
        return 0

    monkeypatch.setattr("core.pipeline.run_book", fake_run_book)
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 1
    row = _rows(state)[0]
    assert row["status"] == "失敗" and "ウィンドウが動いた" in row["detail"]


def test_refuses_a_partial_capture_into_the_library(tmp_path, monkeypatch):
    """--max-pages の出力を蔵書に混ぜない（部分本が完成扱いで固定される）。"""
    books, library, state = _prepare(tmp_path, [{"title": "本D", "asin": "A4"}], [])
    with pytest.raises(SystemExit):
        cab.main(["--books", books, "--library", library, "--state", state, "--max-pages", "8"])


def _run_book_writing_pdf(library, total=120):
    def fake_run_book(**kw):
        # 本物の run_book は撮れた分の PDF を蔵書に書いてから返る
        with open(book_pdf_path(library, kw["title"]), "wb") as f:
            f.write(b"%PDF-1.4\n")
        kw["emit"]("result", total_pages=total, stopped_reason="timeout")
        return 0

    return fake_run_book


def test_a_failed_book_does_not_leave_its_pdf_in_the_library(tmp_path, monkeypatch):
    """**失敗にした本の PDF が蔵書に残ると、次から黙って飛ばされる**（pending は PDF の有無で見る）。

    レビューで見つかった穴。run_book は EXIT_OK を返す前に PDF を蔵書に書いているので、
    「ウィンドウが動いた」「ページが少なすぎる」と判定しても断片入りの PDF が残っていた。
    """
    books, library, state = _prepare(tmp_path, [{"title": "本H", "asin": "A8"}], [])
    _stub_screen(monkeypatch)
    rects = iter([(0, 0, 1200, 1390), (-8, -8, 2560, 1400)])
    monkeypatch.setattr("core.win32_utils.get_window_rect", lambda hwnd: next(rects))
    monkeypatch.setattr("core.pipeline.run_book", _run_book_writing_pdf(library))
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 1
    assert not os.path.exists(book_pdf_path(library, "本H"))
    assert "退避" in _rows(state)[0]["detail"]
    assert os.listdir(tmp_path / "failed_pdfs")
    # 次の実行で、もう一度対象になる
    with open(books, encoding="utf-8") as f:
        entries = json.load(f)
    assert [b["title"] for b in cab.pending(entries, cab.load_state(state), library)] == ["本H"]


def test_a_book_whose_display_changed_while_capturing_is_not_finished(tmp_path, monkeypatch):
    """タイトルバーの有無や読書 UI が撮影中に切り替わると、欠けた／UI の写ったページが混ざる。"""
    books, library, state = _prepare(tmp_path, [{"title": "本I", "asin": "A9"}], [])
    _stub_screen(monkeypatch)
    monkeypatch.setattr(cab, "title_bar_height", lambda image, **kw: 48)  # 開いたときは 0 だった
    monkeypatch.setattr("core.pipeline.run_book", _run_book_writing_pdf(library))
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 1
    row = _rows(state)[0]
    assert row["status"] == "失敗" and "表示が変わった" in row["detail"]
    assert not os.path.exists(book_pdf_path(library, "本I"))


def test_a_trial_capture_is_not_recorded_as_done(tmp_path, monkeypatch):
    """試し撮りを本番の state に「完了」で残すと、蔵書に PDF が無いのに永久に飛ばされる。"""
    books, library, state = _prepare(tmp_path, [{"title": "本J", "asin": "A10"}], [])
    _stub_screen(monkeypatch)
    monkeypatch.setattr("core.pipeline.run_book", _run_book_writing_pdf(library, total=8))
    cab.main(
        [
            "--books",
            books,
            "--library",
            library,
            "--state",
            state,
            "--max-pages",
            "8",
            "--allow-partial",
        ]
    )
    assert _rows(state)[0]["status"] == "試し撮り"
    assert cab.load_state(state) == set()


def test_restarts_the_app_after_an_exception_too(tmp_path, monkeypatch):
    """置けない・窓が見つからない等の例外でも、次の本の前に起動し直す（唯一の復旧手段）。"""
    entries = [{"title": "本K", "asin": "K1"}, {"title": "本L", "asin": "K2"}]
    books, library, state = _prepare(tmp_path, entries, [])
    _stub_screen(monkeypatch)
    restarts: list[int] = []
    monkeypatch.setattr(cab, "restart_app", lambda **kw: restarts.append(1))

    def broken_place(hwnd):
        raise RuntimeError("ウィンドウを置けない")

    monkeypatch.setattr(cab, "_place", broken_place)
    assert cab.main(["--books", books, "--library", library, "--state", state]) == 1
    assert len(restarts) == 2  # 最初に 1 回、例外で失敗した本のあとに 1 回
    assert all(r["status"] == "失敗" for r in _rows(state))
