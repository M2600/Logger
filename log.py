#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import pyperclip  # type: ignore
except Exception:
    pyperclip = None


DEFAULT_SHOT_DIR = Path.home() / "thought_stream_shots"
DEFAULT_DAEMON_URL = os.environ.get("CORE_STREAM_DAEMON_URL", "http://127.0.0.1:8765")
BROWSER_SUFFIXES = [
    "Google Chrome",
    "Chrome",
    "Chromium",
    "Microsoft Edge",
    "Edge",
    "Brave Browser",
    "Brave",
    "Safari",
    "Firefox",
    "Opera",
    "Vivaldi",
]
BROWSER_HINTS = [item.lower() for item in BROWSER_SUFFIXES]


def _run_command(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except Exception:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    output = (result.stdout or "").strip()
    return output or "unknown"


def get_active_window_title() -> str:
    os_name = platform.system()
    if os_name == "Windows":
        try:
            import pygetwindow as gw  # type: ignore

            active = gw.getActiveWindow()
            if active and active.title:
                return active.title.strip() or "unknown"
        except Exception:
            return "unknown"
        return "unknown"
    if os_name == "Darwin":
        script = (
            'tell application "System Events" to tell '
            "(first process whose frontmost is true) to "
            "value of attribute \"AXTitle\" of front window"
        )
        return _run_command(["osascript", "-e", script])
    if os_name == "Linux":
        return _run_command(["xdotool", "getactivewindow", "getwindowname"])
    return "unknown"


def extract_page_title(window_title: str) -> str:
    title = (window_title or "").strip()
    if not title or title == "unknown":
        return "unknown"
    for suffix in BROWSER_SUFFIXES:
        pattern = rf"\s*[-–—|:]\s*{re.escape(suffix)}\s*$"
        title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()
    return title or "unknown"


def is_browser_window(window_title: str) -> bool:
    lowered = (window_title or "").lower()
    return any(hint in lowered for hint in BROWSER_HINTS)


def get_clipboard_text() -> str:
    if pyperclip is None:
        return ""
    try:
        text = pyperclip.paste()
    except Exception:
        return ""
    if text is None:
        return ""
    return str(text)


def infer_project_hint(cwd: str, window_title: str) -> str:
    cwd_name = Path(cwd).name.strip()
    if cwd_name:
        return cwd_name
    page_title = extract_page_title(window_title)
    if page_title != "unknown":
        return page_title
    return "unknown"


def get_gui_input() -> str:
    import tkinter as tk

    result: dict[str, str] = {"value": ""}
    root = tk.Tk()
    root.title("Core-Stream")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.geometry("480x52")
    entry = tk.Entry(root, font=("Arial", 12))
    entry.pack(fill="both", expand=True, padx=8, pady=8)
    entry.focus_set()

    def hide_before_close() -> None:
        try:
            if platform.system() == "Windows":
                root.iconify()
            else:
                root.withdraw()
            root.update_idletasks()
            root.update()
        except Exception:
            pass

    def submit(_: Any = None) -> None:
        result["value"] = entry.get()
        hide_before_close()
        root.destroy()

    def cancel(_: Any = None) -> None:
        hide_before_close()
        root.destroy()

    root.bind("<Return>", submit)
    root.bind("<Escape>", cancel)
    root.mainloop()
    return result["value"]


def make_screenshot_path(shot_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return shot_dir / f"{stamp}.png"


def capture_screenshot(output_path: Path) -> tuple[bool, str, str]:
    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore
    except Exception as exc:
        return False, "unknown", f"mss import failed: {exc}"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with mss.MSS() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(output_path))
        return True, str(output_path), ""
    except Exception as exc:
        return False, "unknown", str(exc)


def parse_log_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Core-Stream client (default: send one event to daemon)",
        epilog=(
            "subcommands:\n"
            "  report   request progress report from daemon\n"
            "  next     request todo list from daemon\n"
            "  settings update daemon AI setting\n\n"
            "examples:\n"
            "  python log.py \"fix docker bug\"\n"
            "  python log.py report --period today --format md\n"
            "  python log.py next --llm never --format json\n"
            "  python log.py settings --ai on"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--shot", dest="capture_shot", action="store_true", help="Capture screenshot")
    parser.add_argument("--no-shot", dest="capture_shot", action="store_false", help="Disable screenshot")
    parser.set_defaults(capture_shot=True)
    parser.add_argument("--shot-dir", default=str(DEFAULT_SHOT_DIR), help="Directory for screenshots")
    parser.add_argument("--stdin", action="store_true", help="Read event body from stdin")
    parser.add_argument("--type", default="thought", help="Event type (thought/stdin/git/system/...)")
    parser.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL, help="Daemon base URL")
    parser.add_argument("--timeout", type=float, default=0.8, help="POST /events timeout seconds")
    # parser.add_argument("--async", dest="async_mode", action="store_true", help="Process in background (all processing is async by default)")
    parser.add_argument("message", nargs="*", help="Event body. If omitted, GUI or stdin is used.")
    return parser.parse_args(argv[1:])


def parse_report_args(argv: list[str], mode: str) -> argparse.Namespace:
    label = "report" if mode == "report" else "next(todo)"
    parser = argparse.ArgumentParser(
        description=f"Core-Stream {label}: request /reports/generate from daemon"
    )
    parser.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL, help="Daemon base URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="POST /reports/generate timeout seconds")
    parser.add_argument("--period", choices=["today", "week", "range"], default="today", help="Time window")
    parser.add_argument("--from-date", help="Range start date (YYYY-MM-DD), required for --period range")
    parser.add_argument("--to-date", help="Range end date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--llm", choices=["never", "auto", "always"], default="auto", help="Report LLM strategy")
    parser.add_argument("--llm-threshold", type=int, default=60, help="Use LLM in auto when entries >= N")
    parser.add_argument("--format", choices=["md", "json", "both"], default="both", help="Client output format")
    parser.add_argument("--no-save", action="store_true", help="Do not save report files on daemon")
    args = parser.parse_args(argv[2:])
    args.mode = mode
    return args


def parse_settings_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core-Stream settings: update daemon runtime settings")
    parser.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL, help="Daemon base URL")
    parser.add_argument("--timeout", type=float, default=10.0, help="POST /settings/ai timeout seconds")
    parser.add_argument("--ai", choices=["on", "off"], required=True, help="Enable/disable daemon AI worker")
    return parser.parse_args(argv[2:])


