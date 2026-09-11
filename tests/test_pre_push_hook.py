"""pre-push フックの検証。

このフックはキャプチャ経路の変更を実機スモークで守るための強制装置で、
壊れていると規約が黙って無効化される（#47: WSL から push すると
インタプリタのパスを exec できず、スモークが一度も走らないまま落ちていた）。
シェルスクリプトなので、実際に sh を起動して振る舞いを確かめる。
"""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".githooks", "pre-push")
AGENTS = os.path.join(ROOT, "AGENTS.md")

# git が「ブランチ削除の push」を表すのに使う全ゼロの sha
ZERO = "0" * 40

# git config / フック内の git 呼び出しを利用者の global 設定から切り離す。
# commit.gpgsign=true のような設定があるとフィクスチャの commit が落ちる。
SEALED_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git_roots():
    """Git for Windows の導入先候補。

    git の実体は cmd/ にも mingw64/bin/ にも置かれ、shim (scoop 等) を挟むこともある。
    どこから来ても届くよう、git 自身と git --exec-path の祖先を全部たどる。
    """
    starts = []
    git = shutil.which("git")
    if git:
        starts.append(git)
    try:
        out = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True)
        starts.append(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass

    roots = []
    for start in starts:
        path = os.path.dirname(os.path.abspath(start))
        while True:
            roots.append(path)
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return roots


def _find_sh():
    """POSIX sh を探す。

    Windows の PATH にある bash.EXE は WSL のもので、Windows 側の一時ディレクトリを
    そのままでは扱えないため使わない。Git for Windows が同梱する sh.exe を使う。
    """
    if os.name != "nt":
        return shutil.which("sh")
    for root in _git_roots():
        for rel in ("bin/sh.exe", "usr/bin/sh.exe"):
            path = os.path.join(root, *rel.split("/"))
            if os.path.isfile(path):
                return path
    return None


SH = _find_sh()
needs_sh = pytest.mark.skipif(SH is None, reason="POSIX sh が見つからない")


@pytest.mark.skipif(not os.environ.get("CI"), reason="CI 以外では sh が無くてもよい")
def test_sh_is_available_on_ci():
    """CI で黙って skip されると、退行検出網が消えたことに誰も気づかない。"""
    assert SH is not None, (
        f"sh が見つからず、フックのテストが全て skip されている（git={shutil.which('git')}）"
    )


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=SEALED_ENV)


def _native_path(path):
    """フックの git config に入れる形（利用者の環境と同じ Windows 絶対パス）にする。"""
    if os.name == "nt":
        return path
    wslpath = shutil.which("wslpath")
    if wslpath is None:
        return None
    out = subprocess.run([wslpath, "-w", path], capture_output=True, text=True, check=True)
    native = out.stdout.strip()
    # pytest の tmp_path は WSL では ext4 上なので UNC (\\wsl.localhost\...) になる。
    # このテストが見たいのはドライブレター形式なので、そうでなければ諦める。
    return native if re.match(r"^[A-Za-z]:[\\/]", native) else None


@pytest.fixture
def repo(tmp_path):
    """フックを入れた git リポジトリ。base → head の 2 コミットを持つ。"""
    root = tmp_path / "repo"
    (root / ".githooks").mkdir(parents=True)
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


def _commit_change(repo, *relpaths):
    """1 つのコミットで複数のファイルを変える。

    _push_input が HEAD~1 を base にするので、**2 回に分けてコミットすると
    後ろの 1 つしかフックに見えない**。
    """
    for relpath in relpaths:
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


def _stub_python(repo, marker):
    """引数を marker に書き足すだけの偽インタプリタ。

    **上書きではなく追記する。** 1 回の push で headless と画面の 2 本を
    走らせることがあるので、上書きだと後ろの 1 本しか観測できない。
    """
    stub = repo / "stubpy"
    body = (
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "' + marker.as_posix() + '"\n'
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
        env=SEALED_ENV,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _push_input(repo):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=SEALED_ENV,
    ).stdout.strip()
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=SEALED_ENV,
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
def test_screen_only_change_runs_the_screen_smoke(repo, tmp_path):
    """画面キャプチャ経路でしか動かないファイルは --screen で確認する (#50)。

    headless スモークは capture_engine を 1 行も実行しない。ここが
    headless だけで守られていたため、「実機で確認した」という記録が
    実態を伴っていなかった。
    """
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "core/capture_engine.py")

    code, out, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err or out
    calls = marker.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1, calls
    assert "--screen" in calls[0]


