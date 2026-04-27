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
- `--shot`: スクリーンショット撮影を有効化（デフォルト: 有効）
- `--no-shot`: スクリーンショット撮影を無効化
- `--type TYPE`: イベント種別（デフォルト: thought）
- `--daemon-url URL`: デーモン接続先（デフォルト: http://127.0.0.1:8765）
- `--timeout SEC`: タイムアウト時間（デフォルト: 0.8秒）
- `--debug`: デバッグログ出力（タイムスタンプ付き stderr に記録）

**スクリーンショット送信（新機能）:**
- ✅ クライアント側でスクリーンショットを撮影
- ✅ Base64 エンコードして JSON に含める
- ✅ daemon 側で受け取り、PNG として保存
- ✅ 保存先: `~/.logger/screenshots/` （--screenshot-dir で指定可能）
- ✅ イベント JSON に `meta.screenshot_path` として保存先を記録
- ✅ ローカル保存は廃止、daemon 側一元管理

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
- `retry-send`: 未送信イベントを再送信

**サブコマンドとグローバルオプションの順序:**
- グローバルオプション（`--daemon-url`, `--api-key` など）はサブコマンドの前後どちらでも指定可能
- 例: `python log.py --daemon-url http://localhost:8765 status` または `python log.py status --daemon-url http://localhost:8765`
- 内部的にサブコマンドを検出してから引数をパースするため、順序に依存しない

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

**自動リトライ機構:**
- LLM 分析失敗時、エラーが一時的（timeout/connection など）なら自動再キュー
- `is_retriable_error()` で エラーの種別を判定
- 指数バックオフで再試行: 5秒 → 10秒 → 20秒（最大3回）
- `retry_queue` と `retry_manager` スレッドで管理
- 永続的エラー（model not found など）は手動 backfill のみ

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
↓
失敗時 → is_retriable なら retry_queue へ
       → 指数バックオフで再処理
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

**プロンプト最適化:**
- GUI 入力（`source="gui"`）の場合、ウィンドウ/ページタイトル優先の指示を追加
- CLI 入力の場合は cwd ベースの分類
- これにより、複数プロジェクト作業時に入力元に応じた正確な分類が可能

**再分析設計:** LLM分析は1回で終わらない。新モデルやプロンプト改善に対応するため、`analysis_runs.jsonl` で分析履歴を管理する。

```
analysis_runs.jsonl
  event_id / model / result / created_at
```

---

## 6. タイムスタンプ管理

すべてのタイムスタンプはローカルタイムゾーン（JST +09:00 等）で記録される。

- **イベント作成時刻** (`event.created_at`): ローカル時刻 ISO 8601 形式
- **分類時刻** (`classified.classified_at`): ローカル時刻
- **ジョブ記録時刻** (`jobs.created_at`): ローカル時刻
- **レポートファイル名**: `20260425T101718+0900_report.md` 形式

→ ユーザーのシステム設定に自動追従。UTC 固定ではない。

---

## 7. アウトプット設計（CLIオプション）

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

## 8. AI処理のオン/オフ制御

デーモン側に設定APIを持たせ、AI処理を一時停止できる。

- 重いゲームや別開発中 → AI処理を停止、ログはJSONLに蓄積
- 後から一気にバッチ処理
- 再起動時は未分類イベントを自動再キューして処理を再開
- `/health` などで warning を返し、モデル未取得や分類停滞を可視化

---

## 9. 検索機能

推奨構成: **Hybrid Search**

| 方式 | 技術 | 用途 |
|---|---|---|
| キーワード検索 | JSONL全文grep / FTS5（将来移行時） | 正確な語句検索 |
| セマンティック検索 | Vector DB（ChromaDB等） | 意味合いでの類似検索 |

---

## 10. 拡張ロードマップ

