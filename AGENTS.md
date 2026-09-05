# AGENTS.md

このリポジトリで AI エージェント（Claude Code 等）が作業するときの規約。
人間向けのセットアップ手順は `README.md` の「セットアップ」、開発手順は「開発メモ」を参照。

コミットメッセージ・PR 説明・コードコメントは日本語で書く。

## 作業の進め方

### 1. ブランチ

GitHub issue に紐づく作業は、**issue 番号を含む feature ブランチ**を切ってから始める。
master に直接コミットしない。作業の種別（機能追加/修正/CI 整備）によらず `feature/` に統一する。

```
feature/<issue番号>-<内容がわかる短い英語>
```

例: issue #1「github actionsの整備」→ `feature/1-github-actions`

issue に紐づかない作業は種別を接頭辞にし、番号は付けない（`docs/...`, `chore/...`）。

### 2. コミットメッセージ

**件名に issue 番号を入れる。** 形式は Conventional Commits に合わせる。

```
<type>(#<issue番号>): <日本語で要約>

<本文: 何をしたかではなく「なぜそうしたか」を書く。
 判断の根拠、採らなかった選択肢とその理由、検証した内容。>

Refs #<issue番号>
```

`type` は `feat` / `fix` / `ci` / `test` / `chore` / `style` / `docs` / `refactor`。
`Co-Authored-By:` などのトレーラーはツールが自動で付与する。

意味のある単位でコミットを分ける。「整形だけのコミット」と「挙動を変えるコミット」は
必ず分離する（レビュー時に差分を追えなくなるため）。

### 3. 変更に伴って必ず直すもの

- 挙動を変えたらテストを追加または更新する（`app.py` / `ui/` の GUI 層は対象外）
- CLI のオプション・終了コード・`config.json` の項目を変えたら
  `README.md` の該当節も更新する（README は 750 行超の事実上の仕様書）

### 4. push 前の検証

`README.md`「開発メモ > 検証コマンド」の lint / format / type check / test を実行する。
`ruff format --check` と `mypy` は CI のゲートなので、通っていない状態で push しない。

**テストの実行には Windows が必要**（後述）。Windows で実行できない環境で作業した場合は、
未検証の範囲を PR 説明に明記し、ユーザーに実行を依頼する。

### 5. 実機スモーク（キャプチャ経路に触るときは必須）

次のいずれかに触れる変更は、**push 前に実機で動作確認する**。
自動テストも CI もこの層を一切カバーしていないため、ここだけは実機確認が要る。

- `core/capture_engine.py` / `core/capture_runner.py` / `core/capture_profiles.py`
- `core/win32_utils.py` / `core/dpi.py` / `core/reader_navigator.py`
- `core/boundary_detector.py` の境界検出（トリミングの純ロジックは対象外）
- `cli.py` の `capture` / `open` / `run` / `batch` / `check` コマンド

**Playwright などのブラウザ自動化は使えない。** このアプリはブラウザを操作していない。
Win32 API でネイティブウィンドウを探し、`ImageGrab` で画面そのものを物理ピクセルで
撮り、pyautogui で OS レベルのキーストロークを送る。Cloud Reader を対象にする場合も
Chrome の DOM ではなく画面を撮っているので、DOM を触るツールでは代替も観測もできない。

#### 対象は Cloud Reader を使う（PC アプリではなく）

本番の運用は数百冊の一括処理で、`cli.py batch` が `open → capture → validate →
trim → convert` を無人で回す。その `open` は **Kindle Cloud Reader (`kindle_cloud`
プロファイル)** を ASIN で開く実装なので、スモークも同じ経路で行う。

PC アプリの `kindle` プロファイルには**プログラムから本を開く手段が無い**。
人が本を開いておく必要があり、無人運用にも一括処理にも使えない。
PC アプリ固有の不具合を追うとき以外は選ばないこと。

#### 手順

1. **検出のみの確認**（無害。フォーカスを奪わない）

   ```
   python cli.py check --profile kindle_cloud
   ```

