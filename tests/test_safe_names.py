"""ファイル名の無害化 (#52)

蔵書 342 冊のうち 13 冊が、タイトルに Windows で使えない文字を含む（洋書の
副題の ":" など）。無害化しないと [WinError 267] でその本だけ落ちる。
"""

import os

from core import safe_names

OUT = r"C:\\Users\\x\\out"


def test_invalid_characters_become_full_width():
    name = safe_names.book_path_name('A:B?C*D"E<F>G|H/I\\J', OUT)
    for ch in ':?*"<>|/\\':
        assert ch not in name
    # 消すのではなく置き換える。消すと別の本が同じ名前に潰れる
    assert len(name) == len('A:B?C*D"E<F>G|H/I\\J')


def test_trailing_dots_and_spaces_are_dropped():
    """末尾のピリオド・空白は Windows のフォルダ名として無効。"""
    assert safe_names.book_path_name("本のタイトル... ", OUT) == "本のタイトル"


def test_reserved_device_names_are_avoided():
    """CON / NUL などは拡張子を付けても開けない。"""
    assert safe_names.book_path_name("CON", OUT) == "_CON"
    assert safe_names.book_path_name("com1", OUT) == "_com1"


def test_empty_title_still_yields_a_name():
    assert safe_names.book_path_name("  ...  ", OUT) == "untitled"


def test_long_title_fits_within_max_path():
    """<out>\\<name>_trimmed\\0000.png が MAX_PATH に収まること。"""
    title = "The Huawei and Snowden Questions: " + "x" * 300
    name = safe_names.book_path_name(title, OUT)
    longest = os.path.join(OUT, name + "_trimmed", "0000.png")
    assert len(longest) < safe_names.MAX_PATH


def test_truncated_names_stay_stable():
    """名前が実行のたびに変わると、完成済みの本を毎回撮り直す。"""
    title = "長いタイトル" * 60
    assert safe_names.book_path_name(title, OUT) == safe_names.book_path_name(title, OUT)


def test_different_long_titles_do_not_collide():
    """先頭が同じで末尾だけ違う本を、切り詰めで同じ名前にしない。"""
    head = "同じ書き出しの長いタイトル" * 30
    a = safe_names.book_path_name(head + "その1", OUT)
    b = safe_names.book_path_name(head + "その2", OUT)
    assert a != b


def test_deep_output_folder_is_detected_by_the_budget():
    """深すぎる出力先は名前では救えない。バッチ開始時に弾くための材料を返す。"""
    deep = "C:\\" + "\\".join(["d" * 30] * 8)
    assert safe_names.name_budget(deep) < safe_names.MIN_NAME_CHARS
    # それでも衝突しない名前は返す（呼ばれた場合に同じ名前を返さない）
    assert safe_names.book_path_name("本A", deep) != safe_names.book_path_name("本B", deep)


def test_budget_is_positive_for_a_normal_output_folder():
    assert safe_names.name_budget(OUT) > 100


def test_short_title_is_left_alone():
    assert (
        safe_names.book_path_name("みんなのフィードバック大全", OUT) == "みんなのフィードバック大全"
    )