| Phase | 内容 |
|---|---|
| Phase 1（MVP, 実装済み） | `log.py` + `daemon.py` + JSONL Event Store + 非同期LLM Worker |
| Phase 2 | 音声入力（Whisper統合）。スマホからのボイスメモを同じ思考ストリームへ |
| Phase 3 | 分散構成。ログ収集はRaspberry Pi、LLM推論はGPUマシン |
| Phase 4 | Vector DB導入による意味検索強化 |
| Phase 5 | セッション機能。`session_id` で同一作業セッションのログを紐付け・グルーピング |

---

## 11. このプロジェクトの位置づけ

| 比較対象 | 違い |
|---|---|
| Roam Research / Logseq | あちらは「手動ナレッジ管理」。Core-Streamは「自動思考ストリーム記録」 |
| Apache Kafka | 思想的に近い。ただし対象は人間の思考 |

---

## 12. 実装完了機能（Phase 1）

**Client (`log.py`):**
- ✅ CLI / GUI / stdin 入力モード
- ✅ 空引数フィルタリングと検証
- ✅ バックグラウンドスレッド送信（thread.join で完了待機）
- ✅ debug ログ出力
- ✅ サブコマンド: status / report / backfill / next / settings / task-complete
- ✅ ローカルタイムゾーン対応

**Daemon (`daemon.py`):**
- ✅ イベント受信・永続化 API
- ✅ 非同期 LLM 分析ワーカー
- ✅ classified.jsonl の整合性管理（失敗時の古い結果削除）
- ✅ 自動リトライ機構（指数バックオフ）
- ✅ /health エンドポイント（状態・warnings 表示）
- ✅ /analyze/backfill エンドポイント（手動再処理）
- ✅ /reports/generate エンドポイント（レポート生成）
- ✅ /tasks/mark-complete エンドポイント（手動タスク完了報告）
- ✅ タスク自動抽出・永続化（next コマンド時）
- ✅ タスク自動完了判定（新規イベント受信時の LLM 分析）

**LLM 分類:**
- ✅ GUI 入力時のウィンドウ優先プロンプト
- ✅ エラーの一時的/永続的判定
- ✅ 指数バックオフ再試行（最大3回）
- ✅ オプションのAPI キー認証（Bearer token）
- ✅ タスク完了判定プロンプト（新規イベント × 既存タスク）

---

## 12.5. Task Completion Tracking System

### 設計
- **目的:** ユーザーが記録したイベントから、タスク完了を自動/手動で追跡する
- **ストレージ:** `~/.logger/tasks.jsonl` に JSONL 形式で永続化
- **タスク状態:** open / completed
- **検出方式:** 
  1. 自動検出：新しいイベント受信時、LLM が既存タスクとの一致度を判定 → 確度高い場合は自動マーク
  2. 手動報告：CLI から直接タスク完了を指定

### 実装詳細

**Daemon（`daemon.py`）:**
- `Task` dataclass：タスク情報管理（id, task_text, extracted_at, status, completed_at, completed_event_id, note, completion_reason）
- `~/.logger/tasks.jsonl`：タスク永続化ストア（JSONL形式）
- `DaemonState.load_tasks()` / `save_task()` / `update_task()`：タスク I/O メソッド
- `DaemonState.auto_complete_tasks(event)`：イベント受信時の自動判定（背景スレッド実行）
- `POST /tasks/mark-complete`：手動完了報告エンドポイント（request: task_id, note）
- `/reports/generate` 拡張：`next` モード時に自動抽出したタスクを `tasks.jsonl` に保存（重複排除）

**Client（`log.py`）:**
- `task-complete <task-id> [--note NOTE]` サブコマンド
- `parse_task_complete_args()`：引数パース
- `mark_task_complete()`：実行ロジック
- `/tasks/mark-complete` エンドポイントを呼び出し

**Core Engine（`core_stream_engine.py`）:**
- `DEFAULT_TASKS_PATH` 定数追加（`~/.logger/tasks.jsonl`）

### ユースケース

1. **タスク抽出**
   - ユーザーが `python log.py next --period week` を実行
   - LLM が「未完了タスク」を抽出
   - Daemon が `tasks.jsonl` に自動保存（UUID生成、重複チェック）

