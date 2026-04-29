#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional


import uvicorn
from fastapi import FastAPI, HTTPException, Request, File, Form, UploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core_stream_engine import (
    DEFAULT_CLASSIFIED_PATH,
    DEFAULT_EVENT_PATH,
    DEFAULT_JOBS_PATH,
    DEFAULT_OLLAMA_URL,
    DEFAULT_REPORT_DIR,
    DEFAULT_SCREENSHOT_DIR,
    DEFAULT_TASKS_PATH,
    append_jsonl,
    build_report_payload,
    classify_event,
    event_fingerprint,
    filter_period,
    is_retriable_error,
    load_jsonl,
    now_iso,
    rebuild_classified_from_jobs,
    render_markdown,
    resolve_period,
    save_report_files,
)


class EventIn(BaseModel):
    type: str = "thought"
    body: str = ""
    source: str = "cli"
    context: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    screenshot_data: str | None = None  # Base64-encoded PNG image


class AiSettingsIn(BaseModel):
    enabled: bool


class ReportRequest(BaseModel):
    mode: Literal["report", "todo"] = "report"
    period: Literal["today", "week", "range"] = "today"
    from_date: str | None = None
    to_date: str | None = None
    llm: Literal["never", "auto", "always"] = "auto"
    llm_threshold: int = 60
    save: bool = True


class MarkTaskCompleteRequest(BaseModel):
    task_id: str
    note: str = ""


@dataclass
class Task:
    """Represents a task extracted from events"""
    id: str
    task_text: str
    extracted_at: str
    status: Literal["open", "completed"] = "open"
    completed_at: Optional[str] = None
    completed_event_id: Optional[str] = None
    note: str = ""
    completion_reason: Literal["manual", "auto"] = "manual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_text": self.task_text,
            "extracted_at": self.extracted_at,
            "status": self.status,
            "completed_at": self.completed_at,
            "completed_event_id": self.completed_event_id,
            "note": self.note,
            "completion_reason": self.completion_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            task_text=data["task_text"],
            extracted_at=data["extracted_at"],
            status=data.get("status", "open"),
            completed_at=data.get("completed_at"),
            completed_event_id=data.get("completed_event_id"),
            note=data.get("note", ""),
            completion_reason=data.get("completion_reason", "manual"),
        )


@dataclass
class RuntimeSettings:
    model: str
    ollama_url: str
    timeout: float
    ai_enabled: bool


@dataclass
class AuthConfig:
    """Global authentication configuration"""
    api_key: Optional[str] = None
    auth_enabled: bool = False
    
    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "AuthConfig":
        """Load auth config from CLI args or config file"""
        api_key = None
        
        # Priority: --api-key > --config-file
        if args.api_key:
            api_key = args.api_key
        elif args.config_file:
            api_key = cls._load_config_file(args.config_file)
        
        return cls(
            api_key=api_key,
            auth_enabled=api_key is not None
        )
    
    @staticmethod
    def _load_config_file(config_path: str) -> Optional[str]:
        """Load API key from JSON config file"""
        try:
            path = Path(config_path).expanduser()
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
                    return data.get("api_key")
        except Exception:
            pass
        return None


