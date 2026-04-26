#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
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
LOGGER_DIR = Path.home() / ".logger"
PENDING_EVENTS_FILE = LOGGER_DIR / "pending_events.jsonl"
LAST_EVENT_LOG_FILE = LOGGER_DIR / "last_event.log"
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


def capture_screenshot_base64() -> tuple[bool, str, str]:
    """Capture screenshot and return Base64-encoded PNG data"""
    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore
    except Exception as exc:
        return False, "", f"mss import failed: {exc}"
    try:
        with mss.MSS() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            png_data = mss.tools.to_png(shot.rgb, shot.size)
            b64_data = base64.b64encode(png_data).decode('utf-8')
            return True, b64_data, ""
    except Exception as exc:
        return False, "", str(exc)


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


def _get_client_config_or_default() -> tuple[str | None, dict[str, Any]]:
    """Get config file and data: try ~/.logger/client.json, return (path, data)"""
    default_path = Path.home() / '.logger' / 'client.json'
    if default_path.exists():
        config_file = str(default_path)
        return config_file, load_client_config(config_file)
    return None, {}


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
    parser.add_argument("--fire-and-forget", action="store_true", help="Release shell immediately, save results to ~/.logger/last_event.log")
    # parser.add_argument("--async", dest="async_mode", action="store_true", help="Process in background (all processing is async by default)")
    parser.add_argument("message", nargs="*", help="Event body. If omitted, GUI or stdin is used.")
    
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    config_file = args.config_file
    if args.config_file:
        config_data = load_client_config(args.config_file)
    else:
        # Try default config path
        config_file, config_data = _get_client_config_or_default()
    
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
    parser.add_argument("--format", choices=["md", "json", "both"], default="md", help="Client output format")
    parser.add_argument("--no-save", action="store_true", help="Do not save report files on daemon")
    args = parser.parse_args(argv[1:])
    args.mode = mode
    
    # Priority: CLI arg > config file > default
    if args.config_file:
        config_data = load_client_config(args.config_file)
    else:
        # Try default config path
        _, config_data = _get_client_config_or_default()
    
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
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    if args.config_file:
        config_data = load_client_config(args.config_file)
    else:
        # Try default config path
        _, config_data = _get_client_config_or_default()
    
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
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    if args.config_file:
        config_data = load_client_config(args.config_file)
    else:
        # Try default config path
        _, config_data = _get_client_config_or_default()
    
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
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    if args.config_file:
        config_data = load_client_config(args.config_file)
    else:
        # Try default config path
        _, config_data = _get_client_config_or_default()
    
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


def ensure_logger_dir() -> None:
    """Ensure ~/.logger directory exists"""
    LOGGER_DIR.mkdir(parents=True, exist_ok=True)


def save_to_pending_events(payload: dict) -> None:
    """Save event payload to pending_events.jsonl for later retry"""
    ensure_logger_dir()
    with open(PENDING_EVENTS_FILE, 'a') as f:
        f.write(json.dumps(payload, ensure_ascii=False) + '\n')


def remove_from_pending_events(event_id: str) -> None:
    """Remove successfully sent event from pending_events.jsonl"""
    if not PENDING_EVENTS_FILE.exists():
        return
    
    pending = []
    with open(PENDING_EVENTS_FILE, 'r') as f:
        for line in f:
            try:
                event = json.loads(line)
                if event.get('id') != event_id:
                    pending.append(event)
            except json.JSONDecodeError:
                pass
    
    with open(PENDING_EVENTS_FILE, 'w') as f:
        for event in pending:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')


def save_last_event_log(result: dict) -> None:
    """Save last event execution result to log file"""
    ensure_logger_dir()
    with open(LAST_EVENT_LOG_FILE, 'w') as f:
        f.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')


