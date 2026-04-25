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


def load_api_key(config_file: str | None = None) -> str | None:
    """Load API key from env var, config file, or None"""
    # Priority: env var > config file
    if api_key := os.environ.get("LOGGER_API_KEY"):
        return api_key
    
    if config_file:
        try:
            path = Path(config_file).expanduser()
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                    return data.get("api_key")
        except Exception:
            pass
    
    return None


def load_client_config(config_file: str) -> dict[str, Any]:
    """Load client configuration from JSON file"""
    try:
        path = Path(config_file).expanduser()
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def parse_log_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Core-Stream client (default: send one event to daemon)",
        epilog=(
            "subcommands:\n"
            "  report    request progress report from daemon\n"
            "  next      request todo list from daemon\n"
            "  settings  update daemon AI setting\n"
            "  status    check daemon health and state\n"
            "  backfill  retry classification of unclassified events\n\n"
            "examples:\n"
            "  python log.py \"fix docker bug\"\n"
            "  python log.py --gui\n"
            "  python log.py report --period today --format md\n"
            "  python log.py next --llm never --format json\n"
            "  python log.py settings --ai on\n"
            "  python log.py status\n"
            "  python log.py backfill"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--shot", dest="capture_shot", action="store_true", help="Capture screenshot")
    parser.add_argument("--no-shot", dest="capture_shot", action="store_false", help="Disable screenshot")
    parser.set_defaults(capture_shot=True)
    parser.add_argument("--shot-dir", default=None, help="Directory for screenshots")
    parser.add_argument("--gui", action="store_true", help="Force GUI input (ignore message arguments)")
    parser.add_argument("--stdin", action="store_true", help="Read event body from stdin")
    parser.add_argument("--type", default=None, help="Event type (thought/stdin/git/system/...)")
    parser.add_argument("--daemon-url", default=None, help="Daemon base URL")
    parser.add_argument("--api-key", type=str, default=None, help="API key for authenticated daemon (Bearer token)")
    parser.add_argument("--config-file", type=str, default=None, help="Load settings from config file (CLI args override)")
    parser.add_argument("--timeout", type=float, default=None, help="POST /events timeout seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    # parser.add_argument("--async", dest="async_mode", action="store_true", help="Process in background (all processing is async by default)")
    parser.add_argument("message", nargs="*", help="Event body. If omitted, GUI or stdin is used.")
    
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    config_data = {}
    config_file = args.config_file
    
    # If no config file specified, try default path
    if not config_file:
        default_client_config = Path.home() / '.logger' / 'client.json'
        if default_client_config.exists():
            config_file = str(default_client_config)
    
    if config_file:
        config_data = load_client_config(config_file)
    
    # Apply config file values if CLI arg not provided
    if args.shot_dir is None:
        args.shot_dir = config_data.get('shot_dir', str(DEFAULT_SHOT_DIR))
    if args.type is None:
        args.type = config_data.get('type', 'thought')
    if args.daemon_url is None:
        args.daemon_url = config_data.get('daemon_url', DEFAULT_DAEMON_URL)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 0.8)
    if not args.gui:
        args.gui = config_data.get('gui', False)
    if not args.stdin:
        args.stdin = config_data.get('stdin', False)
    if not args.debug:
        args.debug = config_data.get('debug', False)
    
    return args


def parse_report_args(argv: list[str], mode: str) -> argparse.Namespace:
    label = "report" if mode == "report" else "next(todo)"
    parser = argparse.ArgumentParser(
        description=f"Core-Stream {label}: request /reports/generate from daemon"
    )
    parser.add_argument("--config-file", type=str, default=None, help="Load settings from config file (CLI args override)")
    parser.add_argument("--daemon-url", default=None, help="Daemon base URL")
    parser.add_argument("--api-key", type=str, default=None, help="API key for authenticated daemon")
    parser.add_argument("--timeout", type=float, default=None, help="POST /reports/generate timeout seconds")
    parser.add_argument("--period", choices=["today", "week", "range"], default="today", help="Time window")
    parser.add_argument("--from-date", help="Range start date (YYYY-MM-DD), required for --period range")
    parser.add_argument("--to-date", help="Range end date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--llm", choices=["never", "auto", "always"], default="auto", help="Report LLM strategy")
    parser.add_argument("--llm-threshold", type=int, default=60, help="Use LLM in auto when entries >= N")
    parser.add_argument("--format", choices=["md", "json", "both"], default="both", help="Client output format")
    parser.add_argument("--no-save", action="store_true", help="Do not save report files on daemon")
    args = parser.parse_args(argv[2:])
    args.mode = mode
    
    # Priority: CLI arg > config file > default
    config_data = {}
    config_file = args.config_file
    
    # If no config file specified, try default path
    if not config_file:
        default_client_config = Path.home() / '.logger' / 'client.json'
        if default_client_config.exists():
            config_file = str(default_client_config)
    
    if config_file:
        config_data = load_client_config(config_file)
    
    if args.daemon_url is None:
        args.daemon_url = config_data.get('daemon_url', DEFAULT_DAEMON_URL)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 15.0)
    
    return args


