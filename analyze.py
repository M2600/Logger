#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_LOG_PATH = Path.home() / "thought_stream.jsonl"
DEFAULT_CLASSIFIED_PATH = Path.home() / ".core_stream_classified.jsonl"
DEFAULT_REPORT_DIR = Path.cwd() / "reports"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"

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


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Core-Stream analyzer: split classify and report workflows"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser(
        "classify",
        help="Classify new logs with LLM and store cache",
    )
    classify.add_argument("--input", default=str(DEFAULT_LOG_PATH), help="Input JSONL log path")
    classify.add_argument(
        "--classified-output",
        default=str(DEFAULT_CLASSIFIED_PATH),
        help="Output JSONL path for classified cache",
    )
    classify.add_argument("--model", default="gemma2", help="Ollama model name")
    classify.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama API URL")
    classify.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout seconds")
    classify.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Loop interval seconds for periodic auto-classification (0: run once)",
    )

    report = subparsers.add_parser(
        "report",
        help="Generate report/todo from classified cache",
    )
    report.add_argument(
        "--classified-input",
        default=str(DEFAULT_CLASSIFIED_PATH),
        help="Classified cache JSONL path",
    )
    report.add_argument(
        "--reports-dir",
        default=str(DEFAULT_REPORT_DIR),
        help="Directory to save generated report files",
    )
    report.add_argument("--mode", choices=["report", "todo"], default="report")
    report.add_argument("--format", choices=["md", "json", "both"], default="both")
    report.add_argument("--stdout", action="store_true", help="Only print output payload/body")
    report.add_argument("--llm", choices=["never", "auto", "always"], default="auto")
    report.add_argument(
        "--llm-threshold",
        type=int,
        default=60,
        help="Auto mode: use LLM when classified entries >= threshold",
    )
    report.add_argument("--model", default="gemma2", help="Ollama model name")
    report.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama API URL")
    report.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout seconds")
    add_period_args(report)
    return parser.parse_args(argv[1:])


def add_period_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--period", choices=["today", "week", "range"], default="today")
    parser.add_argument("--from-date", help="Range start date (YYYY-MM-DD) for --period=range")
    parser.add_argument(
        "--to-date",
        help="Range end date (YYYY-MM-DD, inclusive) for --period=range",
    )


def parse_day(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")


def resolve_period(args: argparse.Namespace) -> Period:
    now = datetime.now(timezone.utc)
    if args.period == "today":
        start = datetime.combine(now.date(), dt_time.min, tzinfo=timezone.utc)
        return Period(start=start, end=now)
    if args.period == "week":
        start = now - timedelta(days=7)
        return Period(start=start, end=now)

    if not args.from_date:
        raise ValueError("--from-date is required when --period=range")
    start_day = parse_day(args.from_date, "--from-date")
    end_day = parse_day(args.to_date, "--to-date") if args.to_date else start_day
    if end_day < start_day:
        raise ValueError("--to-date must be >= --from-date")
    start = datetime.combine(start_day, dt_time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), dt_time.min, tzinfo=timezone.utc)
    return Period(start=start, end=end)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                print(f"Skipping invalid JSONL line {line_number}", file=sys.stderr)
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def remove_browser_suffix(title: str) -> str:
    value = title.strip()
    for suffix in BROWSER_SUFFIXES:
        pattern = rf"\s*[-–—|:]\s*{re.escape(suffix)}\s*$"
        value = re.sub(pattern, "", value, flags=re.IGNORECASE).strip()
    return value