class DaemonState:
    def __init__(
        self,
        *,
        events_path: Path,
        classified_path: Path,
        jobs_path: Path,
        tasks_path: Path,
        reports_dir: Path,
        screenshot_dir: Path,
        settings: RuntimeSettings,
    ) -> None:
        self.events_path = events_path
        self.classified_path = classified_path
        self.jobs_path = jobs_path
        self.tasks_path = tasks_path
        self.reports_dir = reports_dir
        self.screenshot_dir = screenshot_dir
        self.settings = settings
        self.lock = threading.Lock()
        self.analysis_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.retry_queue: queue.Queue[tuple[dict[str, Any], int]] = queue.Queue()  # (event, retry_count)
        self.resume_queued_on_startup = 0
        self.classified_ids = {
            str(item.get("record_id", "")).strip()
            for item in load_jsonl(self.classified_path)
            if str(item.get("record_id", "")).strip()
        }

    def enqueue_job(self, event: dict[str, Any], status: str, error: str = "") -> None:
        payload = {
            "id": str(uuid.uuid4()),
            "event_id": event.get("id", ""),
            "status": status,
            "priority": "high" if event.get("type") == "thought" else "low",
            "model": self.settings.model,
            "error": error,
            "created_at": now_iso(),
        }
        append_jsonl(self.jobs_path, payload)

    def load_tasks(self) -> dict[str, Task]:
        """Load all tasks from tasks.jsonl"""
        tasks = {}
        for item in load_jsonl(self.tasks_path):
            try:
                task = Task.from_dict(item)
                tasks[task.id] = task
            except Exception:
                pass
        return tasks

    def save_task(self, task: Task) -> None:
        """Append task to tasks.jsonl"""
        append_jsonl(self.tasks_path, task.to_dict())

    def update_task(self, task_id: str, status: str, completed_at: Optional[str] = None, 
                   completed_event_id: Optional[str] = None, note: str = "", 
                   completion_reason: str = "manual") -> Optional[Task]:
        """Update task status by rewriting entire tasks.jsonl"""
        tasks = self.load_tasks()
        if task_id not in tasks:
            return None
        
        task = tasks[task_id]
        task.status = status
        if completed_at:
            task.completed_at = completed_at
        if completed_event_id:
            task.completed_event_id = completed_event_id
        if note:
            task.note = note
        task.completion_reason = completion_reason
        
        # Rewrite tasks.jsonl with updated task
        with self.tasks_path.open("w", encoding="utf-8") as f:
            for t in tasks.values():
                if t.id == task_id:
                    f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
                else:
                    f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
        
        return task

    def resolve_task_id_prefix(self, task_id_or_prefix: str) -> tuple[Optional[str], list[Task]]:
        """Resolve exact ID or unique prefix to a single task ID.

        Returns:
            (resolved_id, candidates)
            - resolved_id: matched task ID when unique, else None
            - candidates: ambiguous candidates when multiple matches, else []
        """
        query = task_id_or_prefix.strip()
        if not query:
            return None, []

        tasks = self.load_tasks()
        if query in tasks:
            return query, []

        matches = [tid for tid in tasks.keys() if tid.startswith(query)]
        if not matches:
            return None, []
        if len(matches) == 1:
            return matches[0], []

        # If only one open task matches, prefer it.
        open_matches = [tid for tid in matches if tasks[tid].status == "open"]
        if len(open_matches) == 1:
            return open_matches[0], []

        candidate_ids = open_matches if open_matches else matches
        candidates = [tasks[tid] for tid in candidate_ids]
        return None, candidates

    def auto_complete_tasks(self, event: dict[str, Any]) -> list[str]:
        """Auto-detect and mark completed tasks based on event. Returns list of completed task IDs."""
        tasks = self.load_tasks()
        open_tasks = {tid: t for tid, t in tasks.items() if t.status == "open"}
        if not open_tasks:
            return []
        
        event_body = str(event.get("body", "")).strip()
        if not event_body:
            return []
        
        # Build task list for LLM
        task_list = "\n".join([f"{i}. {t.task_text}" for i, t in enumerate(open_tasks.values())])
        
        # Create prompt for LLM to check if any tasks are completed
        prompt = (
            "I have the following list of open tasks:\n"
            f"{task_list}\n\n"
            "A new event just occurred:\n"
            f'"{event_body}"\n\n'
            "Analyze this new event and determine which (if any) of the open tasks have been completed or resolved by this event.\n"
            "Return ONLY a valid JSON response (no markdown, no code blocks) with this exact format:\n"
            '{"completed_task_indices": [0, 2], "confidence": 0.95, "reason": "日本語での簡潔な理由"}\n'
            "Guidelines:\n"
            "- completed_task_indices: array of indices (0-based) of completed tasks, or empty array if none\n"
            "- confidence: float between 0 and 1 representing your confidence level\n"
            "- reason: brief explanation in Japanese of why these tasks are marked complete\n"
            "- Only include task indices where you have HIGH confidence (>0.8) that the task is actually completed.\n"
            "- Return ONLY the JSON object, no additional text, no code blocks, no markdown."
        )
        
        try:
            import requests as req_lib
            response = req_lib.post(
                self.settings.ollama_url,
                json={"model": self.settings.model, "prompt": prompt, "stream": False},
                timeout=self.settings.timeout,
            )
            if response.status_code != 200:
                print(f"[DEBUG] Ollama API error: {response.status_code}")
                return []
            
            result = response.json()
            response_text = str(result.get("response", "")).strip()
            
            # Clean up response - remove markdown code blocks if present
            if response_text.startswith("```"):
                response_text = response_text.strip()
                # Remove ```json or ``` prefix and ``` suffix
                response_text = response_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            
            # Parse JSON response
            import json as json_lib
            try:
                parsed = json_lib.loads(response_text)
                completed_indices = parsed.get("completed_task_indices", [])
                confidence = parsed.get("confidence", 0.0)
                reason = parsed.get("reason", "")
                
                # Only process if confidence is high enough
                if not isinstance(completed_indices, list):
                    return []
                
                task_list_items = list(open_tasks.items())
                completed_ids = []
                for idx in completed_indices:
                    if isinstance(idx, int) and 0 <= idx < len(task_list_items):
                        if confidence > 0.8:
                            task_id, task = task_list_items[idx]
                            self.update_task(
                                task_id=task_id,
                                status="completed",
                                completed_at=now_iso(),
                                completed_event_id=event.get("id", ""),
                                note=reason,
                                completion_reason="auto",
                            )
                            completed_ids.append(task_id)
                            event_snippet = event_body[:80] + ("..." if len(event_body) > 80 else "")
                            print(
                                f"[AUTO-COMPLETE] タスク完了: {task.task_text!r}"
                                f" | id={task_id[:8]}"
                                f" | confidence={confidence:.2f}"
                                f" | reason={reason!r}"
                                f" | event={event_snippet!r}"
                            )

                return completed_ids
            except (json_lib.JSONDecodeError, ValueError, TypeError) as e:
                print(f"[DEBUG] JSON parse error: {e}, response_text: {response_text[:200]}")
                return []
        except Exception as e:
            print(f"[DEBUG] Auto-complete error: {e}")
            return []