def parse_settings_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core-Stream settings: update daemon runtime settings")
    parser.add_argument("--config-file", type=str, default=None, help="Load settings from config file (CLI args override)")
    parser.add_argument("--daemon-url", default=None, help="Daemon base URL")
    parser.add_argument("--api-key", type=str, default=None, help="API key for authenticated daemon")
    parser.add_argument("--timeout", type=float, default=None, help="POST /settings/ai timeout seconds")
    parser.add_argument("--ai", choices=["on", "off"], required=True, help="Enable/disable daemon AI worker")
    args = parser.parse_args(argv[2:])
    
    # Priority: CLI arg > config file > default
    config_data = {}
    config_file = args.config_file
    
    # If no config file specified, try default path
    if not config_file:
        default_client_config = Path.home() / '.logger' / 'client.json'
        if default_client_config.exists():
            config_file = str(default_client_config)
    
    if config_file:
        config_data = load_client_config(config_file)
    
    if args.daemon_url is None:
        args.daemon_url = config_data.get('daemon_url', DEFAULT_DAEMON_URL)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 10.0)
    
    return args


def parse_status_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core-Stream status: check daemon health and state")
    parser.add_argument("--config-file", type=str, default=None, help="Load settings from config file (CLI args override)")
    parser.add_argument("--daemon-url", default=None, help="Daemon base URL")
    parser.add_argument("--api-key", type=str, default=None, help="API key for authenticated daemon")
    parser.add_argument("--timeout", type=float, default=None, help="GET /health timeout seconds")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    args = parser.parse_args(argv[2:])
    
    # Priority: CLI arg > config file > default
    config_data = {}
    config_file = args.config_file
    
    # If no config file specified, try default path
    if not config_file:
        default_client_config = Path.home() / '.logger' / 'client.json'
        if default_client_config.exists():
            config_file = str(default_client_config)
    
    if config_file:
        config_data = load_client_config(config_file)
    
    if args.daemon_url is None:
        args.daemon_url = config_data.get('daemon_url', DEFAULT_DAEMON_URL)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 5.0)
    
    return args


def parse_backfill_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core-Stream backfill: retry classification of unclassified events")
    parser.add_argument("--config-file", type=str, default=None, help="Load settings from config file (CLI args override)")
    parser.add_argument("--daemon-url", default=None, help="Daemon base URL")
    parser.add_argument("--api-key", type=str, default=None, help="API key for authenticated daemon")
    parser.add_argument("--timeout", type=float, default=None, help="POST /analyze/backfill timeout seconds")
    args = parser.parse_args(argv[2:])
    
    # Priority: CLI arg > config file > default
    config_data = {}
    config_file = args.config_file
    
    # If no config file specified, try default path
    if not config_file:
        default_client_config = Path.home() / '.logger' / 'client.json'
        if default_client_config.exists():
            config_file = str(default_client_config)
    
    if config_file:
        config_data = load_client_config(config_file)
    
    if args.daemon_url is None:
        args.daemon_url = config_data.get('daemon_url', DEFAULT_DAEMON_URL)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 30.0)
    
    return args