2. **自動検出による完了判定**
   - ユーザーが作業ログを送信: `python log.py "Fixed database connection timeout"`
   - Daemon が新しいイベント受信
   - 背景スレッドで `auto_complete_tasks()` 実行
   - LLM が「database connection timeout」タスクとの一致度を判定
   - 確度が高い場合（>0.8）自動的に `status: completed` にマーク

3. **手動完了報告**
   - ユーザーが明示的に: `python log.py task-complete task-id-123 --note "Done in sprint 5"`
   - CLI から `/tasks/mark-complete` エンドポイントを呼び出し
   - タスク情報を更新（completed_at, completed_event_id, note, completion_reason: manual）

### タスクデータ形式

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "task_text": "Fix database connection timeout",
  "extracted_at": "2025-01-15T10:30:00+09:00",
  "status": "completed",
  "completed_at": "2025-01-15T14:22:30+09:00",
  "completed_event_id": "evt-abc123def",
  "note": "Fixed in v1.2.1",
  "completion_reason": "auto"
}
```

### 自動判定プロンプト

```
I have the following list of open tasks:
- Fix database connection timeout
- Implement user authentication
- Add rate limiting

A new event just occurred:
"Successfully resolved the database connection timeout issue by upgrading connection pool size"

Analyze this new event and determine which (if any) tasks have been completed.
Return JSON: {"completed_task_indices": [0], "confidence": 0.95, "notes": "..."}
Only include tasks where confidence > 0.8.
```

### 重複排除ロジック

`next` コマンドから複数回抽出されるタスクは、既存タスクと text-lower で比較して重複チェック。
同一タスク（status: open）が存在する場合は新規保存せず。

### 非ブロッキング実装

自動判定は背景スレッドで実行：
```python
if state.settings.ai_enabled:
    def _check_tasks():
        state.auto_complete_tasks(event)
    threading.Thread(target=_check_tasks, daemon=True).start()
```
メインの POST /events は即座に 200 OK を返却（タスク判定を待たない）。

---

## 13. API Key 認証（Network Deployment）

### 設計
- **デフォルト:** 認証なし（ローカル実行時は変更不要）
- **オプション:** daemon 起動時に `--api-key "secret"` で有効化
- **方式:** `Authorization: Bearer <key>` HTTP ヘッダー
- **対象:** すべてのエンドポイント（`GET /` と `GET /health` 除外）

### 実装
**Daemon（`daemon.py`）:**
- `AuthConfig` dataclass で API キー管理
- FastAPI middleware で Bearer token 検証
- 公開エンドポイントはスキップ（/、/health）
- 401: Authorization ヘッダー不足
- 403: 不正な API キー
- `/health` レスポンスに `auth_enabled` フィールド追加

**Client（`log.py`）:**
- `--api-key` CLI 引数
- `--config-file` で JSON 設定ファイル読み込み
- `LOGGER_API_KEY` 環境変数対応
- 優先度: CLI arg > config file > env var
- すべてのリクエストに Bearer token 自動追加

**Web UI（`index.html`）:**
- API キー入力フィールド（パスワード形式）
- 認証なしでも機能（オプション）
- fetch リクエストに Authorization ヘッダー自動追加

### 設定ファイル例
```json
{
  "api_key": "your-secret-key-here",
  "port": 8765
}
```

### 使用例
```bash
# Daemon をローカル実行（認証なし、従来通り）
python daemon.py

# Daemon をリモート公開（認証あり）
python daemon.py --api-key "my-secret-key"

# または config ファイル
python daemon.py --config-file ~/.logger/daemon.json

# Client で認証
python log.py --api-key "my-secret-key" "Hello from remote"

