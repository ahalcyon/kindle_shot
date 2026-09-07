"""PR の CI を見張り、全チェックが終わったら結果を出して終わる

PR を出したあと、CI が通ったことを利用者から知らされる状態が続いた (#58)。
待ち合わせを起動し忘れる／シェルの ``&`` でデタッチして通知に繋がらない、の
2 通りで同じことが起きる。1 コマンドに固めて、起動の仕方を間違える余地を減らす。

使い方:
    python3 scripts/watch_ci.py         # 今のブランチの PR を見る
    python3 scripts/watch_ci.py 55
    python3 scripts/watch_ci.py 55 --timeout 3600 --interval 30

**エージェントは、ハーネスが追跡するバックグラウンド実行で起動すること。**
シェルの ``&`` でデタッチすると、終了しても誰も気づかない（#58 の原因）。

``gh`` が要る。このリポジトリの開発環境では ``gh`` は WSL 側にしか入っていない
ので、Windows の ``kindle_env`` ではなく **WSL の python3 で実行する**。

終了コード:
    0  全チェックが成功
    1  失敗したチェックがある（末尾にログの要点を出す）
    2  引数が不正、または gh を続けて叩けない
    3  時間切れ（self-hosted runner が動いていない等）
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.parse

DEFAULT_INTERVAL = 30
DEFAULT_TIMEOUT = 3600

# gh 自体が固まることがある。watch_ci の --timeout はポーリングの上限であって
# 1 回の gh 呼び出しには効かないので、こちらにも上限を置く
GH_TIMEOUT = 60

# 「全チェックが終わった」をこの秒数あけて 2 回続けて観測したときだけ確定する。
# ci.yml は cancel-in-progress を有効にしているので、監視中に修正を push すると
# 古い run が cancel になり、新しい run のチェックが登録されるまでの一瞬だけ
# 「全部 terminal・cancel あり」に見える。1 回で決めると CI 失敗と誤報する。
# needs: で連なるジョブ（test -> e2e）の登録の隙間も同じ形で塞げる。
CONFIRM_DELAY = 10

# 失敗したジョブのログはそのまま出すと長いので、末尾だけ見せる
LOG_TAIL_LINES = 25

# gh がこの回数続けて失敗したら諦める。恒久的な誤り（PR 番号の打ち間違い等）は
# 起動時の存在確認で弾くので、ここで相手にするのは一時的な失敗
# （API の 5xx・レート制限・DNS の瞬断）だけ。短く切りすぎない。
MAX_CONSECUTIVE_ERRORS = 8

# gh pr checks が受け付けるフィールドだけを指定する。無効な名前を混ぜると
# コマンドごと失敗し、「チェックがまだ無い」と区別が付かない（実際にやった）
CHECK_FIELDS = "name,bucket,link"


def run_gh(args):
    """gh を叩いて (stdout, stderr, returncode) を返す。"""
    try:
        out = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # ここで固まると watch_ci の --timeout も効かず、
        # 「誰も気づかない」を潰すための道具が気づかれずに止まる
        return "", f"gh が {GH_TIMEOUT} 秒で応答しませんでした", 124
    except OSError as e:
        return "", str(e), 127
    return out.stdout, out.stderr, out.returncode


def resolve_pr(pr):
    """PR の存在を確かめ、(番号, 表示名) を返す。見つからなければ (None, 理由)。

    起動時に一度だけ確かめる。恒久的な誤り（番号の打ち間違い、別リポジトリ）を
    ここで弾いておけば、ループ中のエラーは全部「一時的」として寛容に扱える。
    pr が None なら今のブランチの PR を解決する（番号を控えなくてよい）。
    """
    args = ["pr", "view", "--json", "number,title,state"]
    if pr is not None:
        args.insert(2, str(pr))
    stdout, stderr, code = run_gh(args)
    if code != 0:
        return None, (stderr or stdout).strip() or "PR が見つかりません"
    try:
        info = json.loads(stdout)
    except ValueError:
        return None, f"gh の出力を JSON として読めません: {stdout[:200]}"
    return info.get("number"), f"#{info.get('number')} {info.get('title', '')}"


def fetch_checks(pr):
    """PR のチェック一覧を返す。

    Returns:
        (rows, error): 取得できたら (リスト, None)。チェックがまだ 1 つも
        登録されていなければ ([], None)。gh が失敗したら (None, メッセージ)。
    """
    stdout, stderr, code = run_gh(["pr", "checks", str(pr), "--json", CHECK_FIELDS])
    if code != 0:
        # チェックが 1 つも無い PR でも gh は非ゼロで終わる。メッセージで見分ける
        if "no checks reported" in (stderr + stdout).lower():
            return [], None
        return None, (stderr or stdout).strip()
    try:
        return json.loads(stdout), None
    except ValueError:
        return None, f"gh の出力を JSON として読めません: {stdout[:200]}"


def pending(rows):
    return [r for r in rows if r.get("bucket") == "pending"]


def failed(rows):
    return [r for r in rows if r.get("bucket") in ("fail", "cancel")]


def failing_log(row):
    """失敗したジョブのログの末尾。取れなければ空文字。"""
    # クエリやフラグメントが付く形（?check_suite_focus=true, #step:3:1）でも
    # id を壊さないよう、パスだけ見る
    link = urllib.parse.urlparse(row.get("link") or "").path
    job_id = link.rsplit("/", 1)[-1] if "/job/" in link else ""
    if not job_id:
        return ""
    stdout, _stderr, _code = run_gh(["run", "view", "--job", job_id, "--log-failed"])
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    return "\n".join(lines[-LOG_TAIL_LINES:])


def report(pr, rows):
    """結果を人が読める形で出す。失敗があれば True。"""
    print(f"PR #{pr} のチェック:")
    for row in sorted(rows, key=lambda r: r.get("name") or ""):
        print(f"  {row.get('bucket', '?'):<8} {row.get('name', '?')}")
    bad = failed(rows)
    if not bad:
        print("すべて成功しました。")
        return False
    if any(r.get("bucket") == "cancel" for r in bad):
        print("  （cancel は、新しい push で打ち切られた可能性があります）")
    for row in bad:
        print(f"\n--- {row.get('name', '?')} の失敗ログ（末尾 {LOG_TAIL_LINES} 行）---")
        print(failing_log(row) or "（ログを取得できませんでした）")
        print(f"--- {row.get('link', '')}")
    return True


def watch(pr, *, interval, timeout):
    # time.sleep / time.monotonic は既定引数で受け取らない。既定引数は定義時に
    # 束縛されるので、テストからモンキーパッチしても効かない（実際に効かず、
    # テストが 56 秒かかった）
    deadline = time.monotonic() + timeout
    errors = 0
    seen_any = False
    settled = None  # 「全部終わった」と観測したときのチェック名の集合
    last_rows = []
    while True:
        rows, error = fetch_checks(pr)
        if error is not None:
            errors += 1
            # 黙って再試行しない。1 時間黙ってから諦めるのは #58 の問題そのもの
            print(
                f"PR #{pr}: 取得に失敗しました（{errors}/{MAX_CONSECUTIVE_ERRORS}）: {error}",
                file=sys.stderr,
                flush=True,
            )
            if errors >= MAX_CONSECUTIVE_ERRORS:
                return 2
            settled = None
        else:
            errors = 0
            last_rows = rows
            seen_any = seen_any or bool(rows)
            if rows and not pending(rows):
                names = {r.get("name") for r in rows}
                if settled == names:
                    return 1 if report(pr, rows) else 0
                # 1 回では決めない。cancel-in-progress で古い run が打ち切られた
                # 直後や、needs: の後続ジョブが登録される直前がこの形になる
                settled = names
                if time.monotonic() < deadline:
                    time.sleep(min(CONFIRM_DELAY, interval))
                    continue
            else:
                settled = None
        if time.monotonic() >= deadline:
            waiting = [r.get("name", "?") for r in last_rows if r.get("bucket") == "pending"]
            if not seen_any:
                print(f"PR #{pr}: チェックが 1 つも登録されないまま時間切れになりました。")
            else:
                print(
                    f"PR #{pr}: 時間切れです。まだ終わっていません: {', '.join(waiting) or '(不明)'}"
                )
            print(
                "self-hosted runner (windows-local) が動いていない可能性があります。"
                "PC の電源とランナーの状態を確認してください。"
            )
            return 3
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PR の CI が終わるまで待って結果を出す")
    parser.add_argument("pr", nargs="?", help="PR 番号（省略時は今のブランチの PR）")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        metavar="SEC",
        help=f"ポーリング間隔（既定: {DEFAULT_INTERVAL} 秒）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"打ち切りまでの秒数（既定: {DEFAULT_TIMEOUT}）。"
        "self-hosted runner が止まっているとジョブは待つだけなので上限を置く",
    )
    args = parser.parse_args(argv)
    # gh が無いまま待ち始めると、何度も失敗してから諦めることになる。
    # 実際に「Windows の python から gh が見えない」で 90 秒黙った
    if shutil.which("gh") is None:
        print(
            "gh が見つかりません。gh CLI が入っている環境で実行してください"
            "（このリポジトリでは WSL 側の python3）",
            file=sys.stderr,
        )
        return 2
    if args.pr is not None and not str(args.pr).isdigit():
        print(f"PR 番号は数字で指定してください: {args.pr}", file=sys.stderr)
        return 2
    if args.interval < 1 or args.timeout < 1:
        print("--interval と --timeout は 1 以上で指定してください", file=sys.stderr)
        return 2
    number, label = resolve_pr(args.pr)
    if number is None:
        print(f"PR を特定できません: {label}", file=sys.stderr)
        return 2
    print(
        f"{label} を監視します（interval={args.interval}s / timeout={args.timeout}s）", flush=True
    )
    return watch(number, interval=args.interval, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
