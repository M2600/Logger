# Logger (Core-Stream MVP)

`log.py` をクライアント、`daemon.py` を常駐APIとして分離した構成です。  
クライアントは入力とコンテキスト収集だけを行い、デーモンが保存・非同期分析・レポート生成を担当します。

## Setup

```bash
python -m pip install -r requirements.txt
```

Linux ではアクティブウィンドウ名取得に `xdotool` が必要です。  
macOS は `osascript`、Windows は `pygetwindow` を利用します。

## 1. Start daemon

```bash
python daemon.py --host 127.0.0.1 --port 8765 --model gemma2
```

主な保存先:

- events: `~/thought_stream.jsonl`
- classify cache: `~/.core_stream_classified.jsonl`
- analysis jobs: `~/.core_stream_analysis_jobs.jsonl`
- reports: `./reports/`

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

保存先を変更:

```bash
python log.py --shot-dir ~/my_shots "custom shot dir"
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

## HTTP endpoints (daemon)

- `GET /health`
- `GET /settings`
- `POST /settings/ai`
- `POST /events`
- `POST /analyze/backfill`
- `POST /reports/generate`