2. **1 冊を 3 ページだけ通す**（ここからデスクトップを占有する）

   ```
   python cli.py run --asin <ASIN> --title smoke --out <folder> \
       --format image_pdf --max-pages 3
   ```

   `--max-pages` を必ず付ける。付けないと最終ページまで走り続ける。
   `--format image_pdf` なら OCR エンジン不要で変換まで通せる。
   本を開くところから全自動なので、人が Kindle を操作する必要はない。

3. **一括経路まで見るとき**は 2 冊程度の `books.json` を作って `batch` を回す。

   ```
   python cli.py batch --books <books.json> --out <folder>
   ```

#### 確認すること

- `manifest.json` の `total_pages` が指定どおり、`stopped_reason` が `max_pages`
  （`timeout` なら本が開けていないか、ページが送れていない）
- 取得画像が実際に別ページになっている（同じ絵が並んでいないか目視）
- 生成された PDF が開けてページ数が合っている
- `run` の場合は `run_summary` イベントの各ステップ所要時間

#### 注意

- 先頭ページへの巻き戻しは Kindle の読書位置 (Whispersync) を動かす。
  本番の蔵書で試すときはそのつもりで
- キャプチャ中は前面ウィンドウとマウスを占有する

#### 結果の残し方

PR 説明に、実行したコマンドと `manifest.json` の要点
（`total_pages` / `stopped_reason` / `duration_seconds`）を貼る。
実機確認をしていない場合は、**その旨と未検証の範囲を明記する**。

キャプチャ経路に触れていない変更（CI 整備・依存の更新・トリミングやテキスト処理の
純ロジックなど）では不要。

### 6. push 前のコードレビュー

**push する前に、必ず独立したレビュー担当（Claude Code ならサブエージェント）に
コードレビューを依頼する。**

- レビュー対象は `origin/master...HEAD` の差分。2 回目以降の push は前回レビュー以降の差分でよい
- **依頼時に差分の内容そのものを渡す。** 小さい差分は本文をそのまま prompt に貼る。
  「コードを読んで調べてほしい」という探索タスクにすると時間がかかりすぎる
- 背景（対応している issue、判断の理由、**既に自分で検証済みの事実**）を明示して渡す。
  検証済みのものは「再確認不要」と書く
- 確認してほしい観点を具体的に列挙する。機械的な整形コミットなど不要な範囲は除外を明示する
- 指摘は「must fix / should fix / nits」に分けて報告させる
- 指摘を鵜呑みにせず、事実確認してから採否を決める。見送る場合は理由を PR 説明に書く
- 指摘を反映してから push する

### 7. PR

- 本文の冒頭に `Closes #<issue番号>` を書く
- 何を・なぜ変えたかを書く。特に**挙動を変えていない**ことの根拠
  （整形のみ、注釈のみ等）は明示する
- 実行した検証コマンドとその結果を書く
- レビューで受けた指摘と、その対応/見送りの理由を残す
- 既知の制約（テスト対象外の範囲など）を書く

## このリポジトリ固有の注意

- **Windows 専用アプリ**。`core/win32_utils.py` と `core/dpi.py` は `ctypes.windll` に依存し、
  `pyautogui` は import 時に X11 を要求する。加えて PDF のフォント埋め込みは
  Windows 標準の日本語フォント（`C:\Windows\Fonts`）を前提にしている。
  Linux では一部モジュールが収集不能・一部テストが失敗するため、
  **テストの動作確認は Windows で行う**。CI もテストは windows-latest で回している。
- **改行コード**は `.gitattributes` で「リポジトリ内 LF / 作業ツリー CRLF」に正規化している。
  WSL / Linux 側からファイルを書き換えると作業ツリーが LF になり、Windows 側の表示や
  diff が乱れることがある。書き換えスクリプトでは `newline=""` で読み書きして
  元の改行を保つこと（リポジトリ内は git が LF に正規化するのでコミット内容には影響しない）。
- **`core/config.json` はユーザーのローカル設定**。テストから読み書きしない
  （`tests/conftest.py` の `isolated_config` フィクスチャを使う）。
- 検証コマンドは `README.md` の「開発メモ > 検証コマンド」にまとめてある。
  CI もほぼ同じものを回す（CI にはこれに加えて改行コード正規化のチェックがある）。
