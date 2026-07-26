"""Durable, per-workflow-run logging and bounded retention."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


LOG_SCHEMA_VERSION = 1
DEFAULT_RETENTION_COUNT = 30
_FINAL_EVENTS = {"RUN_FINISHED", "RUN_FAILED", "RUN_STOPPED", "RUN_ABORTED"}
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\\\|?*]')


def _safe_workflow_name(name: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", (name or "workflow").strip())
    cleaned = " ".join(cleaned.split()).strip(" .") or "workflow"
    return cleaned[:48]


class RunLogSession:
    """A thread-safe append-only log for exactly one workflow execution."""

    def __init__(self, runtime_dir: Path, metadata: dict[str, Any], logger=None):
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("ScreenBot")
        self.metadata = dict(metadata)
        started = datetime.now().astimezone()
        self.started_at = started
        self._started_monotonic = time.monotonic()
        self.run_id = f"run_{started.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        workflow_name = _safe_workflow_name(str(self.metadata.get("workflow_name") or "workflow"))
        stem = f"runtime_{started.strftime('%Y%m%d_%H%M%S')}_{workflow_name}_{self.run_id}"
        self.log_path = self.runtime_dir / f"{stem}.log"
        self.jsonl_path = self.runtime_dir / f"{stem}.jsonl"
        self._log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
        self._jsonl_handle = self.jsonl_path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.RLock()
        self._sequence = 0
        self._finalized = False
        self._write_start()

    @property
    def finalized(self) -> bool:
        return self._finalized

    def _write_start(self) -> None:
        header = {
            "run_id": self.run_id,
            "workflow_id": self.metadata.get("workflow_id"),
            "workflow_name": self.metadata.get("workflow_name"),
            "started_at": self.started_at.isoformat(),
            "started_monotonic": self._started_monotonic,
            "application_version": self.metadata.get("application_version", "unknown"),
            "application_pid": self.metadata.get("application_pid", os.getpid()),
            "log_schema_version": LOG_SCHEMA_VERSION,
            "target_hwnd": self.metadata.get("target_hwnd"),
            "target_title": self.metadata.get("target_title"),
            "target_client_size": self.metadata.get("target_client_size"),
            "capture_backend": self.metadata.get("capture_backend"),
            "loop_mode": self.metadata.get("loop_mode"),
            "max_cycles": self.metadata.get("max_cycles"),
            "restart_step": self.metadata.get("restart_step"),
            "steps_total": self.metadata.get("steps_total"),
        }
        self._log_handle.write("=== WORKFLOW RUN START ===\n")
        for key, value in header.items():
            self._log_handle.write(f"{key}: {value}\n")
        self._log_handle.write("\n")
        self.record("Run", "RUN_STARTED", data=header, force_flush=True)

    def record(self, category: str, event: str, *, data: dict[str, Any] | None = None,
               workflow_id: str | None = None, step_id: str | None = None,
               cycle: int | None = None, message: str | None = None,
               force_flush: bool = False) -> dict[str, Any] | None:
        with self._lock:
            if self._finalized:
                return None
            self._sequence += 1
            now = datetime.now().astimezone()
            monotonic_ms = int(round((time.monotonic() - self._started_monotonic) * 1000.0))
            payload = {"schema_version": LOG_SCHEMA_VERSION, "run_id": self.run_id,
                       "sequence": self._sequence, "timestamp": now.isoformat(),
                       "monotonic_ms": monotonic_ms, "category": category, "event": event,
                       "workflow_id": workflow_id if workflow_id is not None else self.metadata.get("workflow_id"),
                       "step_id": step_id, "cycle": cycle, "data": data or {}}
            self._jsonl_handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            details = message or event
            if data:
                details = f"{details} {json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)}"
            self._log_handle.write(f"{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} [{category}] {details}\n")
            if force_flush or event in _FINAL_EVENTS or event in {"MACRO_FINISHED", "WORKFLOW_FINISHED"}:
                self._flush_locked()
            return payload

    def finalize(self, status: str, summary: dict[str, Any]) -> bool:
        with self._lock:
            if self._finalized:
                return False
            elapsed_ms = int(round((time.monotonic() - self._started_monotonic) * 1000.0))
            final_event = {"FINISHED": "RUN_FINISHED", "FAILED": "RUN_FAILED",
                           "STOPPED": "RUN_STOPPED", "ABORTED": "RUN_ABORTED"}.get(status, "RUN_FAILED")
            if status == "FINISHED":
                self.record("Workflow", "WORKFLOW_FINISHED", data=summary, force_flush=True)
            elif status == "FAILED":
                self.record("Workflow", "WORKFLOW_FAILED", data=summary, force_flush=True)
            elif status == "STOPPED":
                self.record("Workflow", "WORKFLOW_STOPPED", data=summary, force_flush=True)
            self.record("Run", final_event, data=summary, force_flush=True)
            end = {"run_id": self.run_id, "workflow_name": self.metadata.get("workflow_name"),
                   "started_at": self.started_at.isoformat(), "finished_at": datetime.now().astimezone().isoformat(),
                   "elapsed_ms": elapsed_ms, "finish_reason": summary.get("finish_reason"),
                   "final_state": summary.get("workflow_state"), "completed_cycles": summary.get("completed_cycles", 0),
                   "trigger_count": summary.get("trigger_count", 0), "macro_count": summary.get("macro_count", 0),
                   "poll_count": summary.get("poll_count", 0), "error_count": summary.get("error_count", 0),
                   "last_error": summary.get("last_error")}
            self._log_handle.write("\n=== WORKFLOW RUN END ===\n")
            for key, value in end.items():
                self._log_handle.write(f"{key}: {value}\n")
            self._flush_locked()
            self._log_handle.close()
            self._jsonl_handle.close()
            self._finalized = True
            return True

    def _flush_locked(self) -> None:
        self._log_handle.flush()
        self._jsonl_handle.flush()


class RuntimeRunLogManager:
    """Owns the active session and runs retention outside the UI thread."""

    def __init__(self, root_path: Path | str, logger=None, retention_count: int = DEFAULT_RETENTION_COUNT,
                 background_maintenance: bool = True):
        self.runtime_dir = Path(root_path) / "logs" / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("ScreenBot")
        self.retention_count = retention_count if isinstance(retention_count, int) and retention_count >= 1 else DEFAULT_RETENTION_COUNT
        self.background_maintenance = background_maintenance
        self._lock = threading.RLock()
        self._active: RunLogSession | None = None
        self._maintenance_lock = threading.Lock()
        if self.background_maintenance:
            self.schedule_retention()

    @property
    def active_session(self) -> RunLogSession | None:
        with self._lock:
            return self._active

    def start(self, metadata: dict[str, Any]) -> RunLogSession:
        with self._lock:
            if self._active and not self._active.finalized:
                self.finalize("STOPPED", {"workflow_state": "STOPPED", "finish_reason": "superseded_by_new_run"})
            self._active = RunLogSession(self.runtime_dir, metadata, self.logger)
            return self._active

    def record(self, category: str, event: str, **kwargs: Any) -> dict[str, Any] | None:
        session = self.active_session
        return session.record(category, event, **kwargs) if session else None

    def finalize(self, status: str, summary: dict[str, Any]) -> bool:
        with self._lock:
            session = self._active
            if session is None:
                return False
            finalized = session.finalize(status, summary)
            if finalized:
                self._active = None
                self.schedule_retention()
            return finalized

    def schedule_retention(self) -> None:
        if not self.background_maintenance:
            return
        if not self._maintenance_lock.acquire(blocking=False):
            return
        threading.Thread(target=self._retention_worker, name="runtime-log-retention", daemon=True).start()

    def _retention_worker(self) -> None:
        try:
            self._recover_aborted_runs()
            groups = self._completed_groups()
            delete_count = max(0, len(groups) - self.retention_count)
            self.logger.info("[RuntimeLogRetention] Scan completed runs=%s keep=%s delete=%s", len(groups), self.retention_count, delete_count)
            for group in groups[:delete_count]:
                for path in group["paths"]:
                    try:
                        path.unlink()
                    except OSError as exc:
                        self.logger.warning("[RuntimeLogRetention] Delete failed run_id=%s error=%s", group["run_id"], exc)
                        break
                else:
                    self.logger.info("[RuntimeLogRetention] Deleted run_id=%s", group["run_id"])
        except Exception:
            self.logger.exception("[RuntimeLogRetention] Maintenance failed")
        finally:
            self._maintenance_lock.release()

    def _recover_aborted_runs(self) -> None:
        """Close stale runs only when their recorded owning process is gone.

        We intentionally leave legacy/unknown files untouched: an unrecognised
        file may belong to another ScreenBot instance or to the user.
        """
        for jsonl_path in self.runtime_dir.glob("runtime_*_run_*.jsonl"):
            try:
                if time.time() - jsonl_path.stat().st_mtime < 5:
                    continue
                lines = [line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if not lines:
                    continue
                first, last = json.loads(lines[0]), json.loads(lines[-1])
                if last.get("event") in _FINAL_EVENTS:
                    continue
                metadata = first.get("data") or {}
                pid = metadata.get("application_pid")
                if not isinstance(pid, int) or _pid_is_alive(pid):
                    continue
                run_id = first.get("run_id")
                sequence = int(last.get("sequence", 0) or 0) + 1
                now = datetime.now().astimezone()
                event = {
                    "schema_version": LOG_SCHEMA_VERSION, "run_id": run_id, "sequence": sequence,
                    "timestamp": now.isoformat(), "monotonic_ms": None, "category": "Run",
                    "event": "RUN_ABORTED", "workflow_id": first.get("workflow_id"),
                    "step_id": None, "cycle": None,
                    "data": {"reason": "application_terminated_before_log_close"},
                }
                with jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                for log_path in self.runtime_dir.glob(f"*{run_id}.log"):
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write("\n[Run] RUN_ABORTED application_terminated_before_log_close\n")
                        handle.write("=== WORKFLOW RUN END ===\n")
                        handle.write(f"run_id: {run_id}\nfinish_reason: application_terminated_before_log_close\n")
                self.logger.info("[RuntimeLogRetention] Marked aborted run_id=%s", run_id)
            except (OSError, ValueError, json.JSONDecodeError):
                self.logger.warning("[RuntimeLogRetention] Could not inspect unfinished run: %s", jsonl_path.name)

    def run_retention_sync(self) -> None:
        """Test/maintenance entry point; normal application use is asynchronous."""
        if not self._maintenance_lock.acquire(blocking=False):
            return
        self._retention_worker()

    def _completed_groups(self) -> list[dict[str, Any]]:
        active_run_id = self.active_session.run_id if self.active_session else None
        groups: list[dict[str, Any]] = []
        for jsonl_path in self.runtime_dir.glob("runtime_*_run_*.jsonl"):
            try:
                lines = [line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if not lines:
                    continue
                first, last = json.loads(lines[0]), json.loads(lines[-1])
                run_id = first.get("run_id")
                if not isinstance(run_id, str) or last.get("event") not in _FINAL_EVENTS or run_id == active_run_id:
                    continue
                companions = [path for path in self.runtime_dir.glob(f"*{run_id}.*") if path.suffix in {".log", ".jsonl"}]
                if not companions:
                    continue
                groups.append({"run_id": run_id, "started_at": first.get("timestamp") or "",
                               "paths": companions, "fallback_mtime": jsonl_path.stat().st_mtime})
            except (OSError, ValueError, json.JSONDecodeError):
                self.logger.warning("[RuntimeLogRetention] Skipped invalid runtime metadata: %s", jsonl_path.name)
        return sorted(groups, key=lambda group: (group["started_at"], group["fallback_mtime"], group["run_id"]))


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
