"""実機スモーク: Kindle Cloud Reader で数ページだけ取って PDF まで通す

キャプチャ経路（Win32 のウィンドウ検出・画面キャプチャ・キーストローク送出）は
ユニットテストでも CI でもカバーできない。ここだけは実機で確認するしかないため、
その確認を 1 コマンドに固めたもの。.githooks/pre-push から呼ばれる。

cli.py run を JSON Lines で起動し、結果を機械的に検証する:
- 終了コードが 0
- manifest.json の total_pages が指定どおり、stopped_reason が max_pages
- 取得画像が全て別物（同じページが並んでいない = ページ送りが効いている）
- 出力 PDF が存在し、ページ数が合っている

**経路が 2 つあり、既定の headless は片方しか通らない。**

    headless  core/headless_capture.py / core/headless_browser.py
    画面      core/capture_engine.py / core/capture_runner.py /
              core/reader_navigator.py

既定は headless。画面もデスクトップセッションも要らないので、実行中に PC を
使えて、push のたびに走っても作業の邪魔にならない。

`--screen` を付けると画面キャプチャ経路を通す。**デスクトップを占有する**
（ブラウザを全画面にし、マウスを別モニタへ退避し、画面を撮る）。

**pre-push が走らせるのは headless だけで、`--screen` は要求しない** (#50)。
画面経路は本番が通らない（`kindle_cloud` は headless 固定）うえ、検証に
デスクトップセッションが要るのでゲートにすると `--no-verify` が常態化する。
画面経路に触ったときに手で流す道具。

使い方:
    python scripts/smoke_capture.py --asin B0XXXXXXXX
    python scripts/smoke_capture.py --asin B0XXXXXXXX --screen
    python scripts/smoke_capture.py --asin B0XXXXXXXX --pages 5 --keep

注意: 先頭ページへの巻き戻しは Kindle の読書位置 (Whispersync) を動かす。
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO_ROOT, "cli.py")

TITLE = "smoke"

# やり直す価値のある停止理由。ページが進まずに止まった形だけを拾う。
# **何で失敗してもやり直すのは駄目。** 引数の誤りや cli.py の異常終了は
# 2 回目も同じように落ちるので、--screen 1 本ぶん (30 秒) を捨てるだけになり、
# 本当に壊れているときの発覚が遅れる。
#
# **表示文言ではなく manifest の値で判定する。** check_manifest が付ける説明の
# 文字列と照合していたことがあったが、文言を変えるとテストは緑のまま
# やり直しだけが黙って死ぬ。この PR が潰そうとしている穴そのものだった。
RETRYABLE_REASON = "timeout"

# 終了コード
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2


# ------------------------------------------------------------
# パス（run_book のフォルダ構成に合わせる）
# ------------------------------------------------------------


def smoke_asin_from_git_config():
    """git config kindleshot.smokeAsin を読む。

    ASIN は秘密ではないので .env には置かない。一方でどの本を使うかは
    開発者ごとに違う（自分が所有する本である必要がある）ので、
    リポジトリにも入れずクローンごとの git config に持つ。
    pre-push フックが読むのと同じ場所。
    """
    try:
        result = subprocess.run(
            ["git", "config", "kindleshot.smokeAsin"],
            cwd=REPO_ROOT,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def capture_dir(out, title=TITLE):
    """キャプチャ画像と manifest.json が置かれるフォルダ。"""
    return os.path.join(out, title)


def trimmed_dir(out, title=TITLE):
    """トリミング後の画像フォルダ。"""
    return os.path.join(out, title + "_trimmed")


def output_pdf(out, title=TITLE):
    """変換後の PDF。"""
    return os.path.join(out, title + ".pdf")


# ------------------------------------------------------------
# 検証（副作用なし。tests/test_smoke_capture.py が直接呼ぶ）
# ------------------------------------------------------------


def check_manifest(manifest, expected_pages):
    """manifest.json の内容を検証し、問題の一覧を返す。空なら合格。"""
    problems = []
    total = manifest.get("total_pages")
    if total != expected_pages:
        problems.append(f"total_pages が {expected_pages} ではなく {total}")

    reason = manifest.get("stopped_reason")
    if reason != "max_pages":
        hint = {
            "timeout": "ページが変化しなかった（本が開けていない・ページ送りキーが違う）",
            "user": "実行中に中断された",
            "reader_error": "リーダーが落ちた（最終ページではない）",
        }.get(reason, "")
        problems.append(
            f"stopped_reason が max_pages ではなく {reason}" + (f"（{hint}）" if hint else "")
        )
    return problems


def check_pages_differ(image_paths):
    """取得画像が全て別物かを検証し、問題の一覧を返す。

    同じページを撮り続けていると内容が一致してファイルが同一になる。
    ページ送りが効いているかを見る最も直接的な指標。
    """
    problems = []
    if not image_paths:
        problems.append("画像が 1 枚も無い")
        return problems

    digests: dict[str, list[str]] = {}
    for path in image_paths:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        digests.setdefault(digest, []).append(os.path.basename(path))

    for names in digests.values():
        if len(names) > 1:
            problems.append(f"同一内容の画像がある（ページ送りが効いていない）: {', '.join(names)}")
    return problems


def build_run_argv(python, asin, out, pages, fmt="image_pdf", screen=False):
    """cli.py run の argv を組み立てる。

    screen=False なら headless。画面もデスクトップセッションも要らないので、
    実行中に PC を使えて、push のたびに走っても邪魔にならない。

    screen=True なら画面キャプチャ経路。デスクトップを占有する代わりに、
    headless では 1 行も動かない capture_engine / capture_runner /
    reader_navigator を通す (#50)。
    """
    argv = [
        python,
        CLI,
        "run",
        "--asin",
        asin,
        "--title",
        TITLE,
        "--out",
        out,
        "--format",
        fmt,
        "--max-pages",
        str(pages),
    ]
    if screen:
        # 読み込み待ちは既定 (45 秒) のまま。画面経路はブラウザの描画を待つ
        argv += ["--no-headless", "--json"]
    else:
        # --load-wait は渡さない。headless 側の既定 (DEFAULT_LOAD_WAIT = 12) と
        # 同じ値なので、明示すると既定の解決経路を素通りするだけになる
        argv += ["--headless", "--json"]
    # 検証で manifest.json とキャプチャ画像を読むので消させない
    argv += ["--keep-images"]
    return argv


def pdf_page_count(path):
    """PDF のページ数。読めなければ None。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


# ------------------------------------------------------------
# 実行
# ------------------------------------------------------------


def list_pngs(folder):
    if not os.path.isdir(folder):
        return []
    return [
        os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.lower().endswith(".png")
    ]


def run_smoke(asin, out, pages, python=None, echo=print, screen=False):
    """スモークを 1 本実行し、(問題の一覧, やり直す価値があるか, 実行した argv) を返す。

    problems が空なら合格。retryable は manifest の stopped_reason が
    RETRYABLE_REASON だったかどうかで、表示文言には依存しない。

    argv を返すのは、**どちらの経路を通ったかを呼び出し側が引数から推測しないため**。
    引数から「画面キャプチャで確認しました」と表示していたことがあり、配線を
    落とすと headless で走って画面経路の合格を名乗れた。#50 そのものだった。
    """
    python = python or sys.executable
    argv = build_run_argv(python, asin, out, pages, screen=screen)
    echo("実行: " + " ".join(argv))

    proc = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            echo(line)
            continue
        if event.get("event") in ("error", "run_summary", "result"):
            echo(f"  [{event['event']}] {json.dumps(event, ensure_ascii=False)}")

    problems = []
    retryable = False
    if proc.returncode != 0:
        problems.append(f"cli.py run が終了コード {proc.returncode} で失敗")
        if proc.stderr.strip():
            echo("--- stderr ---")
            echo(proc.stderr)

    manifest_path = os.path.join(capture_dir(out), "manifest.json")
    if not os.path.exists(manifest_path):
        problems.append(f"manifest.json が無い: {manifest_path}")
    else:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        echo(
            "manifest: total_pages={total_pages} stopped_reason={stopped_reason} "
            "duration_seconds={duration_seconds}".format(**manifest)
        )
        retryable = manifest.get("stopped_reason") == RETRYABLE_REASON
        problems.extend(check_manifest(manifest, pages))
        problems.extend(check_pages_differ(list_pngs(capture_dir(out))))

    pdf = output_pdf(out)
    if not os.path.exists(pdf):
        problems.append(f"出力 PDF が無い: {pdf}")
    else:
        count = pdf_page_count(pdf)
        if count is not None and count != pages:
            problems.append(f"PDF のページ数が {pages} ではなく {count}")
    return problems, retryable, argv


# 画面キャプチャ経路は実測で 4 回に 1 回ほど、1 ページ目から先へ送れずに
# 止まる（stopped_reason=timeout）。原因は未調査 (#74)。**失敗を 1 回で
# 確定させると push のゲートとして使えない**ので、1 度だけやり直す。
#
# headless では使わない（run_smoke_with_retry を参照）。
SMOKE_ATTEMPTS = 2


def clear_output(out):
    """前回の出力を消す。消せたかどうかを返す。

    残っていると 2 回目が「前回の結果」を検証して**誤って通る**。
    ignore_errors で握り潰すと、次の実行が「保存先に既存の画像があります」で
    落ちて、引数の問題に見えるエラーになる。
    """
    shutil.rmtree(capture_dir(out), ignore_errors=True)
    shutil.rmtree(trimmed_dir(out), ignore_errors=True)
    if os.path.exists(output_pdf(out)):
        with contextlib.suppress(OSError):
            os.remove(output_pdf(out))
    return not any(os.path.exists(p) for p in (capture_dir(out), trimmed_dir(out), output_pdf(out)))


def run_smoke_with_retry(asin, out, pages, python=None, echo=print, screen=False):
    """スモークを実行し、(問題の一覧, 実行した argv) を返す。

    やり直すのは**画面経路のときだけ**。headless は 4 回に 1 回の不安定さを
    持たないうえ、ゲートが headless 専用になった今 (#50)、
    `stopped_reason=timeout` は「本が開けていないか、ページが送れていない」
    退行そのものの症状（AGENTS.md「manifest の読み方」）。ここでやり直すと、
    間欠的な退行が push を通る率が 50% から 75% に上がる。
    """
    attempts = SMOKE_ATTEMPTS if screen else 1
    problems: list[str] = []
    argv: list[str] = []
    for attempt in range(1, attempts + 1):
        # 1 回目の前にも消す。--out に前回の出力が残っていると 1 回目が
        # 必ず落ち、やり直しの 1 回をそこで使い切ってしまう
        if not clear_output(out):
            # 元の失敗を落とさない。掃除の失敗だけを返すと、なぜ
            # やり直すことになったのかが分からなくなる
            return [*problems, f"前回の出力を消せませんでした: {out}"], argv
        if attempt > 1:
            echo(f"\nやり直します（{attempt}/{attempts}）")
        problems, retryable, argv = run_smoke(
            asin, out, pages, python=python, echo=echo, screen=screen
        )
        if not problems:
            return [], argv
        for p in problems:
            echo(f"  - {p}")
        if attempt >= attempts:
            return problems, argv
        if not retryable:
            echo("やり直しません（2 回目も同じように落ちる失敗です）")
            return problems, argv
    return problems, argv


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="smoke_capture",
        description="Kindle Cloud Reader で数ページだけ取って PDF まで通す実機スモーク",
    )
    parser.add_argument(
        "--asin",
        default=smoke_asin_from_git_config(),
        help="対象の ASIN（省略時は git config kindleshot.smokeAsin）",
    )
    parser.add_argument("--pages", type=int, default=3, help="取得ページ数（既定: 3）")
    parser.add_argument("--out", help="出力先（省略時は一時フォルダを作って最後に消す）")
    parser.add_argument("--keep", action="store_true", help="一時フォルダを消さずに残す")
    parser.add_argument("--python", help="cli.py を動かす Python（省略時は自分と同じ）")
    parser.add_argument(
        "--screen",
        action="store_true",
        help="画面キャプチャ経路で走らせる（デスクトップを占有する）。"
        "headless では 1 行も動かない capture_engine / capture_runner / "
        "reader_navigator を通す",
    )
    args = parser.parse_args(argv)

    if not args.asin:
        print(
            "エラー: ASIN が指定されていません。\n"
            "  --asin B0XXXXXXXX を渡すか、次を設定してください:\n"
            "    git config kindleshot.smokeAsin B0XXXXXXXX",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS
    if args.pages < 2:
        print(
            "エラー: --pages は 2 以上にしてください（ページ送りを確認できません）", file=sys.stderr
        )
        return EXIT_BAD_ARGS

    out = args.out or tempfile.mkdtemp(prefix="kindle_shot_smoke_")
    created_tmp = args.out is None
    try:
        problems, ran_argv = run_smoke_with_retry(
            args.asin, out, args.pages, python=args.python, screen=args.screen
        )
    finally:
        if created_tmp and not args.keep:
            shutil.rmtree(out, ignore_errors=True)
        elif created_tmp:
            print(f"出力を残しました: {out}")

    if problems:
        print("\n実機スモーク: 失敗", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_FAILED

    # **args.screen ではなく実際に走らせた argv から導く。** 引数を見て表示すると、
    # 配線を落としたときに headless で走って「画面キャプチャで確認」と言える
    path = "画面キャプチャ" if "--no-headless" in ran_argv else "headless"
    print(f"\n実機スモーク: OK（{path} / {args.pages} ページ取得・PDF 生成まで確認）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
