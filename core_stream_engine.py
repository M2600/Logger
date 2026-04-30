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
DEFAULT_TASKS_PATH = Path.home() / ".logger" / "tasks.jsonl"
DEFAULT_REPORT_DIR = Path.home() / ".logger" / "reports"
DEFAULT_SCREENSHOT_DIR = Path.home() / ".logger" / "screenshots"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_VECTORS_PATH = Path.home() / ".logger" / "vectors.jsonl"
DEFAULT_EMBED_URL = "http://localhost:11434/api/embeddings"
DEFAULT_EMBED_MODEL = "nomic-embed-text"

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
    
    # Rebuild: keep entries where latest job status is "done",
    # or where there is no job at all (manually written records, e.g. done events).
    valid_entries = []
    for c in classified_rows:
        event_id = c.get("event_id")
        if not event_id:
            continue
        status = latest_status.get(event_id)
        if status is None or status == "done":
            valid_entries.append(c)
    
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


def call_ollama_embed(*, text: str, model: str, url: str, timeout: float) -> list[float]:
    try:
        response = requests.post(
            url,
            json={"model": model, "prompt": text},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama embed request failed: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"Ollama embed HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Ollama embed returned non-JSON response") from exc
    vector = payload.get("embedding")
    if not isinstance(vector, list):
        raise RuntimeError("Ollama embed response has no embedding field")
    return vector


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
    done = [str(item).strip() for item in data.get("done", []) if str(item).strip() and not is_placeholder(str(item).strip())]
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


RECENT_CONTEXT_MAX_EVENTS = 8
RECENT_CONTEXT_MAX_HOURS = 3.0


