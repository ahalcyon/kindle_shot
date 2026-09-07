"""scripts/watch_ci.py の契約テスト (#58)

PR の CI 完了を利用者に知らせてもらう状態が続いたので、待ち合わせを 1 コマンドに
固めた。gh は叩かず、run_gh を差し替えて判定と終了コードだけを固定する。
"""

import os
import sys

import pytest

# CWD 相対だと rootdir 以外から起動したとき collection に失敗する。
# 既存の tests/ もファイル位置基準で組み立てている
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import watch_ci  # noqa: E402


def rows(*pairs):
    return [
        {"name": name, "bucket": bucket, "link": f"https://x/job/{i}"}
        for i, (name, bucket) in enumerate(pairs, 1)
    ]


def fake_gh(monkeypatch, responses):
    """run_gh の戻り値を呼び出し順に差し替える。最後の値を以後も返す。"""
    seq = list(responses)
    calls = []

    def fake(args):
        calls.append(args)
        # 失敗ログの取得はここでは扱わない
        if args[:2] == ["run", "view"]:
            return "log line 1\nlog line 2", "", 0
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(watch_ci, "run_gh", fake)
    return calls


def ok(rows_):
    import json

    return json.dumps(rows_), "", 0


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # watch_ci.time は標準ライブラリの time そのもの。monkeypatch が戻すので
    # 実害はないが、プロセス全体の time.sleep を潰していることは意識しておく
    monkeypatch.setattr(watch_ci.time, "sleep", lambda _s: None)


def test_returns_zero_when_everything_passes(monkeypatch):
    fake_gh(monkeypatch, [ok(rows(("unit", "pass"), ("lint", "pass")))])
    assert watch_ci.watch(1, interval=1, timeout=60) == 0


def test_uses_only_fields_gh_accepts(monkeypatch):
    """無効なフィールド名を混ぜるとコマンドごと失敗する（実際に踏んだ）。"""
    calls = fake_gh(monkeypatch, [ok(rows(("unit", "pass")))])
    watch_ci.fetch_checks(1)
    assert calls == [["pr", "checks", "1", "--json", "name,bucket,link"]]


def test_cancel_alone_is_not_reported_as_failure_immediately(monkeypatch):
    """cancel-in-progress で古い run が打ち切られた瞬間を「CI 失敗」と誤報しない。

    監視中に修正を push すると、新しい run のチェックが登録されるまでの一瞬だけ
    「全部 terminal・cancel あり」に見える。
    """
    fake_gh(
        monkeypatch,
        [
            ok(rows(("unit", "cancel"), ("lint", "cancel"))),
            ok(rows(("unit", "pending"), ("lint", "pending"))),
            ok(rows(("unit", "pass"), ("lint", "pass"))),
            ok(rows(("unit", "pass"), ("lint", "pass"))),
        ],
    )
    assert watch_ci.watch(1, interval=1, timeout=60) == 0


def test_a_real_cancel_is_still_reported(monkeypatch, capsys):
    """本当に打ち切られたままなら失敗として返す（注意書き付き）。"""
    fake_gh(monkeypatch, [ok(rows(("unit", "cancel")))])
    assert watch_ci.watch(1, interval=1, timeout=60) == 1
    assert "打ち切られた" in capsys.readouterr().out


def test_returns_one_when_a_check_fails(monkeypatch, capsys):
    fake_gh(monkeypatch, [ok(rows(("unit", "fail"), ("lint", "pass")))])
    assert watch_ci.watch(1, interval=1, timeout=60) == 1
    # 失敗したら毎回手で叩いていたログを出す
    assert "log line 2" in capsys.readouterr().out


def test_cancelled_counts_as_failure(monkeypatch):
    fake_gh(monkeypatch, [ok(rows(("unit", "cancel")))])
    assert watch_ci.watch(1, interval=1, timeout=60) == 1


def test_waits_while_pending_then_reports(monkeypatch):
    fake_gh(
        monkeypatch,
        [
            ok(rows(("unit", "pending"))),
            ok(rows(("unit", "pending"))),
            ok(rows(("unit", "pass"))),
            ok(rows(("unit", "pass"))),
        ],
    )
    assert watch_ci.watch(1, interval=1, timeout=60) == 0


