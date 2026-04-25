# 🧠 Core-Stream — 総合設計ドキュメント

> **Frictionless Thought Logger for ADHD**
> 思考速度を阻害しないことを最優先に設計された、思考ログ・自動構造化システム。

---

## 1. 設計思想（Philosophy）

既存のメモアプリ・タスク管理ツールが強要する「綺麗に書くこと」を完全に放棄する。

### Frictionless Input
思考速度はタイピング速度を上回る。以下を排除する。

- GUI起動
- プロジェクト選択
- タグ入力
- ノート構造整理

入力は **1コマンドのみ**。

```bash
l "idea about scheduler"
```

### Context-Aware Logging
ユーザーは文脈を入力しなくてよい。システムが自動取得する。

- `cwd`（カレントディレクトリ）
- `git repo / branch / commit`
- アクティブウィンドウ名
- ブラウザのタブ名
- `timestamp`

### Fire-and-Forget
入力後は**即終了**。AIの応答を待たない。

```
入力 → POST /events → 200 OK → CLI終了
```

タイムアウト目標: **< 200ms**

### Chaos → Order
「入力」と「整理」をアーキテクチャレベルで分離する。

```
Human  →  カオスな断片を投げるだけ
LLM    →  分類・要約・構造化を自動実行
```

---

## 2. システムアーキテクチャ

採用モデル: **Client-Daemon Architecture**（Ollamaライクな構成）

```
CLI (l)
   ↓  POST /events
Daemon API (FastAPI)
   ↓  即時保存
JSONL Event Store
   ↓  非同期キュー
LLM Worker (Ollama)
```

---

### 2.1 CLI Client（`log.py`）

**役割:** 入力受け付けとコンテキスト収集に特化した超軽量プログラム。

| モード | 例 |
|---|---|
| 非対話モード | `python log.py "fix docker bug"` |
| 複数引数連結 | `python log.py this is a test` → "this is a test" として記録 |
| 対話モード | `python log.py`（簡易入力UI） |
| stdinモード | `git diff \| python log.py --stdin` |

**引数の扱い（仕様）:**
- 複数の引数として与えられた場合、すべてスペースで連結して1つのログとして記録する
- 例: `python log.py hello world test` → ログ本体は "hello world test"
- 理由: ユーザーが自由にスペースを含むテキストを記録できるようにするため

stdinモードにより、開発ログ・システムログ・コマンドログをそのまま記録可能。
クライアントの責務は「入力・コンテキスト収集・API送信」に限定する。

**処理フロー（全て非同期バックグラウンド）:**
```
ユーザー入力（GUI / CLI / stdin）
  ↓
コンテキスト・メタデータ収集開始
  ↓
スクリーンショット撮影開始
  ↓
ユーザーへ即座に制御を返却（入力完了）
  ↓ [バックグラウンドスレッド内]
HTTP POST /events（デーモンへ送信）
  ↓
警告メッセージ表示（デーモンからの通知あれば）
```

タイムアウト目標: **< 100ms**（ユーザーへの制御復帰まで）


**CLI オプション:**
- `--gui`: GUI 入力を強制（メッセージ引数を無視）
- `--stdin`: stdin から入力を読み取る
- `--no-shot`: スクリーンショット撮影を無効化
- `--shot-dir DIR`: スクリーンショット保存先
- `--type TYPE`: イベント種別（デフォルト: thought）
- `--daemon-url URL`: デーモン接続先（デフォルト: http://127.0.0.1:8765）
- `--timeout SEC`: タイムアウト時間（デフォルト: 0.8秒）
- `--debug`: デバッグログ出力（タイムスタンプ付き stderr に記録）
- `--async`: イベント送信をバックグラウンドスレッドで実行（デフォルト: 有効）
  - スレッドは非デーモンモード（daemon=False）で実行
  - thread.join(timeout=2.0) で送信完了を待つ
  - → ユーザーへの制御復帰前に HTTP POST が完了することを保証

**入力検証:**
- 複数引数の空文字は自動フィルタ（`python log.py "" test ""`→ "test"）
- 全引数が空文字のみの場合は GUI 起動に fallback
- GUI/stdin/shortcut で空入力の場合はリクエスト送信しない
- ユーザーのタイプミスやムダなリクエスト送信を防止

**サブコマンド:**
- `report [--period PERIOD] [--format FORMAT]`: レポート生成要求
  - エラー時: daemon から返却された warning/hint を stderr に表示
  - hint 表示時: `python log.py backfill` 実行の提案を追加表示
- `next [--llm SETTING] [--format FORMAT]`: 未完了タスク表示
- `status [--format FORMAT]`: daemon 健康状態 / 分析処理状態を表示
  - Queue size / Analysis state / Last error などを可視化
- `backfill`: 分類失敗・未分類イベントを再キューして再処理
  - daemon の `/analyze/backfill` を呼び出し
  - 手動トリガー版（自動は再起動時に実行）
- `settings --ai {on|off}`: AI 処理のオン/オフ制御
  - AI無効時: イベントは記録されるが分類は実行されない

---

### 2.2 Daemon / APIサーバー（FastAPI, `daemon.py`）

**役割:** バックグラウンド常駐。データの確実な保存と重い処理のスケジューリングを担うハブ。

**分類状態の管理:**
- `events.jsonl`: 全イベントの生データ（source of truth）
- `analysis_jobs.jsonl`: 各イベントの分析ジョブ履歴（status: pending/processing/done/failed）
- `classified.jsonl`: 分類済みイベント（キャッシュ）
  - **重要:** `classified.jsonl` は「最新ステータスが done のイベント」のみを保持
  - 失敗・再試行時は古い分類結果は削除
  - 起動時と backfill 時に jobs から再構築

