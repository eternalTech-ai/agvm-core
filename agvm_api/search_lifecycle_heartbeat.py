# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import copy_context
from typing import Any


EventWriter = Callable[[str, dict[str, Any]], Any]


class FinalMaterializationHeartbeatRegistry:
    """Own one truthful final-materialization heartbeat per search lifecycle."""

    def __init__(self) -> None:
        self.states: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def interval_seconds() -> float:
        try:
            configured = float(
                os.getenv("AGVM_SEARCH_FINAL_MATERIALIZATION_HEARTBEAT_SECONDS", "5.0")
                or 5.0
            )
        except (TypeError, ValueError):
            configured = 5.0
        return max(0.01, min(configured, 10.0))

    def stop(self, search_id: str) -> None:
        resolved_search_id = str(search_id or "").strip()
        if not resolved_search_id:
            return
        with self.lock:
            state = self.states.pop(resolved_search_id, None)
        if state:
            state[0].set()

    def after_event(
        self,
        search_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        *,
        persist_heartbeat: EventWriter,
        persist_diagnostic: EventWriter,
    ) -> None:
        normalized_event_type = str(event_type or "")
        captured_payload = dict(payload or {})
        if normalized_event_type == "final_materialization_started":
            self.start(
                search_id,
                {**captured_payload, "event_type": normalized_event_type},
                persist_heartbeat=persist_heartbeat,
                persist_diagnostic=persist_diagnostic,
            )
        elif normalized_event_type in {"result_ready", "search_failed"} and (
            normalized_event_type == "search_failed"
            or not bool(captured_payload.get("final_materialization_pending"))
        ):
            self.stop(search_id)

    def start(
        self,
        search_id: str,
        payload: Mapping[str, Any] | None,
        *,
        persist_heartbeat: EventWriter,
        persist_diagnostic: EventWriter,
    ) -> None:
        resolved_search_id = str(search_id or "").strip()
        if not resolved_search_id:
            return
        captured_payload = dict(payload or {})
        captured_brain_id = str(captured_payload.get("brain_id") or "").strip() or None
        heartbeat_context = copy_context()
        stop_event = threading.Event()
        started_at = time.perf_counter()
        interval_seconds = self.interval_seconds()

        def heartbeat_loop() -> None:
            heartbeat_index = 0
            persist_error_count = 0
            consecutive_persist_error_count = 0
            last_persist_error: str | None = None
            last_persist_error_elapsed_ms: float | None = None
            last_successful_heartbeat_elapsed_ms: float | None = None
            try:
                while not stop_event.wait(interval_seconds):
                    heartbeat_index += 1
                    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
                    event_payload: dict[str, Any] = {
                        "search_id": resolved_search_id,
                        "runtime_phase": "final_materialization",
                        "result_materialization_state": "materializing",
                        "final_materialization_pending": True,
                        "result_ready_terminal": False,
                        "heartbeat_index": heartbeat_index,
                        "heartbeat_interval_seconds": interval_seconds,
                        "elapsed_ms": elapsed_ms,
                        "source_event_type": str(
                            captured_payload.get("event_type")
                            or "final_materialization_started"
                        ),
                        "heartbeat_persist_error_count": persist_error_count,
                        "heartbeat_consecutive_persist_error_count": consecutive_persist_error_count,
                        "last_heartbeat_persist_error": last_persist_error,
                        "last_heartbeat_persist_error_elapsed_ms": last_persist_error_elapsed_ms,
                        "last_successful_heartbeat_elapsed_ms": last_successful_heartbeat_elapsed_ms,
                    }
                    if captured_brain_id:
                        event_payload["brain_id"] = captured_brain_id
                    try:
                        persist_heartbeat("final_materialization_heartbeat", event_payload)
                    except Exception as exc:  # noqa: BLE001
                        persist_error_count += 1
                        consecutive_persist_error_count += 1
                        last_persist_error = str(exc)[:512]
                        last_persist_error_elapsed_ms = elapsed_ms
                        diagnostic_payload: dict[str, Any] = {
                            "search_id": resolved_search_id,
                            "runtime_phase": "final_materialization",
                            "result_materialization_state": "materializing",
                            "final_materialization_pending": True,
                            "result_ready_terminal": False,
                            "heartbeat_index": heartbeat_index,
                            "heartbeat_interval_seconds": interval_seconds,
                            "elapsed_ms": elapsed_ms,
                            "heartbeat_persist_error_count": persist_error_count,
                            "heartbeat_consecutive_persist_error_count": consecutive_persist_error_count,
                            "error": last_persist_error,
                            "retry_state": "retry_next_interval",
                        }
                        if captured_brain_id:
                            diagnostic_payload["brain_id"] = captured_brain_id
                        try:
                            persist_diagnostic(
                                "final_materialization_heartbeat_error",
                                diagnostic_payload,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    last_successful_heartbeat_elapsed_ms = elapsed_ms
                    consecutive_persist_error_count = 0
            finally:
                with self.lock:
                    current = self.states.get(resolved_search_id)
                    if current and current[0] is stop_event:
                        self.states.pop(resolved_search_id, None)

        with self.lock:
            for existing_search_id, existing in list(self.states.items()):
                if not existing[1].is_alive():
                    self.states.pop(existing_search_id, None)
            existing = self.states.get(resolved_search_id)
            if existing and existing[1].is_alive():
                return
            thread = threading.Thread(
                target=lambda: heartbeat_context.run(heartbeat_loop),
                name=f"agvm-final-materialization-heartbeat-{resolved_search_id}",
                daemon=True,
            )
            self.states[resolved_search_id] = (stop_event, thread)
            thread.start()


FINAL_MATERIALIZATION_HEARTBEATS = FinalMaterializationHeartbeatRegistry()
