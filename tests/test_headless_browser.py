"""headless ブラウザのセッション置き場 (#31)。

CI の checkout はリポジトリ内の .playwright-profile/ を毎回消すので、環境変数で外に
置けることを固定する。
"""

from core import headless_browser


def test_default_profile_dir_is_inside_the_repo_by_default(monkeypatch):
    monkeypatch.delenv(headless_browser.ENV_PROFILE_DIR, raising=False)
    assert headless_browser.default_profile_dir() == headless_browser.DEFAULT_PROFILE_DIR


def test_default_profile_dir_honors_the_environment_variable(monkeypatch):
    monkeypatch.setenv(headless_browser.ENV_PROFILE_DIR, r"C:\actions-runner\_work\kindle_shot\p")
    assert headless_browser.default_profile_dir() == r"C:\actions-runner\_work\kindle_shot\p"


def test_default_profile_dir_treats_blank_as_unset(monkeypatch):
    # .env.example のように値が空で書かれていても、既定の置き場を使う
    monkeypatch.setenv(headless_browser.ENV_PROFILE_DIR, "  ")
    assert headless_browser.default_profile_dir() == headless_browser.DEFAULT_PROFILE_DIR
