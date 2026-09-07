"""PR の CI を見張り、全チェックが終わったら結果を出して終わる

PR を出したあと、CI が通ったことを利用者から知らされる状態が続いた (#58)。
待ち合わせを起動し忘れる／シェルの ``&`` でデタッチして通知に繋がらない、の
2 通りで同じことが起きる。1 コマンドに固めて、起動の仕方を間違える余地を減らす。

使い方:
    python scripts/watch_ci.py 55
    python scripts/watch_ci.py 55 --timeout 3600 --interval 30

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

DEFAULT_INTERVAL = 30
DEFAULT_TIMEOUT = 3600

# 失敗したジョブのログはそのまま出すと長いので、末尾だけ見せる
LOG_TAIL_LINES = 25

# gh がこの回数続けて失敗したら、待っても状況は変わらないと判断して諦める。
# 「まだチェックが登録されていない」との区別が付かないまま待ち続けると、
# PR 番号の打ち間違いで 1 時間黙ることになる。
MAX_CONSECUTIVE_ERRORS = 3

# gh pr checks が受け付けるフィールドだけを指定する。無効な名前を混ぜると
# コマンドごと失敗し、「チェックがまだ無い」と区別が付かない（実際にやった）
CHECK_FIELDS = "name,bucket,link"


def run_gh(args):
    """gh を叩いて (stdout, stderr, returncode) を返す。"""
    try:
        out = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as e:
        return "", str(e), 127
    return out.stdout, out.stderr, out.returncode


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
    link = row.get("link") or ""
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
    while True:
        rows, error = fetch_checks(pr)
        if error is not None:
            errors += 1
            if errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"PR #{pr} のチェックを取得できません: {error}", file=sys.stderr)
                return 2
        else:
            errors = 0
            seen_any = seen_any or bool(rows)
            if rows and not pending(rows):
                return 1 if report(pr, rows) else 0
        if time.monotonic() >= deadline:
            waiting = [r.get("name", "?") for r in (rows or []) if r.get("bucket") == "pending"]
            if not seen_any:
                print(f"PR #{pr}: チェックが 1 つも登録されないまま時間切れになりました。")
            else:
                print(f"PR #{pr}: 時間切れです。まだ終わっていません: {', '.join(waiting)}")
            print(
                "self-hosted runner (windows-local) が動いていない可能性があります。"
                "PC の電源とランナーの状態を確認してください。"
            )
            return 3
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PR の CI が終わるまで待って結果を出す")
    parser.add_argument("pr", help="PR 番号")
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
    if not str(args.pr).isdigit():
        print(f"PR 番号は数字で指定してください: {args.pr}", file=sys.stderr)
        return 2
    if args.interval < 1 or args.timeout < 1:
        print("--interval と --timeout は 1 以上で指定してください", file=sys.stderr)
        return 2
    return watch(args.pr, interval=args.interval, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
