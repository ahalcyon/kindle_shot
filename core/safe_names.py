"""ファイル名・フォルダ名の無害化

Windows のファイル名には使えない文字があり（``< > : " / \\ | ? *``）、パス全体の
長さにも上限がある。蔵書には副題を ``:`` で繋ぐ洋書が混ざっており、タイトルを
そのままパスにすると ``[WinError 267] ディレクトリ名が無効です`` で落ちる（#52）。

GUI は以前から ``sanitize_folder_name`` を通していたが、その実体は
``core/win32_utils`` にあり、同モジュールが ``ctypes.windll`` と ``pyautogui`` を
module レベルで import するため Linux から import できない。CLI / バッチ経路から
使えるよう、純粋な文字列処理だけをここに置く。
"""

import hashlib
import os
import re

# Windows のファイル名に使えない文字。消すのではなく全角に置き換える。
# 消すと「Q&A: 総集編」と「Q&A 総集編」が同じ名前になり、別の本が衝突する。
_INVALID_NAME_CHARS = {
    "/": "／",
    "\\": "￥",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "”",
    "<": "＜",
    ">": "＞",
    "|": "｜",
}

# Windows の予約デバイス名。拡張子を付けても開けないので接頭辞で避ける。
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)

# 制御文字はファイル名に使えない
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Windows の MAX_PATH（終端 NUL を含む 260）。
# 長さは Python のコードポイント数で測っている。Windows API は UTF-16 の
# コードユニット数で数えるので、BMP 外の文字（絵文字など）は 1 文字で 2 を
# 消費する。実害が出るのは絵文字入りの極端に長いタイトルだけなので許容する。
MAX_PATH = 260

# 名前の後ろに付く分の見積り。実際に作られる最長は次のとおり（<name> からの追加分）:
#   <name>_trimmed\1000.png  18  ページ番号は 999 を超えると 4 桁になる
#   <name>_pages\1000.png    15  faithful + --embed-images (core/markdown_writer.py)
#   <name>\manifest.json     14
#   <name>\001.png.tmp       13  core/capture_engine.py の書き込み中の一時名
#   <name>_12.md              6  split_words による分割出力
# 最長 18 に余裕を足して 24 とする。
#
# ディレクトリの作成には CreateDirectory の別の上限（MAX_PATH - 12 = 248）が効く。
# reserve を削るとここに先に当たるので、24 より小さくしないこと。
_SUFFIX_RESERVE = 24

# 切り詰めたことを示すハッシュの長さ（"_" + 8 桁）
_DIGEST_LEN = 8

# 名前に最低限これだけ置けないと、出力先が深すぎて本を保存できない。
# ハッシュだけの名前でも通らないので、バッチ開始時に弾く（#52）。
MIN_NAME_CHARS = _DIGEST_LEN


def sanitize_folder_name(text):
    """フォルダ名として使えるように整える（不正文字を全角に置換）。"""
    cleaned = _CONTROL.sub("", text)
    cleaned = "".join(_INVALID_NAME_CHARS.get(ch, ch) for ch in cleaned)
    # 末尾のピリオド・空白は Windows のフォルダ名として無効
    cleaned = cleaned.strip().rstrip(". \u3000")
    # "NUL.txt" のように拡張子が付いていても予約名は予約名なので、
    # ステム（最初のドットまで）で判定する
    if _RESERVED.match(cleaned.split(".")[0]):
        cleaned = "_" + cleaned
    return cleaned


def name_budget(out, reserve=_SUFFIX_RESERVE):
    """出力先 out の下で、名前に使える文字数。"""
    return MAX_PATH - 1 - len(os.path.abspath(out)) - 1 - reserve


def book_path_name(title, out):
    """本のフォルダ名・ファイル名に使う、安全で安定した名前を返す。

    タイトルそのものは表示とイベントに残し、パスを作るときだけこれを通す。

    同じ本が実行のたびに同じ名前になることが要る。変わると再実行時の
    「完成済みならスキップ」が効かず、同じ本を撮り直す。そのため切り詰めには
    元のタイトルのハッシュを使う（切り詰め後の文字列からではなく）。
    """
    cleaned = sanitize_folder_name(title) or "untitled"
    budget = name_budget(out)
    if budget < _DIGEST_LEN + 1:
        # 出力先が深すぎて名前をほとんど置けない。せめて衝突しない名前にする
        return _digest(title)
    if len(cleaned) <= budget:
        return cleaned
    head = cleaned[: budget - _DIGEST_LEN - 1].rstrip(". \u3000")
    return f"{head}_{_digest(title)}"


def _digest(title):
    return hashlib.sha1(title.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