def start_worker(state: DaemonState) -> threading.Thread:
    def _run() -> None:
        while True:
            event = state.analysis_queue.get()
            try:
                state.enqueue_job(event, "processing")
                row = classify_event(
                    event=event,
                    model=state.settings.model,
                    ollama_url=state.settings.ollama_url,
                    timeout=state.settings.timeout,
                    classified_path=state.classified_path,
                    events_path=state.events_path,
                )
                rid = event_fingerprint(event)
                with state.lock:
                    if rid in state.classified_ids:
                        state.enqueue_job(event, "done")
                        continue
                    append_jsonl(state.classified_path, row)
                    state.classified_ids.add(rid)
                state.enqueue_job(event, "done")
            except Exception as exc:
                error_msg = str(exc)
                state.enqueue_job(event, "failed", error_msg)
                # Auto-retry if error is temporary (e.g., Ollama timeout)
                if is_retriable_error(error_msg):
                    # Schedule retry with exponential backoff (max 3 retries)
                    retry_count = getattr(event, '_retry_count', 0)
                    if retry_count < 3:
                        delay = 5 * (2 ** retry_count)  # 5s, 10s, 20s
                        state.retry_queue.put((event, retry_count + 1, delay))
            finally:
                state.analysis_queue.task_done()

    thread = threading.Thread(target=_run, name="core-stream-worker", daemon=True)
    thread.start()
    return thread


def start_retry_manager(state: DaemonState) -> threading.Thread:
    """Monitor retry_queue and re-queue events after delay."""
    def _run() -> None:
        pending_retries: dict[str, tuple[float, dict[str, Any], int]] = {}  # event_id -> (retry_time, event, count)
        
        while True:
            # Check if any retries are ready
            now = time.time()
            ready_to_retry = [
                eid for eid, (retry_time, _, _) in pending_retries.items()
                if now >= retry_time
            ]
            for eid in ready_to_retry:
                _, event, retry_count = pending_retries.pop(eid)
                event_marker = f"{event.get('id', 'unknown')[:8]} (retry {retry_count}/3)"
                state.analysis_queue.put(event)
            
            # Collect new retries
            try:
                while True:
                    event, retry_count, delay = state.retry_queue.get_nowait()
                    eid = event.get('id', '')
                    retry_time = time.time() + delay
                    pending_retries[eid] = (retry_time, event, retry_count)
            except queue.Empty:
                pass
            
            time.sleep(0.1)  # Check frequently for ready retries
    
    thread = threading.Thread(target=_run, name="core-stream-retry-manager", daemon=True)
    thread.start()
    return thread