def test_gives_up_when_gh_keeps_failing(monkeypatch, capsys):
    """失敗し続けたら諦める。しかも黙って再試行しない。"""
    fake_gh(monkeypatch, [("", "boom", 1)])
    assert watch_ci.watch(99999, interval=1, timeout=3600) == 2
    err = capsys.readouterr().err
    assert err.count("取得に失敗しました") == watch_ci.MAX_CONSECUTIVE_ERRORS


def test_a_single_hiccup_does_not_give_up(monkeypatch):
    """一時的な失敗で諦めない（ネットワークの瞬断など）。"""
    fake_gh(monkeypatch, [("", "boom", 1), ok(rows(("unit", "pass")))])
    assert watch_ci.watch(1, interval=1, timeout=60) == 0


def test_no_checks_reported_is_not_an_error(monkeypatch):
    """チェックがまだ登録されていない状態と、gh の失敗を区別する。"""
    fake_gh(monkeypatch, [("", "no checks reported on the 'x' branch", 1), ok(rows(("u", "pass")))])
    assert watch_ci.watch(1, interval=1, timeout=60) == 0


def test_times_out_when_nothing_ever_finishes(monkeypatch, capsys):
    """self-hosted runner が止まっているとジョブは待つだけなので上限で切る。"""
    fake_gh(monkeypatch, [ok(rows(("e2e (self-hosted)", "pending")))])
    clock = iter([0, 0, 100, 100, 200, 200])
    monkeypatch.setattr(watch_ci.time, "monotonic", lambda: next(clock, 999))
    assert watch_ci.watch(1, interval=1, timeout=50) == 3
    out = capsys.readouterr().out
    assert "e2e (self-hosted)" in out
    assert "runner" in out


def test_rejects_a_non_numeric_pr(monkeypatch, capsys):
    monkeypatch.setattr(watch_ci.shutil, "which", lambda _n: "/usr/bin/gh")
    assert watch_ci.main(["feature/x"]) == 2
    assert "数字で" in capsys.readouterr().err


def test_resolves_the_pr_of_the_current_branch(monkeypatch):
    """番号を省略できる。控えなくてよいぶん、起動し忘れる余地が減る。"""
    calls = fake_gh(monkeypatch, [('{"number": 42, "title": "t", "state": "OPEN"}', "", 0)])
    assert watch_ci.resolve_pr(None) == (42, "#42 t")
    assert calls[0] == ["pr", "view", "--json", "number,title,state"]


def test_reports_when_the_pr_does_not_exist(monkeypatch, capsys):
    """恒久的な誤りは起動時に弾く。ループに入って 1 時間待たない。"""
    monkeypatch.setattr(watch_ci.shutil, "which", lambda _n: "/usr/bin/gh")
    fake_gh(monkeypatch, [("", "no pull requests found", 1)])
    assert watch_ci.main(["99999"]) == 2
    assert "特定できません" in capsys.readouterr().err


def test_gh_timeout_is_reported_as_an_error(monkeypatch):
    """gh が固まっても watch_ci ごと止まらない。"""

    def boom(*_a, **_k):
        raise watch_ci.subprocess.TimeoutExpired(cmd="gh", timeout=watch_ci.GH_TIMEOUT)

    monkeypatch.setattr(watch_ci.subprocess, "run", boom)
    _out, err, code = watch_ci.run_gh(["pr", "checks", "1"])
    assert code == 124
    assert "応答しませんでした" in err


def test_rejects_a_bad_interval(monkeypatch):
    monkeypatch.setattr(watch_ci.shutil, "which", lambda _n: "/usr/bin/gh")
    assert watch_ci.main(["1", "--interval", "0"]) == 2


def test_reports_when_gh_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(watch_ci.shutil, "which", lambda _n: None)
    assert watch_ci.main(["1"]) == 2
    assert "gh" in capsys.readouterr().err
