#!/usr/bin/env python3
from __future__ import annotations

import argparse
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core_stream_engine import (
    DEFAULT_CLASSIFIED_PATH,
    DEFAULT_EVENT_PATH,
    DEFAULT_JOBS_PATH,
    DEFAULT_OLLAMA_URL,
    DEFAULT_REPORT_DIR,
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


@dataclass
class RuntimeSettings:
    model: str
    ollama_url: str
    timeout: float
    ai_enabled: bool


class DaemonState:
    def __init__(
        self,
        *,
        events_path: Path,
        classified_path: Path,
        jobs_path: Path,
        reports_dir: Path,
        settings: RuntimeSettings,
    ) -> None:
        self.events_path = events_path
        self.classified_path = classified_path
        self.jobs_path = jobs_path
        self.reports_dir = reports_dir
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


def build_app(state: DaemonState) -> FastAPI:
    app = FastAPI(title="Core-Stream Daemon", version="1.0.0")

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
        append_jsonl(state.events_path, event)
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

    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Core-Stream daemon API server",
        epilog=(
            "endpoints: /health, /settings, /settings/ai, /events, /analyze/backfill, /reports/generate\n"
            "example: python daemon.py --host 127.0.0.1 --port 8765 --model gemma2"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--events-path", default=str(DEFAULT_EVENT_PATH), help="JSONL event store path")
    parser.add_argument(
        "--classified-path",
        default=str(DEFAULT_CLASSIFIED_PATH),
        help="JSONL classified cache path",
    )
    parser.add_argument("--jobs-path", default=str(DEFAULT_JOBS_PATH), help="JSONL analysis jobs path")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORT_DIR), help="Report output directory")
    parser.add_argument("--model", default="gemma2", help="Ollama model for classification/refine")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama /api/generate URL")
    parser.add_argument("--timeout", type=float, default=120.0, help="Ollama HTTP timeout seconds")
    parser.add_argument("--ai-enabled", action="store_true", default=True, help="Enable AI worker (default)")
    parser.add_argument("--ai-disabled", dest="ai_enabled", action="store_false", help="Disable AI worker")
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    settings = RuntimeSettings(
        model=args.model,
        ollama_url=args.ollama_url,
        timeout=args.timeout,
        ai_enabled=bool(args.ai_enabled),
    )
    state = DaemonState(
        events_path=Path(args.events_path).expanduser(),
        classified_path=Path(args.classified_path).expanduser(),
        jobs_path=Path(args.jobs_path).expanduser(),
        reports_dir=Path(args.reports_dir).expanduser(),
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
    app = build_app(state)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv))