def recent_failed_jobs(state: DaemonState, limit: int = 3) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for row in reversed(load_jsonl(state.jobs_path)):
        if str(row.get("status", "")).strip() != "failed":
            continue
        error = str(row.get("error", "")).strip()
        if not error:
            continue
        failures.append(
            {
                "event_id": str(row.get("event_id", "")).strip(),
                "created_at": str(row.get("created_at", "")).strip(),
                "error": error,
            }
        )
        if len(failures) >= limit:
            break
    return failures


def latest_job_status_counts(state: DaemonState) -> dict[str, int]:
    latest_by_event: dict[str, str] = {}
    for row in load_jsonl(state.jobs_path):
        event_id = str(row.get("event_id", "")).strip()
        status = str(row.get("status", "")).strip()
        if not event_id or not status:
            continue
        latest_by_event[event_id] = status
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "other": 0}
    for status in latest_by_event.values():
        if status in counts:
            counts[status] += 1
        else:
            counts["other"] += 1
    return counts


def count_unclassified_events(state: DaemonState) -> int:
    events = load_jsonl(state.events_path)
    with state.lock:
        classified_ids = set(state.classified_ids)
    unclassified = 0
    for event in events:
        if event_fingerprint(event) not in classified_ids:
            unclassified += 1
    return unclassified


def build_analysis_state(state: DaemonState) -> dict[str, int]:
    status_counts = latest_job_status_counts(state)
    queue_size = state.analysis_queue.qsize()
    unclassified_events = count_unclassified_events(state)
    return {
        "queue_size": queue_size,
        "pending_events": status_counts["pending"],
        "processing_events": status_counts["processing"],
        "inflight_events": status_counts["pending"] + status_counts["processing"],
        "failed_events": status_counts["failed"],
        "done_events": status_counts["done"],
        "unclassified_events": unclassified_events,
        "resumed_on_startup": state.resume_queued_on_startup,
    }


def enqueue_unclassified_events(state: DaemonState) -> int:
    if not state.settings.ai_enabled:
        return 0
    events = load_jsonl(state.events_path)
    with state.lock:
        classified_ids = set(state.classified_ids)
    queued = 0
    for event in events:
        rid = event_fingerprint(event)
        if rid in classified_ids:
            continue
        state.enqueue_job(event, "pending")
        state.analysis_queue.put(event)
        queued += 1
    return queued


def build_warnings(state: DaemonState) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    analysis_state = build_analysis_state(state)
    if not state.settings.ai_enabled:
        warnings.append(
            {
                "code": "ai_disabled",
                "message": "AI worker is disabled. New events are saved, but LLM classification is paused.",
                "action": "POST /settings/ai with {\"enabled\": true} to resume classification.",
            }
        )

    failures = recent_failed_jobs(state, limit=1)
    if failures:
        last = failures[0]
        message = "Recent classification failure detected."
        error_text = last.get("error", "")
        if "model" in error_text and "not found" in error_text:
            message = "Configured Ollama model is not available."
        warnings.append(
            {
                "code": "classification_failed",
                "message": message,
                "action": "Fix Ollama/model issue, then POST /analyze/backfill to retry pending events.",
            }
        )

    if analysis_state["unclassified_events"] > 0 and state.settings.ai_enabled:
        warnings.append(
            {
                "code": "no_classified_data",
                "message": (
                    "Some events are not reflected in reports yet "
                    f"({analysis_state['unclassified_events']} unclassified events)."
                ),
                "action": "Check /health last_analysis_error and run /analyze/backfill if needed.",
            }
        )

    if analysis_state["inflight_events"] > 0:
        warnings.append(
            {
                "code": "inference_in_progress",
                "message": (
                    "LLM classification is currently running "
                    f"(pending={analysis_state['pending_events']}, processing={analysis_state['processing_events']})."
                ),
                "action": "Wait for processing to finish before generating final report output.",
            }
        )

    if state.resume_queued_on_startup > 0:
        warnings.append(
            {
                "code": "resumed_unclassified_events",
                "message": (
                    "Daemon resumed pending classification from previous run "
                    f"({state.resume_queued_on_startup} events queued at startup)."
                ),
                "action": "Monitor /health analysis_state until inflight_events reaches 0.",
            }
        )

    if analysis_state["queue_size"] > 20:
        warnings.append(
            {
                "code": "analysis_backlog",
                "message": f"Analysis queue backlog is high ({analysis_state['queue_size']} jobs waiting).",
                "action": "Keep daemon running or reduce event rate until queue drains.",
            }
        )
    return warnings


