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


def _watch_re_files():
    """フックの WATCH_RE が列挙しているファイル名。"""
    with open(HOOK, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^WATCH_RE='\^\((.+)\)\$'$", text, re.MULTILINE)
    assert m, "WATCH_RE を読み取れない"
    return {alt.replace("\\.", ".") for alt in m.group(1).split("|")}


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


_WATCHED_HEADING = "**pre-push が強制するもの**"


def _documented_files():
    """AGENTS.md「実機スモーク」節が、監視対象として挙げているファイル名。"""
    return _files_in(_section(_WATCHED_HEADING, _UNVERIFIABLE_HEADING))


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


def _bullets(chunk):
    """箇条書きを 1 件ずつ返す（継続行を畳む）。"""
    items: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("- "):
            items.append(line)
        elif items and line.startswith("  "):
            items[-1] += " " + line.strip()
    return items


def test_each_bullet_names_exactly_one_file():
    """一覧は 1 行 1 ファイルで書く。

    _files_in は 1 行の**最初の**パスだけを主語として拾う。そのため
    「- `a.py` と `b.py`」と書くと b.py が黙って消え、**ドキュメントには
    載っているのにフックは見ていない**状態が緑で通る。
    """
    # 「検証できないもの」の箇条書きは、なぜ検証できないかの説明で呼び出し元の
    # ファイル名が出るので対象外。監視対象の一覧だけを見る
    chunk = _section(_WATCHED_HEADING, _UNVERIFIABLE_HEADING)
    for item in _bullets(chunk):
        found = re.findall(r"`([\w/]+\.py)`", item)
        assert len(found) == 1, f"監視対象の箇条書きが 1 ファイルでない: {item}"


def test_every_path_in_the_lists_is_accounted_for():
    """一覧に出てくる .py が、主語か・監視対象か・説明用かのどれかであること。"""
    chunk = "\n".join(
        (
            _section(_WATCHED_HEADING, _UNVERIFIABLE_HEADING),
            _section(_UNVERIFIABLE_HEADING, "### 画面キャプチャ経路"),
        )
    )
    mentioned = set(re.findall(r"`([\w/]+\.py)`", chunk))
    known = _documented_files() | _documented_unverifiable() | _watch_re_files() | _CONTEXT_ONLY
    assert mentioned <= known, f"主語にも監視対象にもなっていないパス: {mentioned - known}"


def test_the_screen_path_is_not_gated(repo=None):
    """画面キャプチャ経路のファイルを push のゲートにしないこと (#50)。

    headless スモークはこれらを 1 行も実行しない。監視対象に入れたまま
    headless だけで守ると「緑なのに何も確かめていない」偽陰性になる。
    画面経路でしか確かめられない以上、**ゲートから外して外したと書く**
    のが正しい。--screen は手で流す道具として残してある。
    """
    screen_only = {
        "core/capture_engine.py",
        "core/capture_runner.py",
        "core/reader_navigator.py",
        "core/win32_utils.py",
    }
    assert screen_only & _watch_re_files() == set()
    assert screen_only <= _documented_unverifiable()


# 箇条書きの中で「どこから呼ばれるか」の説明として出てくるだけのパス。
# 主語でも監視対象でもないので、下のテストの対象から外す。
_CONTEXT_ONLY = {"ui/steps/capture_step.py", "tests/test_pre_push_hook.py"}