def resolve_raw_input(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.gui:
        raw = ""
        try:
            raw = get_gui_input()
        except Exception:
            raw = ""
        clipboard = get_clipboard_text()
        return raw, "gui", clipboard
    if args.message:
        filtered = [arg for arg in args.message if arg.strip()]
        if filtered:
            text = " ".join(filtered)
            if text.strip():
                return text, "cli", get_clipboard_text()
    if args.stdin or not sys.stdin.isatty():
        return sys.stdin.read(), "stdin", get_clipboard_text()
    raw = ""
    try:
        raw = get_gui_input()
    except Exception:
        raw = ""
    clipboard = get_clipboard_text()
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


def get_request_headers(args: argparse.Namespace) -> dict[str, str]:
    """Build HTTP headers including API key if configured"""
    headers = {"Content-Type": "application/json"}
    
    # Priority: CLI arg > config file > env var
    api_key = args.api_key if hasattr(args, 'api_key') and args.api_key else None
    if not api_key and hasattr(args, 'config_file') and args.config_file:
        api_key = load_api_key(args.config_file)
    if not api_key:
        api_key = load_api_key()
    
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    return headers


def debug_log(args: argparse.Namespace, message: str) -> None:
    if getattr(args, 'debug', False):
        timestamp = datetime.now().astimezone().isoformat()
        print(f"[debug {timestamp}] {message}", file=sys.stderr)


def post_event(args: argparse.Namespace) -> int:
    raw_text, source, clipboard = resolve_raw_input(args)
    debug_log(args, f"input resolved: source={source}, text_len={len(raw_text)}")
    
    if not raw_text.strip():
        print("no message to send", file=sys.stderr)
        return 1
    
    debug_log(args, f"message valid, starting background process")
    
    def process_event() -> None:
        debug_log(args, "process_event: collecting context")
        window_title = get_active_window_title()
        page_title = extract_page_title(window_title)
        debug_log(args, f"window_title={window_title}")
        
        shot_ok = False
        shot_path = "unknown"
        shot_error = ""
        if args.capture_shot:
            time.sleep(0.15)
            debug_log(args, "process_event: capturing screenshot")
            shot_ok, shot_path, shot_error = capture_screenshot(
                make_screenshot_path(Path(args.shot_dir).expanduser())
            )
            debug_log(args, f"screenshot: ok={shot_ok}, path={shot_path}, error={shot_error}")
        
        payload = {
            "type": args.type,
            "body": raw_text,
            "source": source,
            "created_at": datetime.now().astimezone().isoformat(),
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
        debug_log(args, f"posting to {url}")
        try:
            response = requests.post(url, json=payload, headers=get_request_headers(args), timeout=args.timeout)
            debug_log(args, f"response: status={response.status_code}")
            if response.status_code != 200:
                print(f"daemon rejected event: HTTP {response.status_code} {response.text[:250]}", file=sys.stderr)
                return
            try:
                print_warnings(response.json())
            except ValueError:
                pass
            debug_log(args, "event posted successfully")
        except requests.RequestException as exc:
            debug_log(args, f"request failed: {exc}")
            print(f"failed to send event to daemon: {exc}", file=sys.stderr)
    
    thread = threading.Thread(target=process_event, daemon=False)
    thread.start()
    debug_log(args, "waiting for thread to complete (max 2s)")
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
        response = requests.post(url, json=payload, headers=get_request_headers(args), timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"failed to request report: {exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        try:
            error_data = response.json()
            detail = error_data.get("detail", {})
            if isinstance(detail, dict):
                message = detail.get("message", "Unknown error")
                print(f"daemon report error: HTTP {response.status_code}: {message}", file=sys.stderr)
                warnings = detail.get("warnings", [])
                if warnings:
                    print("\nWarnings:", file=sys.stderr)
                    for w in warnings:
                        if isinstance(w, dict):
                            code = w.get("code", "")
                            msg = w.get("message", "")
                            prefix = f"  [{code}] " if code else "  "
                            print(prefix + msg, file=sys.stderr)
                hint = detail.get("hint", "")
                if hint:
                    print(f"\nHint: {hint}", file=sys.stderr)
                    print(f"  → Try: python log.py backfill", file=sys.stderr)
                failures = detail.get("recent_analysis_failures", [])
                if failures:
                    print("\nRecent failures:", file=sys.stderr)
                    for f in failures[:3]:
                        if isinstance(f, dict):
                            print(f"  Event: {f.get('event_id', 'unknown')[:8]}", file=sys.stderr)
                            print(f"    Error: {f.get('error', 'unknown')[:100]}", file=sys.stderr)
            else:
                print(f"daemon report error: HTTP {response.status_code} {response.text[:500]}", file=sys.stderr)
        except ValueError:
            print(f"daemon report error: HTTP {response.status_code} {response.text[:500]}", file=sys.stderr)
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
        response = requests.post(url, json=payload, headers=get_request_headers(args), timeout=args.timeout)
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


def check_status(args: argparse.Namespace) -> int:
    url = args.daemon_url.rstrip("/") + "/health"
    try:
        response = requests.get(url, headers=get_request_headers(args), timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"failed to check status: {exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"daemon error: HTTP {response.status_code}", file=sys.stderr)
        return 1
    
    data = response.json()
    
    if args.format == "json":
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return 0
    
    # Text format
    status = data.get("status", "unknown")
    ai_enabled = data.get("ai_enabled", False)
    queue_size = data.get("queue_size", 0)
    
    print(f"Status: {status}")
    print(f"AI enabled: {'yes' if ai_enabled else 'no'}")
    print(f"Queue size: {queue_size}")
    
    # Analysis state
    analysis_state = data.get("analysis_state", {})
    if analysis_state:
        print("\nAnalysis State:")
        print(f"  Pending: {analysis_state.get('pending_events', 0)}")
        print(f"  Processing: {analysis_state.get('processing_events', 0)}")
        print(f"  In-flight: {analysis_state.get('inflight_events', 0)}")
        print(f"  Done: {analysis_state.get('done_events', 0)}")
        print(f"  Failed: {analysis_state.get('failed_events', 0)}")
        print(f"  Unclassified: {analysis_state.get('unclassified_events', 0)}")
        print(f"  Resumed on startup: {analysis_state.get('resumed_on_startup', 0)}")
    
    # Last analysis error
    last_error = data.get("last_analysis_error")
    if last_error:
        print("\nLast Analysis Error:")
        print(f"  Event: {last_error.get('event_id', 'unknown')[:8]}")
        print(f"  Error: {last_error.get('error', 'unknown')[:100]}")
    
    # Warnings
    print_warnings(data)
    
    return 0


def run_backfill(args: argparse.Namespace) -> int:
    url = args.daemon_url.rstrip("/") + "/analyze/backfill"
    debug_log(args, f"requesting backfill at {url}")
    try:
        response = requests.post(url, json={}, headers=get_request_headers(args), timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"failed to request backfill: {exc}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"daemon backfill error: HTTP {response.status_code}", file=sys.stderr)
        try:
            print(f"  {response.json()}", file=sys.stderr)
        except ValueError:
            print(f"  {response.text[:300]}", file=sys.stderr)
        return 1
    
    data = response.json()
    queued = data.get("queued", 0)
    print(f"Backfill started: {queued} events queued for re-analysis", file=sys.stderr)
    print_warnings(data)
    return 0



def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "report":
        return generate_report(parse_report_args(argv, mode="report"))
    if len(argv) > 1 and argv[1] == "next":
        return generate_report(parse_report_args(argv, mode="todo"))
    if len(argv) > 1 and argv[1] == "settings":
        return update_settings(parse_settings_args(argv))
    if len(argv) > 1 and argv[1] == "status":
        return check_status(parse_status_args(argv))
    if len(argv) > 1 and argv[1] == "backfill":
        return run_backfill(parse_backfill_args(argv))
    return post_event(parse_log_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
