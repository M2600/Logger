from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import requests

DEFAULT_EVENT_PATH = Path.home() / ".logger" / "events.jsonl"
DEFAULT_CLASSIFIED_PATH = Path.home() / ".logger" / "classified.jsonl"
DEFAULT_JOBS_PATH = Path.home() / ".logger" / "jobs.jsonl"
DEFAULT_REPORT_DIR = Path.home() / ".logger" / "reports"
DEFAULT_SCREENSHOT_DIR = Path.home() / ".logger" / "screenshots"
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


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()



def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def rebuild_classified_from_jobs(
    jobs_path: Path,
    events_path: Path,
    classified_path: Path,
) -> None:
    """Rebuild classified.jsonl from jobs and events, keeping only successful classifications.
    
    For each event, find the latest job status:
    - If "done": look for corresponding classified entry and preserve it
    - If "failed" or "pending" or "processing": skip (not yet classified)
    
    This ensures classified.jsonl never contains stale results from failed retries.
    """
    jobs = load_jsonl(jobs_path)
    events = load_jsonl(events_path)
    
    # Build event map: event_id -> event
    event_map = {e.get("id"): e for e in events if e.get("id")}
    
    # Find latest job status for each event
    latest_status: dict[str, str] = {}
    for job in jobs:
        event_id = str(job.get("event_id", "")).strip()
        if event_id:
            latest_status[event_id] = job.get("status", "")
    
    # Load current classified entries
    classified_rows = load_jsonl(classified_path)
    classified_map = {
        c.get("event_id"): c
        for c in classified_rows
        if c.get("event_id")
    }
    
    # Rebuild: keep only entries where latest status is "done"
    valid_entries = []
    for event_id, status in latest_status.items():
        if status == "done" and event_id in classified_map:
            valid_entries.append(classified_map[event_id])
    
    # Rewrite classified.jsonl with only valid entries
    classified_path.parent.mkdir(parents=True, exist_ok=True)
    with classified_path.open("w", encoding="utf-8") as f:
        for entry in valid_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


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


def remove_browser_suffix(title: str) -> str:
    value = title.strip()
    for suffix in BROWSER_SUFFIXES:
        pattern = rf"\s*[-–—|:]\s*{re.escape(suffix)}\s*$"
        value = re.sub(pattern, "", value, flags=re.IGNORECASE).strip()
    return value


def get_known_projects(classified_path: Path = DEFAULT_CLASSIFIED_PATH) -> list[str]:
    """Extract unique project names from classified.jsonl"""
    if not classified_path.exists():
        return []
    
    projects = set()
    try:
        for line in classified_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                project = str(record.get("project", "")).strip()
                if project and project.lower() not in ("unknown", "null"):
                    projects.add(project)
            except json.JSONDecodeError:
                continue
    except (IOError, OSError):
        pass
    
    return sorted(projects)


def normalize_project_key(event: dict[str, Any]) -> str:
    ctx = event.get("context") if isinstance(event.get("context"), dict) else {}
    meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}

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
        raise RuntimeError(f"Ollama returned HTTP {response.status_code}: {response.text[:300]}")
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


def parse_classification_json(text: str) -> dict[str, Any]:
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"project": None, "summary": cleaned, "done": [], "todos": [], "tags": []}
    if not isinstance(data, dict):
        return {"project": None, "summary": cleaned, "done": [], "todos": [], "tags": []}
    
    project = data.get("project")
    if project is not None:
        project = str(project).strip() if project else None
        if project and project.lower() in ("null", "none", "unknown"):
            project = None
    
    summary = str(data.get("summary", "")).strip()
    done = [str(item).strip() for item in data.get("done", []) if str(item).strip()]
    tags = [str(item).strip() for item in data.get("tags", []) if str(item).strip()]
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
            continue
        task = str(item).strip()
        if task:
            todos.append({"task": task})
    return {"project": project, "summary": summary, "done": done, "todos": todos, "tags": tags}