処理フロー:

```
POST /events 受信
↓
JSONL へ即時追記
↓
200 OK 返却（クライアントを待たせない）
↓
analysis_job を非同期キューへ積む
↓
自身のペースでLLMワーカーへ渡す
```

公開API（現行）:

- `GET /health`
- `GET /settings`
- `POST /settings/ai`
- `POST /events`
- `POST /analyze/backfill`
- `POST /reports/generate`

---

### 2.3 Event Store（JSONL）

ログではなく **Event Stream** として保存する。1イベント1行のJSONL形式。

**レコード構造**

| フィールド | 説明 |
|---|---|
| `id` | ユニークID |
| `type` | イベント種別 |
| `body` | 生テキスト |
| `context` | 自動収集コンテキスト（JSON object） |
| `created_at` | タイムスタンプ |

**event type 一覧**

| type | 説明 |
|---|---|
| `thought` | 通常の思考ログ |
| `stdin` | パイプ入力（git diff等） |
| `git` | Git hookからの自動記録 |
| `voice` | 音声入力（将来対応） |
| `browser` | ブラウザタブ情報 |
| `system` | システムイベント |

---

### 2.4 LLM Worker（Ollama連携）

**役割:** 生ログとコンテキストを読み解き、分類・要約・TODO抽出を行う。

**別プロセス分離の理由:**

- APIを軽量に保つ
- GPUマシンへ分離可能（Raspberry Pi + メインPC構成）
- 複数ワーカーの並列化が可能

**analysis_jobs（JSONL）**

| カラム | 説明 |
|---|---|
| `id` | ジョブID |
| `event_id` | 対象イベント |
| `status` | `pending / processing / done / failed` |
| `priority` | 優先度 |
| `model` | 使用モデル名 |
| `created_at` | 作成日時 |

**priority queue（優先度設定例）**

| type | priority |
|---|---|
| `thought` | high |
| `voice` | medium |
| `stdin` log | low |

---

## 3. コンテキスト自動収集

| カテゴリ | 収集項目 |
|---|---|
| Environment | `cwd`, `hostname`, `timestamp` |
| Git | `repo`, `branch`, `commit` |
| Shell | `last_command` |

---

## 4. Git Hook Integration

Git の `post-commit` hook を利用し、コミット情報を自動ログとして記録する。

```
commit
↓
commit hash / branch / message 取得
↓
python log.py --type git へ自動送信
```

これにより **「思考 → コード → commit」の因果関係** がログとして保存される。

---

## 5. LLM分析内容

| 処理 | 内容 |
|---|---|
| classification | `bug / idea / task / note` への分類 |
| TODO extraction | ログから「疑問」「バグ」「やり残し」を抽出 |
| summarization | 指定期間の要約生成 |
| thought linking | 思考とcommitとbugfixの関連付け |

**再分析設計:** LLM分析は1回で終わらない。新モデルやプロンプト改善に対応するため、`analysis_runs.jsonl` で分析履歴を管理する。

```
analysis_runs.jsonl
  event_id / model / result / created_at
```

---

## 6. アウトプット設計（CLIオプション）

インプット側と同様、アウトプットも **CLIで完結** させる。

### `python log.py report` — 日報・報告用

- **対象:** 他者向け（上司・チーム等）
- **内容:** 完了した事実を時系列でまとめたMarkdown
- **フォーマット:** プロジェクトごとに整理された綺麗なレポート

### `python log.py next` — 自分向け確認用

- **対象:** 自分自身
- **内容:** 未完了タスク・疑問点・やり残しのチェックリスト
- **フォーマット:** チェックボックス形式のTODOリスト

**現行実装:**
- クライアントは `/reports/generate` を呼び、返却結果を表示する
- レポート生成ロジック（静的集計 + 必要時のLLM整形）はデーモン側に集約
- 出力フォーマット（md/json/both）はクライアント側で指定

---

## 7. AI処理のオン/オフ制御

デーモン側に設定APIを持たせ、AI処理を一時停止できる。

- 重いゲームや別開発中 → AI処理を停止、ログはJSONLに蓄積
- 後から一気にバッチ処理
- 再起動時は未分類イベントを自動再キューして処理を再開
- `/health` などで warning を返し、モデル未取得や分類停滞を可視化

---

## 8. 検索機能

推奨構成: **Hybrid Search**

| 方式 | 技術 | 用途 |
|---|---|---|
| キーワード検索 | JSONL全文grep / FTS5（将来移行時） | 正確な語句検索 |
| セマンティック検索 | Vector DB（ChromaDB等） | 意味合いでの類似検索 |

---

## 9. 拡張ロードマップ

| Phase | 内容 |
|---|---|
| Phase 1（MVP, 実装済み） | `log.py` + `daemon.py` + JSONL Event Store + 非同期LLM Worker |
| Phase 2 | 音声入力（Whisper統合）。スマホからのボイスメモを同じ思考ストリームへ |
| Phase 3 | 分散構成。ログ収集はRaspberry Pi、LLM推論はGPUマシン |
| Phase 4 | Vector DB導入による意味検索強化 |
| Phase 5 | セッション機能。`session_id` で同一作業セッションのログを紐付け・グルーピング |

---

## 10. このプロジェクトの位置づけ

| 比較対象 | 違い |
|---|---|
| Roam Research / Logseq | あちらは「手動ナレッジ管理」。Core-Streamは「自動思考ストリーム記録」 |
| Apache Kafka | 思想的に近い。ただし対象は人間の思考 |

---

## まとめ

```
Thought Logger
+
Event Stream
+
AI Structuring
```

**入力はfrictionless、整理はAI自動化。**
思考を止めない、ただそれだけのために設計されたシステム。