def build_app(state: DaemonState, auth_config: AuthConfig) -> FastAPI:
    app = FastAPI(title="Core-Stream Daemon", version="1.0.0")

    # Add authentication middleware if enabled
    if auth_config.auth_enabled:
        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Skip auth for public endpoints
                if request.url.path in ["/", "/health", "/openapi.json", "/docs", "/redoc"]:
                    return await call_next(request)
                
                # Extract Bearer token from Authorization header
                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    return JSONResponse(
                        status_code=401,
                        content={"error": "Missing or invalid Authorization header"}
                    )
                
                token = auth_header[7:]  # Remove "Bearer " prefix
                if token != auth_config.api_key:
                    return JSONResponse(
                        status_code=403,
                        content={"error": "Invalid API key"}
                    )
                
                return await call_next(request)
        
        app.add_middleware(AuthMiddleware)

    @app.get("/")
    def index() -> FileResponse:
        """Serve web UI for event logging"""
        index_path = Path(__file__).parent / "index.html"
        if index_path.exists():
            return FileResponse(index_path, media_type="text/html")
        return {"message": "Core-Stream Daemon API. Use POST /events to log, GET /health for status"}

    @app.get("/health")
    def health() -> dict[str, Any]:
        failures = recent_failed_jobs(state, limit=1)
        analysis_state = build_analysis_state(state)
        return {
            "status": "ok",
            "auth_enabled": auth_config.auth_enabled,
            "ai_enabled": state.settings.ai_enabled,
            "queue_size": analysis_state["queue_size"],
            "analysis_state": analysis_state,
            "last_analysis_error": failures[0] if failures else None,
            "warnings": build_warnings(state),
        }

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return {
            "ai_enabled": state.settings.ai_enabled,
            "model": state.settings.model,
            "ollama_url": state.settings.ollama_url,
            "timeout": state.settings.timeout,
            "warnings": build_warnings(state),
        }

    @app.post("/settings/ai")
    def set_ai_settings(payload: AiSettingsIn) -> dict[str, Any]:
        state.settings.ai_enabled = payload.enabled
        return {"ai_enabled": state.settings.ai_enabled, "warnings": build_warnings(state)}

    @app.post("/events")
    def post_event(event_in: EventIn) -> dict[str, Any]:
        event = {
            "id": str(uuid.uuid4()),
            "type": event_in.type,
            "body": event_in.body,
            "source": event_in.source,
            "context": event_in.context,
            "meta": event_in.meta,
            "created_at": event_in.created_at or now_iso(),
        }
        
        # Handle screenshot if provided
        screenshot_path = ""
        if event_in.screenshot_data:
            try:
                state.screenshot_dir.mkdir(parents=True, exist_ok=True)
                stamp = event["created_at"].replace(":", "").replace("-", "").replace(".", "_")
                screenshot_path = state.screenshot_dir / f"{event['id']}_{stamp}.png"
                image_bytes = base64.b64decode(event_in.screenshot_data)
                screenshot_path.write_bytes(image_bytes)
                event["meta"]["screenshot_path"] = str(screenshot_path)
            except Exception as exc:
                event["meta"]["screenshot_error"] = str(exc)
        
        append_jsonl(state.events_path, event)
        
        # Auto-detect completed tasks in background (non-blocking)
        if state.settings.ai_enabled:
            def _check_tasks():
                state.auto_complete_tasks(event)
            threading.Thread(target=_check_tasks, daemon=True).start()
        
        if state.settings.ai_enabled:
            state.enqueue_job(event, "pending")
            state.analysis_queue.put(event)
        return {"status": "ok", "event_id": event["id"]}

    @app.post("/analyze/backfill")
    def analyze_backfill() -> dict[str, Any]:
        # Rebuild classified.jsonl to remove stale entries from failed retries
        rebuild_classified_from_jobs(
            jobs_path=state.jobs_path,
            events_path=state.events_path,
            classified_path=state.classified_path,
        )
        # Reload classified_ids cache from rebuilt file
        with state.lock:
            state.classified_ids = {
                str(item.get("record_id", "")).strip()
                for item in load_jsonl(state.classified_path)
                if str(item.get("record_id", "")).strip()
            }
        # Queue remaining unclassified events
        queued = enqueue_unclassified_events(state)
        return {"queued": queued, "warnings": build_warnings(state)}

    @app.post("/reports/generate")
    def generate_report(req: ReportRequest) -> dict[str, Any]:
        period = resolve_period(period=req.period, from_date=req.from_date, to_date=req.to_date)
        rows = filter_period(load_jsonl(state.classified_path), period)
        if not rows:
            failures = recent_failed_jobs(state, limit=3)
            detail: dict[str, Any] = {
                "message": "No classified entries found in selected period.",
                "warnings": build_warnings(state),
            }
            if failures:
                detail["recent_analysis_failures"] = failures
                detail["hint"] = "Fix analysis errors, then call /analyze/backfill to classify existing events."
            raise HTTPException(status_code=404, detail=detail)
        try:
            payload = build_report_payload(
                rows=rows,
                mode=req.mode,
                period=period,
                period_name=req.period,
                llm=req.llm,
                llm_threshold=req.llm_threshold,
                model=state.settings.model,
                ollama_url=state.settings.ollama_url,
                timeout=state.settings.timeout,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Reconcile report output with tasks.jsonl state.
        # - todo mode: inject task IDs and avoid recreating already completed tasks
        # - report mode: hide completed tasks from next_actions
        existing_tasks = state.load_tasks()
        open_tasks_by_text = {
            task.task_text.lower(): task for task in existing_tasks.values() if task.status == "open"
        }
        completed_task_texts = {
            task.task_text.lower() for task in existing_tasks.values() if task.status == "completed"
        }
        if req.mode == "todo":
            for project in payload.get("projects", []):
                if not isinstance(project, dict):
                    continue
                analysis = project.get("analysis")
                if not isinstance(analysis, dict):
                    continue
                todos = analysis.get("todos")
                if not isinstance(todos, list):
                    continue

                synced_todos: list[dict[str, Any]] = []
                for todo_item in todos:
                    if isinstance(todo_item, dict):
                        task_text = str(todo_item.get("task", "")).strip()
                        if not task_text:
                            continue
                        task_payload = dict(todo_item)
                    else:
                        task_text = str(todo_item).strip()
                        if not task_text:
                            continue
                        task_payload = {"task": task_text}

                    key = task_text.lower()
                    if key in completed_task_texts and key not in open_tasks_by_text:
                        # User already marked this task completed.
                        # Keep it out of "next" and do not recreate an open task.
                        continue
                    task = open_tasks_by_text.get(key)
                    if task is None:
                        task = Task(
                            id=str(uuid.uuid4()),
                            task_text=task_text,
                            extracted_at=now_iso(),
                            status="open",
                        )
                        state.save_task(task)
                        open_tasks_by_text[key] = task

                    task_payload["id"] = task.id
                    synced_todos.append(task_payload)

                analysis["todos"] = synced_todos
        elif req.mode == "report":
            for project in payload.get("projects", []):
                if not isinstance(project, dict):
                    continue
                analysis = project.get("analysis")
                if not isinstance(analysis, dict):
                    continue
                next_actions = analysis.get("next_actions")
                if not isinstance(next_actions, list):
                    continue
                filtered_next_actions: list[str] = []
                for item in next_actions:
                    text = str(item).strip()
                    if not text:
                        continue
                    key = text.lower()
                    if key in completed_task_texts and key not in open_tasks_by_text:
                        continue
                    filtered_next_actions.append(text)
                analysis["next_actions"] = filtered_next_actions

        markdown = render_markdown(payload)
        files: dict[str, str] = {}
        if req.save:
            files = save_report_files(
                reports_dir=state.reports_dir,
                mode=req.mode,
                markdown=markdown,
                payload=payload,
            )
        
        return {"payload": payload, "markdown": markdown, "files": files, "warnings": build_warnings(state)}

    @app.post("/tasks/mark-complete")
    def mark_task_complete(req: MarkTaskCompleteRequest) -> dict[str, Any]:
        resolved_task_id, candidates = state.resolve_task_id_prefix(req.task_id)
        if candidates:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Task ID prefix is ambiguous",
                    "prefix": req.task_id,
                    "candidates": [
                        {"id": t.id, "task_text": t.task_text, "status": t.status}
                        for t in candidates
                    ],
                },
            )
        if not resolved_task_id:
            raise HTTPException(status_code=404, detail=f"Task {req.task_id} not found")

        task = state.update_task(
            task_id=resolved_task_id,
            status="completed",
            completed_at=now_iso(),
            note=req.note,
            completion_reason="manual",
        )
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {req.task_id} not found")
        return {
            "task": task.to_dict(),
            "resolved_task_id": resolved_task_id,
            "warnings": build_warnings(state),
        }

    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Core-Stream daemon API server",
        epilog=(
            "QUICK START:\n"
            "  Local (no auth):        python daemon.py\n"
            "  Remote (with auth):     python daemon.py --api-key secret-key\n"
            "  Custom model:           python daemon.py --model mistral\n"
            "  From config file:       python daemon.py --config-file ~/.logger/daemon.json\n"
            "\n"
            "REQUIREMENTS:\n"
            "  Python 3.9+\n"
            "  Ollama running:         ollama serve (in another terminal)\n"
            "  Default model:          gemma2 (or install with: ollama pull gemma2)\n"
            "\n"
            "MAIN OPTIONS:\n"
            "  --host HOST             Bind address (default: 127.0.0.1)\n"
            "  --port PORT             Bind port (default: 8765)\n"
            "  --model MODEL           Ollama model name (gemma2, mistral, llama2, etc.)\n"
            "  --config-file FILE      Load all settings from JSON file\n"
            "  --ai-enabled            Enable AI classification (default)\n"
            "  --ai-disabled           Disable AI (process events without LLM)\n"
            "  --api-key KEY           Require API key for all requests\n"
            "\n"
            "STORAGE OPTIONS:\n"
            "  --events-path PATH      JSONL event store (~/.logger/events.jsonl)\n"
            "  --classified-path PATH  JSONL classified cache (~/.logger/classified.jsonl)\n"
            "  --tasks-path PATH       JSONL tasks store (~/.logger/tasks.jsonl)\n"
            "  --reports-dir DIR       Report output directory (~/.logger/reports)\n"
            "  --screenshot-dir DIR    Screenshot storage directory (~/.logger/screenshots)\n"
            "\n"
            "API ENDPOINTS:\n"
            "  GET /health             Check daemon status\n"
            "  GET /settings           Get current settings\n"
            "  POST /events            Send event to daemon\n"
            "  POST /tasks/mark-complete Mark a task as complete\n"
            "  POST /reports/generate  Generate report from events\n"
            "  POST /analyze/backfill  Reclassify unclassified events\n"
            "\n"
            "CONFIGURATION:\n"
            "  Config file: ~/.logger/daemon.json (auto-loaded if exists)\n"
            "  Priority:    CLI args > config file > defaults\n"
            "\n"
            "EXAMPLES:\n"
            "  # Start with default settings\n"
            "  python daemon.py\n"
            "\n"
            "  # Start on custom port with Ollama model\n"
            "  python daemon.py --port 9000 --model mistral\n"
            "\n"
            "  # Enable authentication for remote access\n"
            "  python daemon.py --host 0.0.0.0 --port 8765 --api-key my-secret-key\n"
            "\n"
            "  # Load from config file\n"
            "  python daemon.py --config-file ~/.logger/daemon.json\n"
            "\n"
            "  # Disable AI for development\n"
            "  python daemon.py --ai-disabled\n"
            "\n"
            "TROUBLESHOOTING:\n"
            "  Ollama not running?     Run: ollama serve\n"
            "  Check status:           curl http://localhost:8765/health\n"
            "  Port in use?            Change with: python daemon.py --port 9000\n"
            "  Model not found?        Install with: ollama pull gemma2\n"
            "\n"
            "DOCUMENTATION:\n"
            "  Usage:          README.md\n"
            "  Configuration:  CONFIG.md\n"
            "  Architecture:   PLAN.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config-file", type=str, default=None, help="Load all settings from JSON config file (CLI args override)")
    parser.add_argument("--host", default=None, help="Bind address")
    parser.add_argument("--port", type=int, default=None, help="Bind port")
    parser.add_argument("--events-path", default=None, help="JSONL event store path")
    parser.add_argument(
        "--classified-path",
        default=None,
        help="JSONL classified cache path",
    )
    parser.add_argument("--jobs-path", default=None, help="JSONL analysis jobs path")
    parser.add_argument("--tasks-path", default=None, help="JSONL tasks path")
    parser.add_argument("--reports-dir", default=None, help="Report output directory")
    parser.add_argument("--screenshot-dir", default=None, help="Screenshot storage directory")
    parser.add_argument("--model", default=None, help="Ollama model for classification/refine")
    parser.add_argument("--ollama-url", default=None, help="Ollama /api/generate URL")
    parser.add_argument("--timeout", type=float, default=None, help="Ollama HTTP timeout seconds")
    parser.add_argument("--ai-enabled", action="store_true", default=None, help="Enable AI worker (default)")
    parser.add_argument("--ai-disabled", dest="ai_enabled", action="store_false", help="Disable AI worker")
    parser.add_argument("--api-key", type=str, default=None, help="Enable API key authentication (Bearer token)")
    
    args = parser.parse_args(argv[1:])
    
    # Priority: CLI arg > config file > default
    config_data = {}
    config_file = args.config_file
    
    # If no config file specified, try default paths
    if not config_file:
        default_daemon_config = Path.home() / '.logger' / 'daemon.json'
        if default_daemon_config.exists():
            config_file = str(default_daemon_config)
    
    if config_file:
        config_data = _load_daemon_config(config_file)
    
    # Apply defaults in reverse priority order
    if args.host is None:
        args.host = config_data.get('host', '127.0.0.1')
    if args.port is None:
        args.port = config_data.get('port', 8765)
    if args.events_path is None:
        args.events_path = config_data.get('events_path', str(DEFAULT_EVENT_PATH))
    if args.classified_path is None:
        args.classified_path = config_data.get('classified_path', str(DEFAULT_CLASSIFIED_PATH))
    if args.jobs_path is None:
        args.jobs_path = config_data.get('jobs_path', str(DEFAULT_JOBS_PATH))
    if args.tasks_path is None:
        args.tasks_path = config_data.get('tasks_path', str(DEFAULT_TASKS_PATH))
    if args.reports_dir is None:
        args.reports_dir = config_data.get('reports_dir', str(DEFAULT_REPORT_DIR))
    if args.screenshot_dir is None:
        args.screenshot_dir = config_data.get('screenshot_dir', str(DEFAULT_SCREENSHOT_DIR))
    if args.model is None:
        args.model = config_data.get('model', 'gemma2')
    if args.ollama_url is None:
        args.ollama_url = config_data.get('ollama_url', DEFAULT_OLLAMA_URL)
    if args.timeout is None:
        args.timeout = config_data.get('timeout', 120.0)
    if args.api_key is None:
        args.api_key = config_data.get('api_key')
    
    # Handle ai_enabled specially since it uses action="store_true" with default=None
    if args.ai_enabled is None:
        args.ai_enabled = config_data.get('ai_enabled', True)
    
    return args