def build_classify_prompt(event: dict[str, Any], known_projects: list[str]) -> str:
    ctx = event.get("context") if isinstance(event.get("context"), dict) else {}
    
    schema = (
        '{"project":null or "project-name",'
        '"summary":"...",'
        '"done":["..."],'
        '"todos":[{"task":"...","priority":1,"context":"..."}],'
        '"tags":["..."]}'
    )
    
    known_list = ", ".join(f'"{p}"' for p in known_projects) if known_projects else "none"
    
    return (
        "Classify this development log into progress and actionable todos.\n"
        "Infer the project PRIMARILY from the log body content itself.\n"
        "Use context information (git_repo, window, page_title, cwd) as supplementary hints only.\n"
        "Git repository name is the most reliable context signal when available.\n"
        "Return null for project only if genuinely unclear from all available information.\n\n"
        f"Known projects: [{known_list}]\n"
        f"Expected JSON schema: {schema}\n\n"
        f"Log details:\n"
        f"  body: {event.get('body', '')}\n"
        f"  git_repo: {ctx.get('git_repo', 'unknown')}\n"
        f"  window: {ctx.get('win', 'unknown')}\n"
        f"  page_title: {ctx.get('page_title', 'unknown')}\n"
        f"  cwd: {ctx.get('cwd', 'unknown')}\n\n"
        "Return strict JSON only."
    )



