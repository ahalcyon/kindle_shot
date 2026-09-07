# CLAUDE.md

作業規約は [AGENTS.md](AGENTS.md) に集約している。作業前に必ず読むこと。

要点:

1. **ブランチ** — issue 番号を含む feature ブランチを切る（`feature/<issue番号>-<内容>`）。
   master に直接コミットしない。issue に紐づかない作業は `docs/...` / `chore/...` とし、
   番号は付けない。
2. **コミットメッセージ** — 件名に issue 番号を入れる（`<type>(#<番号>): <要約>`）。
   本文には「なぜそうしたか」を書き、末尾に `Refs #<番号>`。
   issue に紐づかない作業は `(#番号)` と `Refs` を省く。
3. **キャプチャ経路に触ったら実機スモーク** — `core/capture_*` / `core/headless_*` /
   `core/win32_utils.py` / `core/dpi.py` / `core/reader_navigator.py` / `cli.py` の
   capture・open・run・batch を変更したら、
   `python scripts/smoke_capture.py --asin <ASIN>` を実機で流す。CI はこの層を
   一切カバーしない。Playwright は使えない（DOM ではなく画面を撮っているため）。
   対象は Cloud Reader（PC アプリはプログラムから本を開けない）。
   `git config core.hooksPath .githooks` を設定しておくと pre-push で強制される
   （使う Python は `.githooks/pre-push --check` で確認できる）。
4. **push 前に検証とレビュー** — lint / format / type check / test を通し
   （テストは Windows が必要）、サブエージェントに `origin/master...HEAD` を
   レビューさせ、指摘を反映してから push する。レビュー依頼には差分の内容と
   確認観点を明示して渡すこと。
5. **本番バッチは都度の確認なしに回してよい** — ただし中間生成ファイルを溜めず
   （`--keep-images` を付けない）、ディスクの空きを見ながら進める。
   容量が尽きてから報告するのは失敗。詳細は AGENTS.md「本番バッチの運用」。

6. **指摘を受けたら AGENTS.md に書く** — 会話の中だけで済ませない。
   過去の指摘は AGENTS.md「指摘を受けて決めたこと」にまとまっている。
   作業前に目を通すこと（検証していないことを事実として書かない／実機確認は
   変更した経路を通す／待ち合わせを起動してからターンを終える 等）。

詳細と、このリポジトリ固有の注意（Windows 専用・改行コード・ローカル設定）は
[AGENTS.md](AGENTS.md) を参照。