def _load_daemon_config(config_path: str) -> dict[str, Any]:
    """Load daemon configuration from JSON file and return as dict"""
    try:
        path = Path(config_path).expanduser()
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    settings = RuntimeSettings(
        model=args.model,
        ollama_url=args.ollama_url,
        timeout=args.timeout,
        ai_enabled=bool(args.ai_enabled),
    )
    auth_config = AuthConfig.from_args(args)
    state = DaemonState(
        events_path=Path(args.events_path).expanduser(),
        classified_path=Path(args.classified_path).expanduser(),
        jobs_path=Path(args.jobs_path).expanduser(),
        tasks_path=Path(args.tasks_path).expanduser(),
        reports_dir=Path(args.reports_dir).expanduser(),
        screenshot_dir=Path(args.screenshot_dir).expanduser(),
        settings=settings,
    )
    # On startup, rebuild classified.jsonl to remove stale entries from failed retries
    rebuild_classified_from_jobs(
        jobs_path=state.jobs_path,
        events_path=state.events_path,
        classified_path=state.classified_path,
    )
    # Reload classified_ids cache from rebuilt file
    with state.lock:
        state.classified_ids = {
            str(item.get("record_id", "")).strip()
            for item in load_jsonl(state.classified_path)
            if str(item.get("record_id", "")).strip()
        }
    start_worker(state)
    start_retry_manager(state)
    state.resume_queued_on_startup = enqueue_unclassified_events(state)
    app = build_app(state, auth_config)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv))