@needs_sh
def test_headless_only_change_does_not_occupy_the_desktop(repo, tmp_path):
    """headless で確認できる変更で、画面を占有するスモークを走らせない。"""
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "core/headless_capture.py")

    code, out, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err or out
    calls = marker.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1, calls
    assert "--screen" not in calls[0]


@needs_sh
def test_touching_both_paths_runs_both_smokes(repo, tmp_path):
    """両方に触ったら両方確認する。片方で済ませない。"""
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "core/capture_engine.py", "core/headless_capture.py")

    code, out, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err or out
    calls = marker.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, calls
    assert sum("--screen" in c for c in calls) == 1


@needs_sh
def test_a_file_in_both_lists_runs_both_smokes(repo, tmp_path):
    """両方の一覧に入っているファイル 1 つで、両方のスモークが走ること。

    共有部分を片方だけで守ると、もう片方でしか確かめられない部分が
    無検証のまま通る (#50)。
    """
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "cli.py")

    code, out, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err or out
    calls = marker.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, calls
    assert sum("--screen" in c for c in calls) == 1


@needs_sh
def test_the_smoke_script_itself_is_checked_on_both_paths(repo, tmp_path):
    """スモーク自身を変えたら両方走ること。

    --screen の配線（--no-headless を組み立てる所）は画面スモークでしか
    通らない。headless 側だけで守ると、--no-headless のタイプミスが
    緑のまま通る。
    """
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "scripts/smoke_capture.py")

    code, out, err = _run_hook(repo, "origin", "u", stdin=_push_input(repo))
    assert code == 0, err or out
    calls = marker.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, calls


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


@needs_sh
def test_branch_deletion_push_does_not_run_smoke(repo, tmp_path):
    """ブランチ削除の push（local_sha が全ゼロ）は対象外。"""
    marker = tmp_path / "ran.txt"
    _git(repo, "config", "kindleshot.python", str(_stub_python(repo, marker)))
    _commit_change(repo, "cli.py")

    stdin = f"(delete) {ZERO} refs/heads/main {ZERO}\n"
    code, _, err = _run_hook(repo, "origin", "u", stdin=stdin)
    assert code == 0, err
    assert not marker.exists()


def _hook_files(name):
    """フックの <name>_RE が列挙しているファイル名。"""
    with open(HOOK, encoding="utf-8") as f:
        text = f.read()
    m = re.search(rf"^{name}_RE='\^\((.+)\)\$'$", text, re.MULTILINE)
    assert m, f"{name}_RE を読み取れない"
    return {alt.replace("\\.", ".") for alt in m.group(1).split("|")}


def _watch_re_files():
    """フックが監視しているファイル名（画面 + headless）。"""
    return _hook_files("SCREEN") | _hook_files("HEADLESS")


# スモークで検証できないと AGENTS.md が明記している見出し。ここから下は
# 「監視しないと決めたもの」なので、フックの一覧と突き合わせない
_UNVERIFIABLE_HEADING = "**どちらのスモークでも検証できないもの**"


def _section(start, end):
    with open(AGENTS, encoding="utf-8") as f:
        text = f.read()
    return text.split(start, 1)[1].split(end, 1)[0]


def _files_in(chunk):
    """箇条書きが主語にしているファイル名。

    **1 行につき最初のパスだけを見る。** 2 つ目以降は「どこから呼ばれるか」の
    説明で出てくるので、主語と混ぜると一覧が狂う。
    """
    names = set()
    for line in chunk.splitlines():
        if not line.startswith("- "):
            continue
        found = re.findall(r"`([\w/]+\.py)`", line)
        if found:
            names.add(found[0])
    return names


_SCREEN_HEADING = "**画面キャプチャ経路**"
_HEADLESS_HEADING = "**headless 経路**"


