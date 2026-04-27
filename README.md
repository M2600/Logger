# Logger (Core-Stream MVP)

`log.py` をクライアント、`daemon.py` を常駐APIとして分離した構成です。  
クライアントは入力とコンテキスト収集だけを行い、デーモンが保存・非同期分析・レポート生成を担当します。

**📚 Documentation (integrated):**
- This README - setup, usage, task completion workflow
- [CONFIG.md](CONFIG.md) - configuration reference
- [PLAN.md](PLAN.md) - architecture and implementation details

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

## 3.5. Task Completion Tracking

Logger automatically tracks task completion based on your logged events. Tasks are extracted from your `next` command output and can be tracked until completion.

### Workflow

**1. View Current Tasks**
```bash
python log.py next --period week
```
This displays all uncompleted tasks extracted from your classified events. Tasks are automatically saved to `~/.logger/tasks.jsonl` with unique IDs.

**2. Auto-Detection (Recommended)**
Simply log your work as usual:
```bash
python log.py "Fixed the database connection timeout"
```
The daemon automatically:
- Analyzes the new event in the background
- Compares it against all open tasks
- Marks matching tasks as complete (high confidence only)
- Runs in background thread (non-blocking)

**3. Manual Completion**
Explicitly mark a task as complete:
```bash
# Mark specific task
python log.py done <task-id> --note "Completed in sprint 5"

# Example with task ID from 'next' output
python log.py done abc123def456 --note "Implemented auth system"

# With authentication
python log.py done abc123 --api-key secret --daemon-url http://remote:8765
```

### Task Storage (`~/.logger/tasks.jsonl`)

The system automatically manages tasks:
- **Extraction**: Tasks extracted from `next` command output
- **Storage**: JSONL format with one task per line
- **Fields**: id (UUID), task_text, extracted_at, status, completed_at, completed_event_id, note, completion_reason
- **Status tracking**: open → completed
- **Deduplication**: Prevents duplicate task extraction
- **Persistence**: Data survives daemon restart
- **Completion filtering**: `done` 済みタスクは `next` と `report` の未完了アクション表示から除外

### Task Completion Methods

| Method | Command | Auto? | Use Case |
|--------|---------|-------|----------|
| Manual CLI | `done <id>` (alias: `task-complete`) | No | Explicit completion marking |
| Auto-Detection | Log event normally | Yes | Seamless workflow integration |
| API Endpoint | POST /tasks/mark-complete | No | Programmatic integration |

### Example Workflow

```bash
# 1. Start daemon
python daemon.py

# 2. View pending work
$ python log.py next --period today
- [ ] Fix database connection (id: 550e8400-e29b-41d4-a716-446655440000)
- [ ] Implement user auth (id: abc123-def456-ghi789-jkl012)
- [ ] Add rate limiting (id: xyz789-aaa111-bbb222-ccc333)

# 3. Do the work and log it
$ python log.py "Successfully fixed the database connection timeout issue"
✓ Event logged (ID: evt-123)
# Behind the scenes: Daemon analyzes event and detects "Fix database connection" is complete

# 4. View updated task list
$ python log.py next --period today
- [x] Fix database connection (✓ completed)
- [ ] Implement user auth (id: abc123-def456-ghi789-jkl012)
- [ ] Add rate limiting (id: xyz789-aaa111-bbb222-ccc333)

# Or manually mark:
$ python log.py done task-id-2 --note "Just finished auth implementation"
✓ Task marked as complete: Implement user auth
```

## 4. Daemon settings

```bash
# AI処理ON/OFF
python log.py settings --ai on
python log.py settings --ai off
```

## 5. Subcommand & Global Options

Subcommands (`status`, `report`, `next`, `settings`, `backfill`, `retry-send`, `done`, `task-complete`) can appear anywhere in the command line. Global options (`--daemon-url`, `--api-key`, etc.) can come before or after the subcommand:

```bash
# Both forms are equivalent
python log.py --daemon-url http://localhost:8765 status
python log.py status --daemon-url http://localhost:8765

# Mixed with other options
python log.py --daemon-url http://localhost:8765 report --period week
python log.py report --period week --daemon-url http://localhost:8765

# Task completion
python log.py done task-id-123 --note "Done!"
```

## 6. HTTP endpoints (daemon)

- `GET /health`
- `GET /settings`
- `POST /settings/ai`
- `POST /events`
- `POST /analyze/backfill`
- `POST /reports/generate`
- `POST /tasks/mark-complete` - Mark a task as complete

## 7. Configuration Files

Example config files for reference:
- `daemon.config.example.json` - Daemon configuration template
- `client.config.example.json` - Client configuration template

For setup instructions, see [CONFIG.md](CONFIG.md).

## 8. FAQ - Task Completion System

### Q: How do I see my task IDs?
**A:** Run `python log.py next` and look at the output. Each open task shows `(... id: <task-id>)`. Use that ID with `python log.py done <task-id>` (or `task-complete`).

Example:
```bash
python log.py next
# - [ ] Verify task completion status (P1 / ...) (id: 550e8400-e29b-41d4-a716-446655440000)
python log.py done 550e8400-e29b-41d4-a716-446655440000 --note "done"
```

### Q: Will auto-detection mark tasks incorrectly?
**A:** Auto-detection only marks tasks complete when it has HIGH confidence (>0.8) that the event resolves the task. This minimizes false positives. If you want guaranteed accuracy, use manual completion.

### Q: Can I undo a completed task?
**A:** Not yet - once a task is marked complete, it cannot be unmarked. This is by design to maintain a clean audit trail. Future enhancement: Add task state transitions (open → in-progress → completed → reopened).

### Q: Where are tasks stored?
**A:** Tasks are stored in `~/.logger/tasks.jsonl` in JSONL format (one JSON object per line). You can inspect this file directly or query via the daemon API.

### Q: Can I export my task history?
**A:** Yes! Simply copy `~/.logger/tasks.jsonl` or use standard JSONL tools:
```bash
cat ~/.logger/tasks.jsonl | jq '.[] | select(.status == "completed")'
```

### Q: Does auto-detection work when AI is disabled?
**A:** No. Auto-detection requires the LLM to analyze task matching. If you disable AI (`python log.py settings --ai off`), use manual completion instead.

### Q: Can I bulk-import tasks?
**A:** Yes. Add entries directly to `~/.logger/tasks.jsonl`:
```bash
echo '{"id":"task-1","task_text":"Example task","extracted_at":"2025-01-01T10:00:00+00:00","status":"open","completed_at":null,"completed_event_id":null,"note":"","completion_reason":"manual"}' >> ~/.logger/tasks.jsonl
```

### Q: How often does auto-detection run?
**A:** Auto-detection runs in a background thread whenever a new event is received (after you run `python log.py`). It completes within seconds.

### Q: Can I configure auto-detection sensitivity?
**A:** Currently, the threshold is hard-coded to 0.8 confidence. Future enhancement: Make this configurable via `--auto-complete-threshold` flag.

## 9. Documentation Map (統合版)

このREADMEに主要な使い方（セットアップ、CLI、タスク完了フロー、FAQ）を統合しています。

- **運用手順（このファイル）**: 起動、送信、`next`、`done`、FAQ
- **設定リファレンス**: [CONFIG.md](CONFIG.md)
- **設計/実装詳細**: [PLAN.md](PLAN.md)

用途別の最短ルート:

1. すぐ使いたい: 「Quick Start」→「3.5 Task Completion Tracking」
2. タスクIDを確認したい: `python log.py next`
3. 設定を変更したい: [CONFIG.md](CONFIG.md)
4. 内部実装を確認したい: [PLAN.md](PLAN.md)