def _fmt_time_short(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M")


def get_recent_context(
    events_path: Path,
    classified_path: Path,
    before_time: datetime,
    max_count: int = RECENT_CONTEXT_MAX_EVENTS,
    max_hours: float = RECENT_CONTEXT_MAX_HOURS,
) -> list[dict[str, Any]]:
    """Return recent events (before_time, within max_hours) condensed for LLM context."""
    cutoff = before_time.astimezone(timezone.utc) - timedelta(hours=max_hours)
    before_utc = before_time.astimezone(timezone.utc)

    classified_map: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(classified_path):
        eid = str(row.get("event_id", "")).strip()
        if eid:
            classified_map[eid] = row

    result: list[dict[str, Any]] = []
    for ev in load_jsonl(events_path):
        ts = parse_timestamp(ev.get("created_at"))
        if ts is None or ts < cutoff or ts >= before_utc:
            continue
        classified = classified_map.get(str(ev.get("id", "")))
        cls = classified.get("classification", {}) if classified else {}
        result.append({
            "created_at": _fmt_time_short(ts),
            "body": str(ev.get("body", ""))[:100],
            "project": str(classified.get("project", "?")) if classified else "?",
            "summary": str(cls.get("summary", ""))[:80] if cls else "",
        })

    return result[-max_count:]


def build_classify_prompt(
    event: dict[str, Any],
    known_projects: list[str],
    recent_context: list[dict[str, Any]] | None = None,
) -> str:
    ctx = event.get("context") if isinstance(event.get("context"), dict) else {}
    source = str(event.get("source", "cli")).strip().lower()

    schema = (
        '{"project":null or "project-name",'
        '"summary":"...",'
        '"done":["..."],'
        '"todos":[{"task":"...","priority":1,"context":"..."}],'
        '"tags":["..."]}'
    )
    known_list = ", ".join(f'"{p}"' for p in known_projects) if known_projects else "none"

    lines: list[str] = [
        "Classify this development log entry into project, progress, and actionable todos.",
        "",
    ]

    # Recent activity for session continuity
    if recent_context:
        lines.append("## Recent Activity (use for session context)")
        for item in recent_context:
            project_label = f"[{item['project']}] " if item["project"] != "?" else ""
            summary_part = f" — {item['summary']}" if item["summary"] else ""
            lines.append(f"  {item['created_at']}: \"{item['body']}\"{' → ' + project_label + summary_part if project_label or summary_part else ''}")
        lines.append("")

    # Source-specific guidance on which context signals to trust
    if source == "gui":
        lines += [
            "## Input Mode: GUI",
            "Triggered via keyboard shortcut / GUI launcher.",
            "Signal reliability: git_repo (high) > active_window / page_title (high) > cwd (NOT reliable — ignore)",
            "",
        ]
    elif source == "stdin":
        lines += [
            "## Input Mode: stdin pipe",
            "Content piped in (e.g. git diff | log.py).",
            "Signal reliability: git_repo (high) > cwd (high) > active_window (low)",
            "",
        ]
    elif source == "git":
        lines += [
            "## Input Mode: git hook",
            "Auto-generated by a git commit hook.",
            "Signal reliability: git_repo / branch / commit (authoritative)",
            "",
        ]
    else:
        lines += [
            "## Input Mode: CLI",
            "Typed directly in terminal.",
            "Signal reliability: git_repo (high) > cwd (high) > active_window (medium)",
            "",
        ]

    lines += [
        "## Log Entry",
        f"body: {event.get('body', '')}",
        "",
        "## Context Signals",
    ]

    git_repo = ctx.get("git_repo", "unknown")
    if git_repo and git_repo != "unknown":
        lines.append(f"  git_repo: {git_repo}  [most reliable]")

    if source != "gui":
        cwd = ctx.get("cwd", "unknown")
        if cwd and cwd != "unknown":
            lines.append(f"  cwd: {cwd}")

    win = ctx.get("win", "unknown")
    if win and win != "unknown":
        reliability = "reliable in GUI mode" if source == "gui" else ""
        suffix = f"  [{reliability}]" if reliability else ""
        lines.append(f"  active_window: {win}{suffix}")

    page_title = ctx.get("page_title", "unknown")
    if page_title and page_title != "unknown":
        reliability = "reliable in GUI mode" if source == "gui" else ""
        suffix = f"  [{reliability}]" if reliability else ""
        lines.append(f"  page_title: {page_title}{suffix}")

    lines += [
        "",
        "## Project Resolution — follow this priority order strictly",
        "",
        "1. BODY (highest priority)",
        "   The user's own words are the ground truth.",
        "   - Explicit project name in body (e.g. 'project2について', 'Logger のバグ')",
        "     → use that project. IGNORE git_repo/cwd even if they differ.",
        "   - Body clearly describes work that belongs to a specific project",
        "     → use that project.",
        "",
        "2. RECENT ACTIVITY (second priority)",
        "   If body does not explicitly name a project:",
        "   - If recent entries are consistently one project AND body is clearly a continuation",
        "     (same topic, follow-up, or references like 'さっきの' / 'これ' / 'また同じ')",
        "     → use that project.",
        "",
        "3. CONTEXT SIGNALS (tiebreaker only)",
        "   Use git_repo / cwd / window ONLY when body AND recent activity",
        "   leave the project genuinely ambiguous.",
        "   These signals show where the user IS — not necessarily what they are thinking about.",
        "",
        "Decision examples:",
        "  body: 'project2のバグを修正した' + git_repo: project1  →  project2  (body wins)",
        "  body: 'さっきの問題が再現した' + recent: [project2, project2]  →  project2  (continuity wins)",
        "  body: 'typoを直した' + git_repo: project1 + recent: []  →  project1  (context wins, body is vague)",
        "",
        f"Known existing projects: [{known_list}] — match spelling when a project name fits.",
        "Set project to null only if all three levels leave it genuinely unclear.",
        "",
        "## Output Rules",
        "- Write summary, done, todos, and context fields in natural Japanese.",
        "- Keep tags compact and machine-oriented.",
        f"- Return ONLY strict JSON: {schema}",
    ]

    return "\n".join(lines)


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
    events_path: Path = DEFAULT_EVENT_PATH,
) -> dict[str, Any]:
    known_projects = get_known_projects(classified_path)
    event_time = parse_timestamp(event.get("created_at")) or datetime.now().astimezone()
    recent_context = get_recent_context(
        events_path=events_path,
        classified_path=classified_path,
        before_time=event_time,
    )
    prompt = build_classify_prompt(event, known_projects, recent_context=recent_context)
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
            if text and not is_placeholder(text):
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
            "Refine this pre-aggregated todo list into a concise, deduplicated actionable list.\n"
            "Write task/context content in natural Japanese.\n"
            "Keep JSON keys and schema exactly as specified.\n"
            "Return strict JSON only.\n"
            f"Project: {project}\n"
            f"Expected JSON schema: {schema}\n"
            f"Input JSON: {json.dumps(static_analysis, ensure_ascii=False)}"
        )
    schema = '{"done":["..."],"next_actions":["..."],"risks":["..."]}'
    return (
        "Refine this pre-aggregated progress summary into clean professional status bullets.\n"
        "Write done/next_actions/risks content in natural Japanese.\n"
        "Keep JSON keys and schema exactly as specified.\n"
        "Return strict JSON only.\n"
        f"Project: {project}\n"
        f"Expected JSON schema: {schema}\n"
        f"Input JSON: {json.dumps(static_analysis, ensure_ascii=False)}"
    )


_PLACEHOLDER_VALUES = frozenset({
    # English
    "none", "null", "undefined", "n/a", "...",
    # Japanese
    "無", "無し", "なし", "特になし", "ない", "なし。", "特にない",
    "(なし)", "(無)", "（なし）", "（無）",
})


def is_placeholder(item: Any) -> bool:
    if not isinstance(item, str):
        return False
    return item.strip().lower() in _PLACEHOLDER_VALUES or item.strip() in _PLACEHOLDER_VALUES


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
            todos = analysis.get("todos", [])
            if not todos:
                continue
            lines.append("### Todo")
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
                    task_id = str(item.get("id", "")).strip()
                    id_suffix = f" (id: {task_id})" if task_id else ""
                    lines.append(f"- [ ] {task}{suffix}{id_suffix}")
                else:
                    text = str(item).strip()
                    if text:
                        lines.append(f"- [ ] {text}")
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