def post_event(args: argparse.Namespace) -> int:
    raw_text, source, clipboard = resolve_raw_input(args)
    debug_log(args, f"input resolved: source={source}, text_len={len(raw_text)}")
    
    if not raw_text.strip():
        print("no message to send", file=sys.stderr)
        return 1
    
    debug_log(args, f"message valid, starting background process")
    
    fire_and_forget = getattr(args, 'fire_and_forget', False)
    
    def process_event() -> None:
        start_time = time.time()
        warnings_list = []
        errors_list = []
        event_id = None
        
        try:
            debug_log(args, "process_event: collecting context")
            window_title = get_active_window_title()
            page_title = extract_page_title(window_title)
            debug_log(args, f"window_title={window_title}")
            
            screenshot_data = ""
            shot_error = ""
            if args.capture_shot:
                time.sleep(0.15)
                debug_log(args, "process_event: capturing screenshot")
                shot_ok, screenshot_data, shot_error = capture_screenshot_base64()
                debug_log(args, f"screenshot: ok={shot_ok}, error={shot_error}")
            
            import uuid
            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            
            payload = {
                "id": event_id,
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
                        "ok": bool(screenshot_data),
                        "error": shot_error,
                    },
                },
            }
            
            # Include screenshot data if captured
            if screenshot_data:
                payload["screenshot_data"] = screenshot_data
            
            # fire-and-forget: save to pending before sending
            if fire_and_forget:
                debug_log(args, f"fire-and-forget: saving to pending (event_id={event_id})")
                save_to_pending_events(payload)
            
            url = args.daemon_url.rstrip("/") + "/events"
            debug_log(args, f"posting to {url}")
            
            try:
                # Use longer timeout for background thread (large image uploads may need time)
                # User shell is already released, so we can afford to wait
                background_timeout = max(args.timeout * 10, 30.0)
                response = requests.post(url, json=payload, headers=get_request_headers(args), timeout=background_timeout)
                debug_log(args, f"response: status={response.status_code}")
                
                if response.status_code == 200:
                    try:
                        resp_data = response.json()
                        if not fire_and_forget:
                            # Default mode: print warnings to stderr
                            print_warnings(resp_data)
                        else:
                            # Fire-and-forget: collect warnings for logging
                            if "warnings" in resp_data:
                                for w in resp_data["warnings"]:
                                    if isinstance(w, dict):
                                        warnings_list.append(w.get("message", str(w)))
                    except ValueError:
                        pass
                    debug_log(args, "event posted successfully")
                    
                    # Remove from pending if send succeeded
                    if fire_and_forget:
                        debug_log(args, f"fire-and-forget: removing from pending (event_id={event_id})")
                        remove_from_pending_events(event_id)
                    
                    status = "success"
                else:
                    error_msg = f"HTTP {response.status_code}"
                    errors_list.append(error_msg)
                    if not fire_and_forget:
                        print(f"daemon rejected event: {error_msg} {response.text[:250]}", file=sys.stderr)
                    debug_log(args, f"daemon rejected: {error_msg}")
                    status = "error"
                    
            except requests.Timeout as exc:
                error_msg = f"timeout: {exc}"
                errors_list.append(error_msg)
                if not fire_and_forget:
                    print(f"failed to send event to daemon: {error_msg}", file=sys.stderr)
                debug_log(args, f"request timeout: {exc}")
                status = "timeout"
            except requests.RequestException as exc:
                error_msg = str(exc)
                errors_list.append(error_msg)
                if not fire_and_forget:
                    print(f"failed to send event to daemon: {error_msg}", file=sys.stderr)
                debug_log(args, f"request failed: {exc}")
                status = "error"
            
        except Exception as exc:
            error_msg = str(exc)
            errors_list.append(error_msg)
            if not fire_and_forget:
                print(f"error processing event: {error_msg}", file=sys.stderr)
            debug_log(args, f"unexpected error: {exc}")
            status = "error"
        
        finally:
            # Save result log if fire-and-forget mode
            if fire_and_forget:
                duration_ms = int((time.time() - start_time) * 1000)
                result = {
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "status": locals().get('status', 'error'),
                    "event_id": event_id or "unknown",
                    "warnings": warnings_list,
                    "errors": errors_list,
                    "duration_ms": duration_ms
                }
                save_last_event_log(result)
                debug_log(args, f"fire-and-forget: result saved to {LAST_EVENT_LOG_FILE}")
    
    # Always use non-daemon thread to allow completion
    thread = threading.Thread(target=process_event, daemon=False)
    thread.start()
    
    if not fire_and_forget:
        # Default mode: wait for thread to complete
        debug_log(args, "waiting for thread to complete (max 30s)")
        thread.join(timeout=30)
    else:
        # Fire-and-forget mode: release shell immediately without waiting
        debug_log(args, "fire-and-forget mode: shell released immediately")
    
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



