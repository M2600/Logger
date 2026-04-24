# Logger
Daily logger for ADHD

## Setup

```bash
python -m pip install -r requirements.txt
```

Linux ではアクティブウィンドウ名取得に `xdotool` が必要です。  
macOS は `osascript`、Windows は `pygetwindow` を利用します。

## Usage

```bash
# CLI入力
python log.py "今の思考"

# GUI入力（引数なし）
python log.py
```

既定でログ保存時に毎回スクリーンショットを撮影し、`~/thought_stream_shots` に保存します。  
スクリーンショットを無効化する場合:

```bash
python log.py --no-shot "スクショ不要のログ"
```

保存先を変更する場合:

```bash
python log.py --shot-dir ~/my_shots "保存先指定"
```

## Analyze logs (split classify/report)

`analyze.py` は「分類」と「レポート生成」を分離しています。

1. `classify`: 新規ログだけを LLM で分類し、キャッシュ (`~/.core_stream_classified.jsonl`) に追記  
2. `report`: 分類済みキャッシュからレポート/Todoを生成（静的処理中心、必要時のみLLM整形）

分類キーは `ctx.cwd` を優先し、欠損時に `ctx.page_title` / `ctx.win` / `meta.project_hint` を使います。  
事前に Ollama を起動し、モデルを取得してください（例: `ollama run gemma2`）。

```bash
# 1回だけ分類（重複ログは自動スキップ）
python analyze.py classify --model gemma2

# 定期分類（60秒ごと）
python analyze.py classify --interval 60 --model gemma2

# 今日の進捗レポート（分類キャッシュから生成）
python analyze.py report --period today --mode report --format both

# Todo抽出（静的のみ: LLM未使用）
python analyze.py report --period week --mode todo --format json --llm never

# 件数が多い時だけLLM整形（auto）
python analyze.py report --period week --mode report --llm auto --llm-threshold 60
```

`report` の出力はコンソール表示され、同時に `reports/` 配下へ日付付き保存されます。  
`--stdout` と `--format json` を併用すると、API連携向けに機械可読JSONをそのままパイプしやすくなります。

## Notes

- アプリの GUI 入力ウィンドウは撮影前に退避してから保存します。
- Wayland 環境などではウィンドウ情報取得や撮影が制限される場合があり、その場合もログ保存は継続します。
