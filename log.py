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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pyperclip  # type: ignore
except Exception:
    pyperclip = None


THOUGHT_STREAM_PATH = Path.home() / "thought_stream.jsonl"
DEFAULT_SHOT_DIR = Path.home() / "thought_stream_shots"
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
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return "unknown"
        output = (result.stdout or "").strip()
        return output or "unknown"
    except Exception:
        return "unknown"


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
        if text is None:
            return ""
        return str(text)
    except Exception:
        return ""


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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Core-Stream thought logger")
    parser.add_argument(
        "--shot",
        dest="capture_shot",
        action="store_true",
        help="Capture a screenshot (default: enabled)",
    )
    parser.add_argument(
        "--no-shot",
        dest="capture_shot",
        action="store_false",
        help="Disable screenshot capture",
    )
    parser.set_defaults(capture_shot=True)
    parser.add_argument(
        "--shot-dir",
        default=str(DEFAULT_SHOT_DIR),
        help="Directory for screenshots",
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="Message to log. If omitted, a GUI input window is shown.",
    )
    return parser.parse_args(argv[1:])


def resolve_raw_input(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.message:
        raw = " ".join(args.message)
        clipboard = get_clipboard_text()
        return raw, "cli", clipboard

    raw = ""
    source = "shortcut"
    try:
        raw = get_gui_input()
    except Exception:
        raw = ""

    clipboard = get_clipboard_text()
    if not raw.strip() and clipboard.strip():
        raw = clipboard
    return raw, source, clipboard


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
        mss_factory = getattr(mss, "MSS", None) or getattr(mss, "mss", None)
        if mss_factory is None:
            return False, "unknown", "mss backend unavailable: missing MSS/mss constructor"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with mss_factory() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(output_path))
        return True, str(output_path), ""
    except Exception as exc:
        return False, "unknown", str(exc)


def build_record(
    raw_text: str,
    source: str,
    clipboard: str,
    capture_shot: bool,
    shot_dir: Path,
) -> dict[str, Any]:
    cwd = os.getcwd()
    window_title = get_active_window_title()
    page_title = extract_page_title(window_title)
    shot_ok = False
    shot_path = "unknown"
    shot_error = ""

    if capture_shot:
        # Let the GUI window teardown settle so it is less likely to appear in captures.
        time.sleep(0.15)
        shot_ok, shot_path, shot_error = capture_screenshot(make_screenshot_path(shot_dir))

    return {
        "t": datetime.now(timezone.utc).isoformat(),
        "raw": raw_text,
        "src": source,
        "ctx": {
            "cwd": cwd,
            "win": window_title or "unknown",
            "host": socket.gethostname(),
            "is_browser": is_browser_window(window_title),
            "page_title": page_title,
            "os": platform.system(),
        },
        "meta": {
            "clipboard": clipboard[:500] if clipboard else "",
            "project_hint": infer_project_hint(cwd, window_title),
            "screenshot": {
                "enabled": capture_shot,
                "ok": shot_ok,
                "path": shot_path if capture_shot else "",
                "error": shot_error,
            },
        },
    }


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args(sys.argv)
    raw_text, source, clipboard = resolve_raw_input(args)
    record = build_record(
        raw_text=raw_text,
        source=source,
        clipboard=clipboard,
        capture_shot=bool(args.capture_shot),
        shot_dir=Path(args.shot_dir).expanduser(),
    )
    append_jsonl(THOUGHT_STREAM_PATH, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
