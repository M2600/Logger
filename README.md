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

## Notes

- アプリの GUI 入力ウィンドウは撮影前に退避してから保存します。
- Wayland 環境などではウィンドウ情報取得や撮影が制限される場合があり、その場合もログ保存は継続します。
