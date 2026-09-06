"""CI ワークフローの取りこぼしを防ぐテスト

e2e のジョブは対象ファイルを明示している。`-m e2e` だけで走らせると、
実 Amazon 用のテストまで拾って資格情報が無いまま skip され、
「緑だが何も試していない」状態になるため。

その代わり、**新しく e2e ファイルを足したときにどのジョブからも参照されず、
黙って実行されない**という穴ができる。ここでそれを塞ぐ。
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
E2E_DIR = os.path.join(REPO_ROOT, "tests", "e2e")


def workflow_text():
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def e2e_test_files():
    """tests/e2e 直下のテストファイル名。"""
    return sorted(
        name for name in os.listdir(E2E_DIR) if name.startswith("test_") and name.endswith(".py")
    )


def test_every_e2e_file_is_run_by_some_job():
    """全ての e2e ファイルが、いずれかのジョブから名指しで実行されること。"""
    text = workflow_text()
    missing = [name for name in e2e_test_files() if f"tests/e2e/{name}" not in text]
    assert not missing, (
        f"どのジョブからも実行されない e2e ファイルがあります: {missing}\n"
        f".github/workflows/ci.yml のいずれかのジョブに追加してください"
    )


def test_e2e_files_exist():
    """参照だけあってファイルが無い状態を防ぐ（リネーム漏れ）。"""
    referenced = set(re.findall(r"tests/e2e/(test_\w+\.py)", workflow_text()))
    missing = sorted(name for name in referenced if not os.path.exists(os.path.join(E2E_DIR, name)))
    assert not missing, f"ワークフローが存在しないファイルを参照しています: {missing}"


def test_unit_job_excludes_e2e():
    """ユニットのジョブが e2e を巻き込まないこと（実機依存で落ちるため）。"""
    assert 'pytest -m "not e2e" -q' in workflow_text()
