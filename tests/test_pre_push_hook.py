"""pre-push フックの検証。

このフックはキャプチャ経路の変更を実機スモークで守るための強制装置で、
壊れていると規約が黙って無効化される（#47: WSL から push すると
インタプリタのパスを exec できず、スモークが一度も走らないまま落ちていた）。
シェルスクリプトなので、実際に sh を起動して振る舞いを確かめる。
"""

import os
import shutil
import subprocess

import pytest

HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".githooks", "pre-push"
)
ZERO = "0" * 40


def _find_sh():
    """POSIX sh を探す。

    Windows の PATH にある bash.EXE は WSL のもので、Windows 側の一時ディレクトリを
    そのままでは扱えないため使わない。Git for Windows が同梱する sh.exe を使う。
    """
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            root = os.path.dirname(os.path.dirname(git))
            for rel in ("bin/sh.exe", "usr/bin/sh.exe"):
                path = os.path.join(root, *rel.split("/"))
                if os.path.isfile(path):
                    return path
        return None
    return shutil.which("sh")


SH = _find_sh()
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh が見つからない")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _native_path(path):
    """フックの git config に入れる形（利用者の環境と同じ Windows 絶対パス）にする。"""
    if os.name == "nt":
        return path
    wslpath = shutil.which("wslpath")
    if wslpath is None:
        return None
    out = subprocess.run([wslpath, "-w", path], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """フックを入れた git リポジトリ。base → head の 2 コミットを持つ。"""
    root = tmp_path / "repo"
    (root / ".githooks").mkdir(parents=True)
    (root / "core").mkdir()
    shutil.copy(HOOK, root / ".githooks" / "pre-push")
    os.chmod(root / ".githooks" / "pre-push", 0o755)

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "kindleshot.smokeAsin", "B0TEST0000")

    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _commit_change(repo, relpath):
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


def _stub_python(repo, marker):
    """引数を marker に書き出すだけの偽インタプリタ。"""
    stub = repo / "stubpy"
    body = (
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'printf %s "$*" > "' + marker.as_posix() + '"\n'
    )
    stub.write_text(body, encoding="utf-8", newline="\n")
    os.chmod(stub, 0o755)
    return stub


def _run_hook(repo, *args, stdin=""):
    proc = subprocess.run(
        [SH, ".githooks/pre-push", *args],
        cwd=repo,
        input=stdin.encode(),
        capture_output=True,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _push_input(repo):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"refs/heads/main {head} refs/heads/main {base}\n"


@needs_sh
def test_check_mode_prints_resolved_python(repo):
    """--check は実際に使う Python を表示する（落ちた原因を切り分けるため）。"""
    _git(repo, "config", "kindleshot.python", "/usr/bin/python3")
    code, out, _ = _run_hook(repo, "--check")
    assert code == 0
    assert "/usr/bin/python3" in out
    assert "B0TEST0000" in out


@needs_sh
def test_windows_style_python_path_is_executable(repo, tmp_path):
    r"""#47 の本体。git config に Windows 絶対パスが入っていてもスモークが走る。

    Git Bash は C:\... をそのまま解決できるが WSL の sh はできず、
    従来は exec に失敗して「スモークが失敗した」ように見えていた。
    """
    marker = tmp_path / "ran.txt"
    stub = _stub_python(repo, marker)
    native = _native_path(str(stub))
    if native is None:
        pytest.skip("Windows 形式のパスを作れない環境")

    _git(repo, "config", "kindleshot.python", native)
    _commit_change(repo, "cli.py")

    code, out, err = _run_hook(repo, "origin", "git@example.com:x/y.git", stdin=_push_input(repo))
    assert code == 0, err or out
    assert marker.exists(), "スモークが起動していない"
    assert "B0TEST0000" in marker.read_text(encoding="utf-8")


@needs_sh
def test_headless_capture_is_watched(repo, tmp_path):
    """本番のキャプチャ経路 headless_capture.py もスモークの対象にする。

    kindle_cloud プロファイルの既定は headless なので、ここが監視から漏れていると
    実運用の経路だけ無防備になる。
    """
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "core/headless_capture.py")

    code, out, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err or out
    assert marker.exists(), "headless_capture.py の変更でスモークが走っていない"


@needs_sh
def test_unwatched_change_does_not_run_smoke(repo, tmp_path):
    """キャプチャ経路に触らない変更は素通しする。"""
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "README.md")

    code, _, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err
    assert not marker.exists()


@needs_sh
def test_unusable_python_is_reported_as_environment_problem(repo):
    """Python を起動できないとき、スモークの失敗と混同されない。"""
    _git(repo, "config", "kindleshot.python", "/nonexistent/python")
    _commit_change(repo, "cli.py")

    code, _, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 1
    assert "Python を実行できませんでした" in err
    assert "スモークに失敗" not in err