def resolve_raw_input(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.message:
        filtered = [arg for arg in args.message if arg.strip()]
        if filtered:
            return " ".join(filtered), "cli", get_clipboard_text()
    if args.stdin or not sys.stdin.isatty():
        return sys.stdin.read(), "stdin", get_clipboard_text()
    raw = ""
    try:
        raw = get_gui_input()
    except Exception:
        raw = ""
    clipboard = get_clipboard_text()
    if not raw.strip() and clipboard.strip():
        raw = clipboard
    return raw, "shortcut", clipboard


def print_warnings(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        return
    for item in warnings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        message = str(item.get("message", "")).strip()
        action = str(item.get("action", "")).strip()
        if not message:
            continue
        prefix = f"[warning:{code}] " if code else "[warning] "
        print(prefix + message, file=sys.stderr)
        if action:
            print(f"  action: {action}", file=sys.stderr)


def post_event(args: argparse.Namespace) -> int:
    raw_text, source, clipboard = resolve_raw_input(args)
    
    def process_event() -> None:
        window_title = get_active_window_title()
        page_title = extract_page_title(window_title)
        shot_ok = False
        shot_path = "unknown"
        shot_error = ""
        if args.capture_shot:
            time.sleep(0.15)
            shot_ok, shot_path, shot_error = capture_screenshot(
                make_screenshot_path(Path(args.shot_dir).expanduser())
            )
        payload = {
            "type": args.type,
            "body": raw_text,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "context": {
                "cwd": os.getcwd(),
                "win": window_title or "unknown",
                "host": socket.gethostname(),
                "is_browser": is_browser_window(window_title),
                "page_title": page_title,
                "os": platform.system(),
            },
            "meta": {
                "clipboard": clipboard[:500] if clipboard else "",
                "project_hint": infer_project_hint(os.getcwd(), window_title),
                "screenshot": {
                    "enabled": bool(args.capture_shot),
                    "ok": shot_ok,
                    "path": shot_path if args.capture_shot else "",
                    "error": shot_error,
                },
            },
        }
        url = args.daemon_url.rstrip("/") + "/events"
        try:
            response = requests.post(url, json=payload, timeout=args.timeout)
            if response.status_code != 200:
                print(f"daemon rejected event: HTTP {response.status_code} {response.text[:250]}", file=sys.stderr)
                return
            try:
                print_warnings(response.json())
            except ValueError:
                pass
        except requests.RequestException as exc:
            print(f"failed to send event to daemon: {exc}", file=sys.stderr)
    
    thread = threading.Thread(target=process_event, daemon=False)
    thread.start()
    thread.join(timeout=2.0)
    return 0


def generate_report(args: argparse.Namespace) -> int:
    url = args.daemon_url.rstrip("/") + "/reports/generate"
    payload = {
        "mode": args.mode,
        "period": args.period,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "llm": args.llm,
        "llm_threshold": args.llm_threshold,
        "save": not args.no_save,
    }
    try:
        response = requests.post(url, json=payload, timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"failed to request report: {exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"daemon report error: HTTP {response.status_code} {response.text[:300]}", file=sys.stderr)
        return 1
    data = response.json()
    print_warnings(data)
    markdown = str(data.get("markdown", ""))
    report_payload = data.get("payload", {})
    if args.format == "md":
        sys.stdout.write(markdown)
    elif args.format == "json":
        sys.stdout.write(json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(markdown + "\n---\n\n")
        sys.stdout.write(json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def update_settings(args: argparse.Namespace) -> int:
    url = args.daemon_url.rstrip("/") + "/settings/ai"
    payload = {"enabled": args.ai == "on"}
    try:
        response = requests.post(url, json=payload, timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"failed to update settings: {exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"daemon settings error: HTTP {response.status_code} {response.text[:250]}", file=sys.stderr)
        return 1
    try:
        print_warnings(response.json())
    except ValueError:
        pass
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "report":
        return generate_report(parse_report_args(argv, mode="report"))
    if len(argv) > 1 and argv[1] == "next":
        return generate_report(parse_report_args(argv, mode="todo"))
    if len(argv) > 1 and argv[1] == "settings":
        return update_settings(parse_settings_args(argv))
    return post_event(parse_log_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