# または環境変数
export LOGGER_API_KEY="my-secret-key"
python log.py "Hello from remote"
```

---

## 14. 拡張設定ファイル対応（All Options）

### 概要
すべてのコマンドラインオプションを設定ファイルで指定可能にし、正確な優先順位で処理される。

### 優先順位（重要）
**CLI 引数 > 設定ファイル > デフォルト値**

#### デフォルト設定ファイルパス
明示的に `--config-file` を指定しなくても、以下のデフォルトパスが存在すれば自動的に読み込まれます：

**Daemon:**
- `~/.logger/daemon.json` （自動検出）
- または明示的に `--config-file ~/.logger/daemon.json`

**Client:**
- `~/.logger/client.json` （自動検出）
- または明示的に `--config-file ~/.logger/client.json`

実装方法：
- argparse のデフォルト値を `None` に設定
- parse_args() で解析後、`None` 値のみ config/default から取得
- CLI 引数が明示的に指定されていれば、値は `None` 以外となり優先される
- `--config-file` が指定されていなければ、デフォルトパスを確認して存在すれば使用

### Daemon 設定ファイル (`daemon.config.example.json`)
```json
{
  "host": "127.0.0.1",
  "port": 8765,
  "model": "gemma2",
  "ollama_url": "http://127.0.0.1:11434/api/generate",
  "timeout": 120.0,
  "ai_enabled": true,
  "api_key": "your-secret-key-here",
  "events_path": "~/.logger/events.jsonl",
  "classified_path": "~/.logger/classified.jsonl",
  "jobs_path": "~/.logger/jobs.jsonl",
  "reports_dir": "~/.logger/reports",
  "screenshot_dir": "~/.logger/screenshots"
}
```

**対応オプション:** host, port, events_path, classified_path, jobs_path, reports_dir, screenshot_dir, model, ollama_url, timeout, ai_enabled, api_key (12項目)

### Client 設定ファイル (`client.config.example.json`)
```json
{
  "daemon_url": "http://localhost:8765",
  "api_key": "your-secret-key-here",
  "shot_dir": "~/thought_stream_shots",
  "type": "thought",
  "timeout": 0.8,
  "debug": false,
  "gui": false,
  "stdin": false
}
```

**対応オプション:** daemon_url, api_key, timeout (すべてのコマンド共通)、各コマンド固有のオプション

### 実装状況
**Daemon (`daemon.py`):**
- ✅ argparse デフォルト値を `None` に設定
- ✅ parse_args() で `None` 値のみ config/default から取得
- ✅ 優先順位: CLI > config > default で正確に実装
- ✅ 11 オプション完全対応

**Client (`log.py`):**
- ✅ すべての parse_*_args() 関数で統一された実装
- ✅ argparse デフォルト値を `None` に設定
- ✅ 優先順位が正確に機能
- ✅ parse_log_args (8オプション)
- ✅ parse_report_args (3オプション)
- ✅ parse_settings_args (3オプション)
- ✅ parse_status_args (3オプション)
- ✅ parse_backfill_args (3オプション)

### 使用例
```bash
# 設定ファイルからのみ読み込み
python daemon.py --config-file ~/.logger/daemon.json

# CLI 引数で port を上書き（明示的に指定）
python daemon.py --config-file ~/.logger/daemon.json --port 9000
# → port=9000 (CLI優先), 他はconfigから

# Client で設定ファイル使用
python log.py --config-file ~/.logger/client.json "test message"

# CLI 引数で API キーを上書き（明示的に指定）
python log.py --config-file ~/.logger/client.json --api-key "new-key" "test"
# → api_key=new-key (CLI優先), 他はconfigから