def run_retry_send(args: argparse.Namespace) -> int:
    """Retry sending all pending events"""
    if not PENDING_EVENTS_FILE.exists():
        print("No pending events to retry", file=sys.stderr)
        return 0
    
    pending_events = []
    with open(PENDING_EVENTS_FILE, 'r') as f:
        for line in f:
            try:
                event = json.loads(line)
                pending_events.append(event)
            except json.JSONDecodeError:
                pass
    
    if not pending_events:
        print("No pending events to retry", file=sys.stderr)
        return 0
    
    print(f"Retrying {len(pending_events)} pending events...", file=sys.stderr)
    
    success_count = 0
    failure_count = 0
    
    for payload in pending_events:
        event_id = payload.get('id', 'unknown')
        url = args.daemon_url.rstrip("/") + "/events"
        
        try:
            response = requests.post(
                url,
                json=payload,
                headers=get_request_headers(args),
                timeout=max(args.timeout * 10, 30.0)
            )
            
            if response.status_code == 200:
                print(f"✓ {event_id}: sent successfully", file=sys.stderr)
                remove_from_pending_events(event_id)
                success_count += 1
            else:
                print(f"✗ {event_id}: HTTP {response.status_code}", file=sys.stderr)
                failure_count += 1
        except requests.RequestException as exc:
            print(f"✗ {event_id}: {exc}", file=sys.stderr)
            failure_count += 1
    
    print(f"\nRetry complete: {success_count} sent, {failure_count} failed", file=sys.stderr)
    return 0 if failure_count == 0 else 1


def parse_retry_send_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core-Stream retry-send: resend pending events")
    parser.add_argument("--config-file", type=str, default=None, help="Load settings from config file (CLI args override)")
    parser.add_argument("--daemon-url", default=None, help="Daemon base URL")
    parser.add_argument("--api-key", type=str, default=None, help="API key for authenticated daemon")
    parser.add_argument("--timeout", type=float, default=None, help="POST timeout seconds")
    
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    if args.config_file:
        config_data = load_client_config(args.config_file)
    else:
        config_file, config_data = _get_client_config_or_default()
    
    if args.daemon_url is None:
        args.daemon_url = config_data.get('daemon_url', DEFAULT_DAEMON_URL)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 30.0)
    
    return args


def _find_subcommand(argv: list[str]) -> str | None:
    """Find subcommand in argv at any position."""
    subcommands = {"report", "next", "settings", "status", "backfill", "retry-send"}
    for arg in argv[1:]:
        if arg in subcommands:
            return arg
    return None


def _remove_subcommand(argv: list[str], subcommand: str) -> list[str]:
    """Remove subcommand from argv and return cleaned list."""
    result = [argv[0]]
    for arg in argv[1:]:
        if arg != subcommand:
            result.append(arg)
    return result


def main(argv: list[str]) -> int:
    # Handle fire-and-forget mode: spawn as background subprocess for instant shell release
    if "--fire-and-forget" in argv:
        # Check if this is already a subprocess (to avoid infinite recursion)
        if os.environ.get("_LOGGER_FFG_SUBPROCESS") != "1":
            # Spawn as background subprocess
            env = os.environ.copy()
            env["_LOGGER_FFG_SUBPROCESS"] = "1"
            subprocess.Popen(
                [sys.executable] + argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None
            )
            return 0
    
    subcommand = _find_subcommand(argv)
    cleaned_argv = _remove_subcommand(argv, subcommand) if subcommand else argv
    
    if subcommand == "report":
        return generate_report(parse_report_args(cleaned_argv, mode="report"))
    if subcommand == "next":
        return generate_report(parse_report_args(cleaned_argv, mode="todo"))
    if subcommand == "settings":
        return update_settings(parse_settings_args(cleaned_argv))
    if subcommand == "status":
        return check_status(parse_status_args(cleaned_argv))
    if subcommand == "backfill":
        return run_backfill(parse_backfill_args(cleaned_argv))
    if subcommand == "retry-send":
        return run_retry_send(parse_retry_send_args(cleaned_argv))
    return post_event(parse_log_args(cleaned_argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