def normalize_project_key(record: dict[str, Any]) -> str:
    ctx = record.get("ctx") if isinstance(record.get("ctx"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}

    cwd = str(ctx.get("cwd", "")).strip()
    if cwd and cwd.lower() != "unknown":
        name = Path(cwd).name.strip()
        if name:
            return name

    page_title = str(ctx.get("page_title", "")).strip()
    if page_title and page_title.lower() != "unknown":
        cleaned = remove_browser_suffix(page_title)
        if cleaned:
            return cleaned

    win = str(ctx.get("win", "")).strip()
    if win and win.lower() != "unknown":
        cleaned = remove_browser_suffix(win)
        if cleaned:
            return cleaned

    hint = str(meta.get("project_hint", "")).strip()
    if hint and hint.lower() != "unknown":
        cleaned = remove_browser_suffix(hint)
        if cleaned:
            return cleaned

    return "unknown"


def call_ollama(*, url: str, model: str, prompt: str, timeout: float) -> str:
    try:
        response = requests.post(
            url,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(f"Ollama returned HTTP {response.status_code}: {response.text[:400]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Ollama returned non-JSON response") from exc

    text = payload.get("response")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama response field is empty")
    return text


def strip_code_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", value)
        value = re.sub(r"\n?```$", "", value)
    return value.strip()


def record_id(record: dict[str, Any]) -> str:
    payload = {
        "t": record.get("t"),
        "raw": record.get("raw"),
        "ctx": record.get("ctx"),
        "meta": record.get("meta"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def load_existing_ids(classified_path: Path) -> set[str]:
    if not classified_path.exists():
        return set()
    ids: set[str] = set()
    for item in load_jsonl(classified_path):
        value = item.get("record_id")
        if isinstance(value, str) and value:
            ids.add(value)
    return ids


def build_classify_prompt(record: dict[str, Any], project: str) -> str:
    ctx = record.get("ctx") if isinstance(record.get("ctx"), dict) else {}
    schema = (
        '{"summary":"...",'
        '"done":["..."],'
        '"todos":[{"task":"...","priority":1,"context":"..."}],'
        '"tags":["..."]}'
    )
    return (
        "Classify this single development log into factual progress and actionable todos. "
        "Ignore emotional noise and return strict JSON only.\n"
        f"Project: {project}\n"
        f"Expected JSON schema: {schema}\n"
        f"t: {record.get('t', 'unknown')}\n"
        f"raw: {record.get('raw', '')}\n"
        f"cwd: {ctx.get('cwd', 'unknown')}\n"
        f"page_title: {ctx.get('page_title', 'unknown')}\n"
        f"win: {ctx.get('win', 'unknown')}\n"
    )


def parse_classification_json(text: str) -> dict[str, Any]:
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"summary": cleaned, "done": [], "todos": [], "tags": []}
    if not isinstance(data, dict):
        return {"summary": cleaned, "done": [], "todos": [], "tags": []}

    summary = str(data.get("summary", "")).strip()
    done_raw = data.get("done") if isinstance(data.get("done"), list) else []
    tags_raw = data.get("tags") if isinstance(data.get("tags"), list) else []
    done = [str(item).strip() for item in done_raw if str(item).strip()]
    tags = [str(item).strip() for item in tags_raw if str(item).strip()]

    todos_raw = data.get("todos") if isinstance(data.get("todos"), list) else []
    todos: list[dict[str, Any]] = []
    for item in todos_raw:
        if isinstance(item, dict):
            task = str(item.get("task", "")).strip()
            if not task:
                continue
            todo_item: dict[str, Any] = {"task": task}
            priority = item.get("priority")
            if isinstance(priority, int) and 1 <= priority <= 5:
                todo_item["priority"] = priority
            context = str(item.get("context", "")).strip()
            if context:
                todo_item["context"] = context
            todos.append(todo_item)
        else:
            task = str(item).strip()
            if task:
                todos.append({"task": task})

    return {"summary": summary, "done": done, "todos": todos, "tags": tags}


def classify_once(args: argparse.Namespace) -> tuple[int, int, int]:
    input_path = Path(args.input).expanduser()
    output_path = Path(args.classified_output).expanduser()

    records = load_jsonl(input_path)
    existing_ids = load_existing_ids(output_path)

    new_rows: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()
    processed = 0
    skipped = 0
    failed = 0

    for record in records:
        rid = record_id(record)
        if rid in existing_ids or rid in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(rid)
        project = normalize_project_key(record)
        prompt = build_classify_prompt(record, project)
        try:
            response_text = call_ollama(
                url=args.ollama_url,
                model=args.model,
                prompt=prompt,
                timeout=args.timeout,
            )
        except RuntimeError as exc:
            print(f"classification failed for record {rid}: {exc}", file=sys.stderr)
            failed += 1
            continue
        classification = parse_classification_json(response_text)
        new_rows.append(
            {
                "record_id": rid,
                "source_t": record.get("t"),
                "project": project,
                "raw": record.get("raw", ""),
                "ctx": record.get("ctx", {}),
                "classification": classification,
                "classified_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        processed += 1

    append_jsonl(output_path, new_rows)
    return processed, skipped, failed


def run_classify_command(args: argparse.Namespace) -> int:
    if args.interval < 0:
        print("--interval must be >= 0", file=sys.stderr)
        return 2

    if args.interval == 0:
        try:
            processed, skipped, failed = classify_once(args)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"classify done: processed={processed}, skipped={skipped}, failed={failed}",
            file=sys.stderr,
        )
        return 0 if failed == 0 else 3

    while True:
        try:
            processed, skipped, failed = classify_once(args)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"classify tick: processed={processed}, skipped={skipped}, failed={failed}",
            file=sys.stderr,
        )
        time.sleep(args.interval)


def filter_period(
    rows: list[dict[str, Any]],
    period: Period,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        source_t = parse_timestamp(row.get("source_t"))
        if source_t is None:
            continue
        if period.start <= source_t < period.end:
            filtered.append(row)
    return filtered


def make_static_analysis(mode: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if mode == "todo":
        dedup: dict[str, dict[str, Any]] = {}
        for row in rows:
            cls = row.get("classification") if isinstance(row.get("classification"), dict) else {}
            todos = cls.get("todos") if isinstance(cls.get("todos"), list) else []
            for item in todos:
                if not isinstance(item, dict):
                    continue
                task = str(item.get("task", "")).strip()
                if not task:
                    continue
                key = task.lower()
                if key not in dedup:
                    dedup[key] = {"task": task}
                priority = item.get("priority")
                if isinstance(priority, int) and 1 <= priority <= 5:
                    current = dedup[key].get("priority")
                    if not isinstance(current, int) or priority < current:
                        dedup[key]["priority"] = priority
                context = str(item.get("context", "")).strip()
                if context and "context" not in dedup[key]:
                    dedup[key]["context"] = context
        todos = list(dedup.values())
        todos.sort(key=lambda x: (x.get("priority", 9), x["task"]))
        return {"todos": todos}

    done_set: set[str] = set()
    next_set: set[str] = set()
    for row in rows:
        cls = row.get("classification") if isinstance(row.get("classification"), dict) else {}
        done_items = cls.get("done") if isinstance(cls.get("done"), list) else []
        for item in done_items:
            text = str(item).strip()
            if text:
                done_set.add(text)
        todos = cls.get("todos") if isinstance(cls.get("todos"), list) else []
        for item in todos:
            if isinstance(item, dict):
                text = str(item.get("task", "")).strip()
            else:
                text = str(item).strip()
            if text:
                next_set.add(text)
    return {
        "done": sorted(done_set),
        "next_actions": sorted(next_set),
        "risks": [],
    }


def build_report_llm_prompt(mode: str, project: str, static_analysis: dict[str, Any]) -> str:
    if mode == "todo":
        schema = '{"todos":[{"task":"...","priority":1,"context":"..."}]}'
        return (
            "Refine this pre-aggregated todo list into a concise, deduplicated actionable list. "
            "Return strict JSON only.\n"
            f"Project: {project}\n"
            f"Expected JSON schema: {schema}\n"
            f"Input JSON: {json.dumps(static_analysis, ensure_ascii=False)}"
        )
    schema = '{"done":["..."],"next_actions":["..."],"risks":["..."]}'
    return (
        "Refine this pre-aggregated progress summary into clean professional status bullets. "
        "Return strict JSON only.\n"
        f"Project: {project}\n"
        f"Expected JSON schema: {schema}\n"
        f"Input JSON: {json.dumps(static_analysis, ensure_ascii=False)}"
    )


def parse_report_json(mode: str, text: str) -> dict[str, Any]:
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"todos": []} if mode == "todo" else {"done": [], "next_actions": [], "risks": []}
    if not isinstance(data, dict):
        return {"todos": []} if mode == "todo" else {"done": [], "next_actions": [], "risks": []}

    if mode == "todo":
        todos = data.get("todos")
        if isinstance(todos, list):
            return {"todos": todos}
        return {"todos": []}
    done = data.get("done") if isinstance(data.get("done"), list) else []
    next_actions = data.get("next_actions") if isinstance(data.get("next_actions"), list) else []
    risks = data.get("risks") if isinstance(data.get("risks"), list) else []
    return {"done": done, "next_actions": next_actions, "risks": risks}


def render_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Core-Stream {payload['mode']} report")
    lines.append("")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- period: {payload['period']['from']} to {payload['period']['to']}")
    lines.append(f"- llm_strategy: {payload['llm_strategy']}")
    lines.append("")
    for project in payload["projects"]:
        lines.append(f"## {project['project']}")
        analysis = project["analysis"]
        if payload["mode"] == "todo":
            lines.append("### Todo")
            todos = analysis.get("todos", [])
            if todos:
                for item in todos:
                    if isinstance(item, dict):
                        task = str(item.get("task", "")).strip()
                        if not task:
                            continue
                        suffix_parts: list[str] = []
                        priority = item.get("priority")
                        if isinstance(priority, int):
                            suffix_parts.append(f"P{priority}")
                        context = str(item.get("context", "")).strip()
                        if context:
                            suffix_parts.append(context)
                        suffix = f" ({' / '.join(suffix_parts)})" if suffix_parts else ""
                        lines.append(f"- [ ] {task}{suffix}")
                    else:
                        text = str(item).strip()
                        if text:
                            lines.append(f"- [ ] {text}")
            else:
                lines.append("- [ ] (none)")
            lines.append("")
            continue

        lines.append("### Done")
        done = analysis.get("done", [])
        if done:
            lines.extend(f"- {item}" for item in done if str(item).strip())
        else:
            lines.append("- (none)")
        lines.append("")

        lines.append("### Next Action")
        next_actions = analysis.get("next_actions", [])
        if next_actions:
            lines.extend(f"- {item}" for item in next_actions if str(item).strip())
        else:
            lines.append("- (none)")
        lines.append("")

        lines.append("### Risks")
        risks = analysis.get("risks", [])
        if risks:
            lines.extend(f"- {item}" for item in risks if str(item).strip())
        else:
            lines.append("- (none)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_outputs(
    *,
    reports_dir: Path,
    mode: str,
    output_format: str,
    markdown_text: str,
    json_payload: dict[str, Any],
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}_{mode}"
    if output_format in {"md", "both"}:
        (reports_dir / f"{stem}.md").write_text(markdown_text, encoding="utf-8")
    if output_format in {"json", "both"}:
        (reports_dir / f"{stem}.json").write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def run_report_command(args: argparse.Namespace) -> int:
    try:
        period = resolve_period(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    classified_path = Path(args.classified_input).expanduser()
    reports_dir = Path(args.reports_dir).expanduser()
    try:
        rows = load_jsonl(classified_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    filtered = filter_period(rows, period)
    if not filtered:
        print("No classified entries found in the selected period.", file=sys.stderr)
        return 1

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in filtered:
        project = str(row.get("project", "unknown")).strip() or "unknown"
        grouped[project].append(row)

    llm_strategy = args.llm
    total_entries = len(filtered)
    use_llm = llm_strategy == "always" or (
        llm_strategy == "auto" and total_entries >= args.llm_threshold
    )

    projects_payload: list[dict[str, Any]] = []
    for project in sorted(grouped.keys()):
        project_rows = grouped[project]
        static_analysis = make_static_analysis(args.mode, project_rows)
        analysis = static_analysis
        if use_llm:
            prompt = build_report_llm_prompt(args.mode, project, static_analysis)
            try:
                response = call_ollama(
                    url=args.ollama_url,
                    model=args.model,
                    prompt=prompt,
                    timeout=args.timeout,
                )
                analysis = parse_report_json(args.mode, response)
            except RuntimeError as exc:
                print(f"LLM refine failed for project={project}: {exc}", file=sys.stderr)
                analysis = static_analysis
        projects_payload.append(
            {
                "project": project,
                "entry_count": len(project_rows),
                "analysis": analysis,
                "source": "llm" if (use_llm and analysis is not static_analysis) else "static",
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "period": {
            "type": args.period,
            "from": period.start.isoformat(),
            "to": period.end.isoformat(),
        },
        "classified_input": str(classified_path),
        "llm_strategy": llm_strategy,
        "used_llm": use_llm,
        "projects": projects_payload,
    }
    markdown = render_markdown(payload)
    save_outputs(
        reports_dir=reports_dir,
        mode=args.mode,
        output_format=args.format,
        markdown_text=markdown,
        json_payload=payload,
    )

    if args.format == "md":
        sys.stdout.write(markdown)
    elif args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(markdown + "\n---\n\n")
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def main() -> int:
    args = parse_args(sys.argv)
    if args.command == "classify":
        return run_classify_command(args)
    if args.command == "report":
        return run_report_command(args)
    print("Unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
