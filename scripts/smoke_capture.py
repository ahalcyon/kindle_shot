"""実機スモーク: Kindle Cloud Reader で数ページだけ取って PDF まで通す

キャプチャ経路（Win32 のウィンドウ検出・画面キャプチャ・キーストローク送出）は
ユニットテストでも CI でもカバーできない。ここだけは実機で確認するしかないため、
その確認を 1 コマンドに固めたもの。.githooks/pre-push から呼ばれる。

cli.py run を JSON Lines で起動し、結果を機械的に検証する:
- 終了コードが 0
- manifest.json の total_pages が指定どおり、stopped_reason が max_pages
- 取得画像が全て別物（同じページが並んでいない = ページ送りが効いている）
- 出力 PDF が存在し、ページ数が合っている

使い方:
    python scripts/smoke_capture.py --asin B0XXXXXXXX
    python scripts/smoke_capture.py --asin B0XXXXXXXX --pages 5 --keep

注意:
- 実行中は前面ウィンドウとマウスを占有する
- 先頭ページへの巻き戻しは Kindle の読書位置 (Whispersync) を動かす
"""

import argparse
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


def build_run_argv(python, asin, out, pages, fmt="image_pdf"):
    """cli.py run の argv を組み立てる。"""
    return [
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
        "--json",
    ]


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


def run_smoke(asin, out, pages, python=None, echo=print):
    """スモークを 1 本実行し、問題の一覧を返す。空なら合格。"""
    python = python or sys.executable
    argv = build_run_argv(python, asin, out, pages)
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
        problems.extend(check_manifest(manifest, pages))
        problems.extend(check_pages_differ(list_pngs(capture_dir(out))))

    pdf = output_pdf(out)
    if not os.path.exists(pdf):
        problems.append(f"出力 PDF が無い: {pdf}")
    else:
        count = pdf_page_count(pdf)
        if count is not None and count != pages:
            problems.append(f"PDF のページ数が {pages} ではなく {count}")
    return problems


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
        problems = run_smoke(args.asin, out, args.pages, python=args.python)
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

    print(f"\n実機スモーク: OK（{args.pages} ページ取得・PDF 生成まで確認）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