def _documented_files():
    """AGENTS.md「実機スモーク」節が、監視対象として挙げているファイル名。"""
    return _files_in(_section("### 5. 実機スモーク", _UNVERIFIABLE_HEADING))


def _documented_screen_files():
    return _files_in(_section(_SCREEN_HEADING, _HEADLESS_HEADING))


def _documented_headless_files():
    return _files_in(_section(_HEADLESS_HEADING, _UNVERIFIABLE_HEADING))


def _documented_unverifiable():
    """検証できないと明記されているファイル名。"""
    return _files_in(_section(_UNVERIFIABLE_HEADING, "#### 対象は Cloud Reader"))


def test_watch_list_matches_the_documentation():
    """フックと AGENTS.md の一覧がずれていないこと。

    「AGENTS.md の一覧と合わせること」というコメントだけでは守られず、
    本番経路の headless_capture.py が実際に漏れていた（#47）。
    """
    assert _watch_re_files() == _documented_files()


def test_the_unverifiable_files_are_documented_and_not_gated():
    """検証できないファイルを、黙って監視対象に入れない (#50)。

    core/amazon_signin.py は reader_navigator から呼ばれるが、サインアウト
    していないと通らない。監視対象に足すと「検証できないのに push が
    ブロックされる」だけになる。**外すこと自体は正しいが、外したことが
    どこにも書かれていないと穴が隠れる**ので、AGENTS.md に明記して
    ここで固定する。
    """
    unverifiable = _documented_unverifiable()
    assert "core/amazon_signin.py" in unverifiable
    assert unverifiable & _watch_re_files() == set()


# 箇条書きの中で「どこから呼ばれるか」の説明として出てくるだけのパス。
# 主語でも監視対象でもないので、下のテストの対象から外す。
_CONTEXT_ONLY = {"ui/steps/capture_step.py", "tests/test_pre_push_hook.py"}


def test_every_path_in_the_documentation_is_accounted_for():
    """節の中に出てくる .py が、主語か・監視対象か・説明用かのどれかであること。

    _files_in は 1 行の**最初の**パスだけを主語として拾う。そのため
    「- `a.py` と `b.py`」と書くと b.py が黙って消え、**ドキュメントには
    載っているのにフックは見ていない**状態が緑で通る。1 行 1 ファイルという
    書き方の規約を、ここで強制する。
    """
    chunk = _section("### 5. 実機スモーク", "#### 対象は Cloud Reader")
    mentioned = set(re.findall(r"`([\w/]+\.py)`", chunk))
    known = _documented_files() | _documented_unverifiable() | _watch_re_files() | _CONTEXT_ONLY
    assert mentioned <= known, f"主語にも監視対象にもなっていないパス: {mentioned - known}"


def test_each_list_matches_its_own_section_in_the_documentation():
    """どちらの一覧にどのファイルが入っているかまで一致させる。

    **合計だけ見ても足りない。** 2 つの一覧を入れ替えても合計は変わらない
    ので、「どちらの経路で検証するか」というこの区別そのものが守られない。
    """
    assert _hook_files("SCREEN") == _documented_screen_files()
    assert _hook_files("HEADLESS") == _documented_headless_files()


def test_the_screen_only_files_are_watched_by_the_screen_smoke():
    """headless が 1 行も実行しない、あるいは一部しか実行しないファイル (#50)。

    **重なりは禁止しない。** 規準は「どちらか一方に入れる」ではなく
    「検証できる経路すべてに入れる」。共有部分を headless 側だけに置くと、
    画面側でしか確かめられない部分が無検証のまま通る。
    """
    screen = _hook_files("SCREEN")
    # headless が 1 行も実行しない
    assert {
        "core/capture_engine.py",
        "core/capture_runner.py",
        "core/reader_navigator.py",
    } <= screen
    # headless は prevent_sleep / allow_sleep しか呼ばない。ウィンドウ探索も
    # 画面座標もモニタ列挙も画面経路でしか通らない
    assert "core/win32_utils.py" in screen
    # headless が使うのは get_profile / to_dict だけ
    assert "core/capture_profiles.py" in screen
    # run --no-headless の配線は --screen でしか通らない
    assert "cli.py" in screen