def event_fingerprint(event: dict[str, Any]) -> str:
    event_id = str(event.get("id", "")).strip()
    if event_id:
        return event_id
    payload = {
        "created_at": event.get("created_at"),
        "body": event.get("body"),
        "context": event.get("context"),
        "meta": event.get("meta"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def classify_event(
    *,
    event: dict[str, Any],
    model: str,
    ollama_url: str,
    timeout: float,
    classified_path: Path = DEFAULT_CLASSIFIED_PATH,
) -> dict[str, Any]:
    known_projects = get_known_projects(classified_path)
    prompt = build_classify_prompt(event, known_projects)
    response_text = call_ollama(url=ollama_url, model=model, prompt=prompt, timeout=timeout)
    classification = parse_classification_json(response_text)
    
    # Use project from LLM classification, fallback to normalize_project_key if null
    project = classification.get("project")
    if not project:
        project = normalize_project_key(event)
    
    return {
        "record_id": event_fingerprint(event),
        "event_id": event.get("id", ""),
        "source_t": event.get("created_at"),
        "project": project,
        "body": event.get("body", ""),
        "context": event.get("context", {}),
        "classification": classification,
        "classified_at": now_iso(),
    }


def is_retriable_error(error_message: str) -> bool:
    """Check if an error is temporary/retriable (vs permanent)."""
    msg = str(error_message).lower()
    # Retriable: network timeouts, connection refused, transient ollama issues
    retriable_patterns = [
        "timeout",
        "connection refused",
        "connection reset",
        "broken pipe",
        "temporarily unavailable",
        "service unavailable",
        "too many requests",
        "read timed out",
    ]
    return any(pattern in msg for pattern in retriable_patterns)

def resolve_period(
    *,
    period: Literal["today", "week", "range"],
    from_date: str | None,
    to_date: str | None,
) -> Period:
    now = datetime.now().astimezone()
    if period == "today":
        start = datetime.combine(now.date(), dt_time.min).astimezone()
        return Period(start=start, end=now)
    if period == "week":
        return Period(start=now - timedelta(days=7), end=now)
    if not from_date:
        raise ValueError("--from-date is required when period=range")
    try:
        start_day = datetime.strptime(from_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--from-date must be YYYY-MM-DD") from exc
    if to_date:
        try:
            end_day = datetime.strptime(to_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("--to-date must be YYYY-MM-DD") from exc
    else:
        end_day = start_day
    if end_day < start_day:
        raise ValueError("--to-date must be >= --from-date")
    start = datetime.combine(start_day, dt_time.min).astimezone()
    end = datetime.combine(end_day + timedelta(days=1), dt_time.min).astimezone()
    return Period(start=start, end=end)



def filter_period(rows: list[dict[str, Any]], period: Period) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        source_t = parse_timestamp(row.get("source_t"))
        if source_t is None:
            continue
        if period.start <= source_t < period.end:
            out.append(row)
    return out


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
            text = str(item.get("task", "")).strip() if isinstance(item, dict) else str(item).strip()
            if text:
                next_set.add(text)
    return {"done": sorted(done_set), "next_actions": sorted(next_set), "risks": []}


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


def is_placeholder(item: Any) -> bool:
    """Check if item is a placeholder value (none, None, etc.)"""
    if not isinstance(item, str):
        return False
    normalized = str(item).strip().lower()
    return normalized in ("none", "null", "undefined", "n/a", "...")


def filter_placeholders(items: list[Any]) -> list[Any]:
    """Remove placeholder values from list"""
    return [item for item in items if not is_placeholder(item)]


def parse_report_json(mode: str, text: str) -> dict[str, Any]:
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"todos": []} if mode == "todo" else {"done": [], "next_actions": [], "risks": []}
    if not isinstance(data, dict):
        return {"todos": []} if mode == "todo" else {"done": [], "next_actions": [], "risks": []}
    if mode == "todo":
        todos = data.get("todos") if isinstance(data.get("todos"), list) else []
        return {"todos": filter_placeholders(todos)}
    done = data.get("done") if isinstance(data.get("done"), list) else []
    next_actions = data.get("next_actions") if isinstance(data.get("next_actions"), list) else []
    risks = data.get("risks") if isinstance(data.get("risks"), list) else []
    return {
        "done": filter_placeholders(done),
        "next_actions": filter_placeholders(next_actions),
        "risks": filter_placeholders(risks)
    }


def build_report_payload(
    *,
    rows: list[dict[str, Any]],
    mode: Literal["report", "todo"],
    period: Period,
    period_name: str,
    llm: Literal["never", "auto", "always"],
    llm_threshold: int,
    model: str,
    ollama_url: str,
    timeout: float,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        project = str(row.get("project", "unknown")).strip() or "unknown"
        grouped[project].append(row)

    total_entries = len(rows)
    use_llm = llm == "always" or (llm == "auto" and total_entries >= llm_threshold)
    projects_payload: list[dict[str, Any]] = []
    for project in sorted(grouped.keys()):
        project_rows = grouped[project]
        static_analysis = make_static_analysis(mode, project_rows)
        analysis = static_analysis
        if use_llm:
            prompt = build_report_llm_prompt(mode, project, static_analysis)
            response = call_ollama(url=ollama_url, model=model, prompt=prompt, timeout=timeout)
            analysis = parse_report_json(mode, response)
        projects_payload.append(
            {
                "project": project,
                "entry_count": len(project_rows),
                "analysis": analysis,
                "source": "llm" if use_llm else "static",
            }
        )

    return {
        "generated_at": now_iso(),
        "mode": mode,
        "period": {"type": period_name, "from": period.start.isoformat(), "to": period.end.isoformat()},
        "llm_strategy": llm,
        "used_llm": use_llm,
        "projects": projects_payload,
    }


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
        lines.extend(f"- {item}" for item in done if str(item).strip()) if done else lines.append("- (none)")
        lines.append("")
        lines.append("### Next Action")
        nxt = analysis.get("next_actions", [])
        lines.extend(f"- {item}" for item in nxt if str(item).strip()) if nxt else lines.append("- (none)")
        lines.append("")
        lines.append("### Risks")
        risks = analysis.get("risks", [])
        lines.extend(f"- {item}" for item in risks if str(item).strip()) if risks else lines.append("- (none)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_report_files(*, reports_dir: Path, mode: str, markdown: str, payload: dict[str, Any]) -> dict[str, str]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    md_path = reports_dir / f"{stamp}_{mode}.md"
    json_path = reports_dir / f"{stamp}_{mode}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path)}
