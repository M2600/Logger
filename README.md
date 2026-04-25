# Logger (Core-Stream MVP)

`log.py` をクライアント、`daemon.py` を常駐APIとして分離した構成です。  
クライアントは入力とコンテキスト収集だけを行い、デーモンが保存・非同期分析・レポート生成を担当します。

## Setup

```bash
python -m pip install -r requirements.txt
```

Linux ではアクティブウィンドウ名取得に `xdotool` が必要です。  
macOS は `osascript`、Windows は `pygetwindow` を利用します。

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

## HTTP endpoints (daemon)

- `GET /health`
- `GET /settings`
- `POST /settings/ai`
- `POST /events`
- `POST /analyze/backfill`
- `POST /reports/generate`

## Configuration Files

Example config files for reference:
- `daemon.config.example.json` - Daemon configuration template
- `client.config.example.json` - Client configuration template

For setup instructions, see [CONFIG.md](CONFIG.md).
