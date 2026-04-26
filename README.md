# Logger (Core-Stream MVP)

`log.py` をクライアント、`daemon.py` を常駐APIとして分離した構成です。  
クライアントは入力とコンテキスト収集だけを行い、デーモンが保存・非同期分析・レポート生成を担当します。

## Setup

```bash
python -m pip install -r requirements.txt
```

Linux ではアクティブウィンドウ名取得に `xdotool` が必要です。  
macOS は `osascript`、Windows は `pygetwindow` を利用します。

### Ollama Setup

This logger uses **Ollama** for local LLM-based event classification and analysis. Ollama must be running before the daemon can perform AI operations.

**Install Ollama:**
- Download and install from https://ollama.ai

**Start Ollama (in another terminal):**
```bash
ollama serve
```

**Pull a model (e.g., gemma2):**
```bash
ollama pull gemma2
```

Available models: `gemma2`, `mistral`, `neural-chat`, `llama2`, etc.  
Daemon auto-downloads the specified model on first run.

## Configuration

See [CONFIG.md](CONFIG.md) for detailed configuration guide.

### Quick Start

**Local (no auth):**
```bash
python daemon.py
python log.py "my thought"
```

**Remote (with API key):**
```bash
python daemon.py --api-key "my-secret-key"
python log.py --api-key "my-secret-key" "my thought"
```

Or use environment variable:
```bash
export LOGGER_API_KEY="my-secret-key"
python log.py "my thought"
```

## 1. Start daemon

```bash
python daemon.py --host 127.0.0.1 --port 8765 --model gemma2
```

主な保存先（すべて `~/.logger/` 配下に統一）:

- events: `~/.logger/events.jsonl`
- classify cache: `~/.logger/classified.jsonl`
- analysis jobs: `~/.logger/jobs.jsonl`
- reports: `~/.logger/reports/`
- screenshots: `~/.logger/screenshots/`

## 2. Send logs from client

```bash
# CLI入力
python log.py "fix docker bug"

# GUI入力（引数なし）
python log.py

# stdin入力
git diff | python log.py --stdin --type stdin
```

既定でスクリーンショットを撮影します。無効化:

```bash
python log.py --no-shot "no screenshot"
```

スクリーンショットはすべてdaemon側の `~/.logger/screenshots/` に保存されます。

### Fire-and-Forget Mode (即座にシェル解放)

通常モードはイベント送信完了を待ちますが、`--fire-and-forget` フラグで即座にシェルを解放し、バックグラウンドで処理を続行します：

```bash
# 0.14秒でシェルプロンプト返却、処理はバックグラウンド実行
python log.py --fire-and-forget "quick message"

# 結果は ~/.logger/last_event.log に自動記録
cat ~/.logger/last_event.log
```

**利点:**
- タイピング中断なし（シェル即解放）
- 大きなリクエスト（画像など）も確実に送信（最大30秒タイムアウト）
- ネットワーク失敗時も `~/.logger/pending_events.jsonl` に保存

### Pending Events & Retry

送信失敗時は `~/.logger/pending_events.jsonl` に保存され、後で再送信できます：

```bash
# 未送信イベントをすべて再送信
python log.py retry-send
```

## 3. Generate outputs

```bash
# 日報
python log.py report --period today --format both

# 次アクション（todo）
python log.py next --period week --llm never --format md
```

## 4. Daemon settings

```bash
# AI処理ON/OFF
python log.py settings --ai on
python log.py settings --ai off
```

## 5. Subcommand & Global Options

Subcommands (`status`, `report`, `next`, `settings`, `backfill`, `retry-send`) can appear anywhere in the command line. Global options (`--daemon-url`, `--api-key`, etc.) can come before or after the subcommand:

```bash
# Both forms are equivalent
python log.py --daemon-url http://localhost:8765 status
python log.py status --daemon-url http://localhost:8765

# Mixed with other options
python log.py --daemon-url http://localhost:8765 report --period week
python log.py report --period week --daemon-url http://localhost:8765
```

## 6. HTTP endpoints (daemon)

- `GET /health`
- `GET /settings`
- `POST /settings/ai`
- `POST /events`
- `POST /analyze/backfill`
- `POST /reports/generate`

## 7. Configuration Files

Example config files for reference:
- `daemon.config.example.json` - Daemon configuration template
- `client.config.example.json` - Client configuration template

For setup instructions, see [CONFIG.md](CONFIG.md).
