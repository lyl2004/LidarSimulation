from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from path_resolver import get_root as _get_root

from .system_probe import build_session_header, dump_json


def diagnostics_enabled() -> bool:
    return os.environ.get("LIDAR_DIAGNOSTICS", "").strip() == "1"


def ensure_json_serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): ensure_json_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [ensure_json_serializable(v) for v in value]
    try:
        json.dumps(value)
        return value
    except Exception:
        return repr(value)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _default_run_id(prefix: str | None = None) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    if prefix:
        return f"{prefix}_{ts}_{suffix}"
    return f"run_{ts}_{suffix}"


def get_diagnostics_root() -> Path:
    override = os.environ.get("LIDAR_DIAGNOSTICS_ROOT")
    if override:
        return Path(override)
    return _get_root() / "diagnostics"


class DiagnosticsSession:
    def __init__(
        self,
        *,
        component: str,
        enabled: bool,
        root_dir: Path | None = None,
        run_id: str | None = None,
        mode: str | None = None,
        create_header: bool = True,
    ) -> None:
        self.component = component
        self.enabled = enabled
        self.mode = mode or os.environ.get("LIDAR_DIAGNOSTICS_MODE", "first_run")
        self.run_id = run_id or os.environ.get("LIDAR_DIAGNOSTICS_RUN_ID") or _default_run_id(component)
        self.root_dir = (root_dir or get_diagnostics_root()).resolve()
        self.run_dir = self.root_dir / "runs" / self.run_id
        self.component_dir = self.run_dir / component
        self.timeline_path = self.run_dir / "timeline.jsonl"
        self.component_timeline_path = self.component_dir / f"{component}_timeline.jsonl"
        self.summary_path = self.run_dir / "summary.json"
        self.session_path = self.run_dir / "session.json"
        self.host_info_path = self.run_dir / "host_info.json"
        self.runtime_env_path = self.run_dir / "runtime_env.json"
        self.raw_refs_path = self.run_dir / "raw_refs" / "known_logs.json"
        self.residual_check_path = self.run_dir / "residual_check.json"
        self._lock = threading.Lock()
        self._closed = False
        if self.enabled:
            self._init_files(create_header=create_header)

    def _safe_write_text(self, path: Path, text: str, append: bool = False) -> None:
        if not self.enabled:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(path, mode, encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass

    def _safe_write_json(self, path: Path, payload: dict) -> None:
        if not self.enabled:
            return
        try:
            dump_json(path, ensure_json_serializable(payload))
        except Exception:
            pass

    def _init_files(self, *, create_header: bool) -> None:
        try:
            self.component_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "raw_refs").mkdir(parents=True, exist_ok=True)
            session_payload = {
                "run_id": self.run_id,
                "mode": self.mode,
                "component": self.component,
                "root_dir": str(self.root_dir),
                "run_dir": str(self.run_dir),
                "created_at": _utc_now(),
            }
            self._safe_write_json(self.session_path, session_payload)
            if create_header:
                header = build_session_header()
                self._safe_write_json(self.host_info_path, header.get("host_info", {}))
                self._safe_write_json(self.runtime_env_path, header.get("runtime_env", {}))
                self._safe_write_json(
                    self.raw_refs_path,
                    {
                        "tool_versions": header.get("tool_versions", {}),
                        "known_logs": {},
                    },
                )
                self._safe_write_json(
                    self.summary_path,
                    {
                        "run_id": self.run_id,
                        "mode": self.mode,
                        "status": "running",
                        "components": {},
                        "created_at": session_payload["created_at"],
                        "updated_at": session_payload["created_at"],
                    },
                )
                self._safe_write_json(
                    self.residual_check_path,
                    {
                        "scope": "project_local_only",
                        "checked_paths": [
                            str(_get_root() / "outputs"),
                            str(_get_root() / "log"),
                            str(_get_root() / "diagnostics"),
                            str(_get_root() / "temp" / "lidar_1d" / "run_history"),
                        ],
                        "excluded_global_actions": [
                            "system python cache",
                            "system julia cache",
                            "pixi global cache",
                            "PATH mutation",
                            "registry mutation",
                        ],
                    },
                )
        except Exception:
            pass

    def _build_record(
        self,
        stage: str,
        *,
        status: str = "ok",
        elapsed_ms: float | None = None,
        payload: dict | None = None,
        component: str | None = None,
    ) -> dict:
        return {
            "run_id": self.run_id,
            "ts": _utc_now(),
            "component": component or self.component,
            "stage": stage,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "payload": ensure_json_serializable(payload or {}),
        }

    def event(
        self,
        stage: str,
        *,
        status: str = "ok",
        elapsed_ms: float | None = None,
        payload: dict | None = None,
        component: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        record = self._build_record(
            stage,
            status=status,
            elapsed_ms=elapsed_ms,
            payload=payload,
            component=component,
        )
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self._safe_write_text(self.timeline_path, line, append=True)
            self._safe_write_text(self.component_timeline_path, line, append=True)

    def write_component_event(
        self,
        relative_path: str,
        stage: str,
        *,
        status: str = "ok",
        elapsed_ms: float | None = None,
        payload: dict | None = None,
        component: str | None = None,
    ) -> Path:
        record = self._build_record(
            stage,
            status=status,
            elapsed_ms=elapsed_ms,
            payload=payload,
            component=component,
        )
        path = self.component_dir / relative_path
        self._safe_write_text(path, json.dumps(record, ensure_ascii=False) + "\n", append=True)
        return path

    def set_known_log(self, name: str, path: str | Path) -> None:
        if not self.enabled:
            return
        try:
            current = {}
            if self.raw_refs_path.exists():
                current = json.loads(self.raw_refs_path.read_text(encoding="utf-8"))
            known_logs = current.setdefault("known_logs", {})
            known_logs[name] = str(path)
            self._safe_write_json(self.raw_refs_path, current)
        except Exception:
            pass

    def write_component_json(self, relative_path: str, payload: dict) -> Path:
        path = self.component_dir / relative_path
        self._safe_write_json(path, payload)
        return path

    def write_run_json(self, relative_path: str, payload: dict) -> Path:
        path = self.run_dir / relative_path
        self._safe_write_json(path, payload)
        return path

    def append_component_log(self, relative_path: str, text: str) -> Path:
        path = self.component_dir / relative_path
        self._safe_write_text(path, text, append=True)
        return path

    def update_summary(self, component: str, payload: dict, *, status: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            summary = {}
            if self.summary_path.exists():
                summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
            summary.setdefault("components", {})[component] = ensure_json_serializable(payload)
            if status:
                summary["status"] = status
            summary["updated_at"] = _utc_now()
            self._safe_write_json(self.summary_path, summary)
        except Exception:
            pass

    def close(self, *, status: str = "success", payload: dict | None = None) -> None:
        if not self.enabled or self._closed:
            return
        self._closed = True
        self.update_summary(self.component, payload or {}, status=status)
        self.event(f"{self.component}_closed", status=status, payload=payload or {})

    def fail(self, error: BaseException, *, stage: str, payload: dict | None = None) -> None:
        merged = dict(payload or {})
        merged.update({
            "error": str(error),
            "traceback": traceback.format_exc(),
        })
        self.event(stage, status="error", payload=merged)
        self.update_summary(self.component, merged, status="error")

    @contextmanager
    def span(self, stage: str, *, payload: dict | None = None):
        start = _dt.datetime.now()
        self.event(f"{stage}_begin", status="begin", payload=payload or {})
        try:
            yield
        except Exception as exc:
            elapsed_ms = (_dt.datetime.now() - start).total_seconds() * 1000.0
            self.event(stage, status="error", elapsed_ms=elapsed_ms, payload={**(payload or {}), "error": str(exc)})
            raise
        else:
            elapsed_ms = (_dt.datetime.now() - start).total_seconds() * 1000.0
            self.event(stage, status="ok", elapsed_ms=elapsed_ms, payload=payload or {})


def get_or_create_session(
    component: str,
    *,
    root_dir: Path | None = None,
    run_id: str | None = None,
    mode: str | None = None,
    create_header: bool = True,
) -> DiagnosticsSession:
    return DiagnosticsSession(
        component=component,
        enabled=diagnostics_enabled(),
        root_dir=root_dir,
        run_id=run_id,
        mode=mode,
        create_header=create_header,
    )


class ComponentLogger:
    def __init__(self, session: DiagnosticsSession, component: str) -> None:
        self.session = session
        self.component = component

    def event(self, stage: str, *, status: str = "ok", elapsed_ms: float | None = None, payload: dict | None = None) -> None:
        self.session.event(stage, status=status, elapsed_ms=elapsed_ms, payload=payload, component=self.component)

    @contextmanager
    def span(self, stage: str, *, payload: dict | None = None):
        start = _dt.datetime.now()
        self.event(f"{stage}_begin", status="begin", payload=payload or {})
        try:
            yield
        except Exception as exc:
            elapsed_ms = (_dt.datetime.now() - start).total_seconds() * 1000.0
            self.event(stage, status="error", elapsed_ms=elapsed_ms, payload={**(payload or {}), "error": str(exc)})
            raise
        else:
            elapsed_ms = (_dt.datetime.now() - start).total_seconds() * 1000.0
            self.event(stage, status="ok", elapsed_ms=elapsed_ms, payload=payload or {})


def new_component_logger(session: DiagnosticsSession, component: str) -> ComponentLogger:
    return ComponentLogger(session, component)