# 複数の上書き
python daemon.py --config-file ~/.logger/daemon.json --port 9000 --model llama2 --ai-enabled
```

### テスト結果
- ✅ デフォルト値が正しく使用される
- ✅ 設定ファイルが正しく適用される
- ✅ CLI 引数が設定ファイルを上書きする
- ✅ 複数値が同時に指定された場合、混合で正しく機能

---

## 15. 実装完了機能（Phase 1 + Auth + Extended Config）

**認証:**
- ✅ Daemon: Bearer token validation middleware
- ✅ Daemon: CLI 引数 (--api-key, --config-file)
- ✅ Daemon: /health endpoint auth_enabled field
- ✅ Client: API key loading (CLI arg, config, env var)
- ✅ Client: Bearer token header injection
- ✅ Web UI: Optional API key input field
- ✅ Backward compatible (auth disabled by default)

**スクリーンショット送信・保存:**
- ✅ Client: Base64 エンコード機能 (capture_screenshot_base64)
- ✅ Daemon: screenshot_data フィールド対応
- ✅ Daemon: Base64 デコード・PNG 保存機能
- ✅ Daemon: --screenshot-dir オプション（デフォルト: ~/.logger/screenshots）
- ✅ Daemon: 設定ファイル対応
- ✅ Client: thread.join(timeout=30) で送信完了待機
- ✅ Event meta に screenshot_path を記録

**デュアルモード処理（Fire-and-Forget + Default）:**
- ✅ `--fire-and-forget` フラグで即座にシェル解放（0.14秒）
- ✅ バックグラウンド subprocess で独立実行
- ✅ `~/.logger/pending_events.jsonl` で送信内容を永続化
- ✅ `~/.logger/last_event.log` で実行結果を記録
- ✅ `python log.py retry-send` で未送信イベントを再送信
- ✅ デフォルトモード（`python log.py "msg"`）で結果を待って表示（互換性維持）

---

## まとめ

```
Thought Logger
+
Event Stream
+
AI Structuring
+
Secure Network Deployment (optional API key)
```

**入力はfrictionless、整理はAI自動化、展開は安全。**
思考を止めない、ただそれだけのために設計されたシステム。

---

## 16. 将来の拡張案（Phase 2+）

### Vision-Language Model Agent 設計

**背景:**
スクリーンショットは現在 Base64 エンコードで保存されているが、未活用。VL(Vision-Language)モデルで画像を解析すると、テキスト情報が不足している場合に強力になる可能性がある。

**ハードウェア制約（RTX3060 12GB 想定）:**
- テキストモデル (gemma2:7b, qwen2.5:7b 等): 常時常駐可能 (~7-8GB)
- VLモデル (llava:13b 等): 常時常駐はメモリ逼迫 (~11-12GB)
- 両方の同時常駐は不可能

**提案設計: エージェント + ツール呼び出し**

1. **通常フロー:**
   ```
   Event (body + context) → テキストモデル分類
   ```

2. **フォールバックフロー (条件付き):**
   ```
   テキスト分類時に以下を判定:
   - body が曖昧 (例: "これどうすればいいんだ")
   - かつ context が薄い (git_repo = unknown, win = unknown)
   
   → LLM が "context_insufficient" ツールを呼び出し
   → スクリーンショット + body → VLモデル解析
   → 補足コンテキストを取得 → テキストモデルが再分類
   ```

3. **ツール定義:**
   ```json
   {
     "type": "function",
     "function": {
       "name": "analyze_screenshot",
       "description": "画像を解析してプロジェクト・タスクを判定する（body が曖昧で context が不足している場合のみ呼び出す）",
       "parameters": {
         "type": "object",
         "properties": {
           "reason": {
             "type": "string",
             "description": "なぜ画像解析が必要なのか"
           }
         },
         "required": ["reason"]
       }
     }
   }
   ```

**実装手順:**
- Phase 2a: Ollama のツール呼び出し機能を実装
- Phase 2b: VLモデル統合 (初回ロード・キャッシュ戦略含む)
- Phase 2c: スワップ頻度の測定・最適化

**期待される効果:**
- 画像解析は本当に必要な時だけ → GPU メモリ・レイテンシの節約
- LLM 自身が判断 → ルールベースより柔軟
- モデルスワップは 1 日数十回程度 → ハード寿命への影響無視できる

**懸念事項:**
- スワップ頻度が予想より高い場合 → VL モデル統一に切り替える検討
- Ollama のツール呼び出し API の仕様確認が必要
- VL モデルの精度検証 (実装時)

