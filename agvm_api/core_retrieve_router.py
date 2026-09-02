# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import copy_context
from typing import Any, AsyncIterator, Iterator, Mapping

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from brain_health import build_brain_health_report, build_mcp_brain_health_output
from health_ai_diagnosis import runtime_health_ai_diagnoser
from brain_registry import BrainRegistryError, resolve_brain_scope
from feedback_ledger_runtime import (
    persisted_feedback_events_for_health,
    record_feedback_event,
    record_mcp_evidence,
    record_search_terminal,
)
from answering import (
    build_context_payload,
    build_document_workspace_package,
    build_mcp_context_package,
    build_query_contract,
)
from core_document_registry import preview_document_node
from mcp_retrieval import (
    build_mcp_memory_object_output,
    build_mcp_retrieval_tool_output,
    build_mcp_route_trace_output,
)
from retrieval import (
    _apply_plan_first_usable_partial_public_projection,
    _planner_seed_transport_guard_seconds,
    _search_ai_admission_timeout_seconds,
    SearchAiExecutionError,
    build_search_ai_execution_http_block_payload,
    build_search_ai_http_block_payload,
    build_search_ai_blocked_result,
    build_landing_metadata,
    build_search_map_2d_truth,
    normalize_retrieve_response_payload,
    prepare_runtime_plan,
    require_search_ai_admission,
    request_search_supersede,
    retrieve_runtime,
    search_identity_nucleus_for_named_targets,
)
from runtime_scope import current_brain_id, runtime_scope_summary, use_runtime_brain
from search_lifecycle_heartbeat import FINAL_MATERIALIZATION_HEARTBEATS
from schemas import (
    McpBrainHealthRequest,
    McpBrainHealthToolExecutionResponse,
    McpInspectionRequest,
    McpMemoryObjectInspectionRequest,
    McpRetrievalToolRequest,
    McpToolExecutionResponse,
    RetrieveRequest,
    RetrieveResponse,
    SearchPlanResponse,
    SearchRunLedgerResponse,
    SearchRunRequest,
    SearchRunResponse,
    SearchTraceResponse,
)
from sqlite_store import (
    append_search_event,
    apply_runtime_retention_policy,
    cancel_search_session as _store_cancel_search_session,
    create_search_session,
    fail_search_session as _store_fail_search_session,
    fetch_active_search_session_by_thread,
    fetch_atlas,
    fetch_cluster_runtime,
    fetch_graph_snapshot,
    fetch_heuristic_calibration_snapshot,
    fetch_identity_nucleus,
    fetch_nodes_by_ids,
    fetch_nodes_by_text_terms,
    fetch_recent_maintenance_runs,
    fetch_recent_search_sessions,
    fetch_search_events,
    fetch_search_session,
    fetch_search_trace,
    finalize_search_session as _store_finalize_search_session,
    mark_search_running,
    preview_runtime_retention_policy,
    save_search_plan,
    save_search_result_snapshot,
)
from storage import utc_timestamp
from stream_contract import annotate_stream_event, project_search_result_lifecycle, search_result_ref

try:
    from metamemory import metamemory_snapshot
except Exception:  # noqa: BLE001
    metamemory_snapshot = None  # type: ignore[assignment]


_SEARCH_THREADS: dict[str, threading.Thread] = {}
_SEARCH_THREAD_LOCK = threading.Lock()
_MCP_FIRST_PACKAGE_SIGNALS: dict[str, threading.Event] = {}
_MCP_FIRST_PACKAGE_CACHE: dict[str, dict[str, Any]] = {}
_MCP_FIRST_PACKAGE_LOCK = threading.Lock()
_MCP_FIRST_PACKAGE_WAIT_SECONDS = {
    "flash": 2.4,
    "balanced": 4.8,
    "heavy": 6.0,
    "forensic": 6.0,
}


def _query_stream_after_seq(after_seq: int = 0, last_event_id: str | None = None) -> int:
    if isinstance(last_event_id, str) and last_event_id:
        value = str(last_event_id).strip()
        if value:
            try:
                return max(0, int(value))
            except ValueError:
                match = re.search(r"(\d+)$", value)
                if match:
                    return max(0, int(match.group(1)))
    try:
        return max(0, int(after_seq or 0))
    except (TypeError, ValueError):
        return 0


def _sse_search_event_frame(event: dict[str, Any], *, retry_ms: int = 1000) -> str:
    event_type = str(event.get("event_type") or "message")
    seq = int(event.get("seq") or 0)
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {event_type}\nretry: {int(retry_ms)}\ndata: {payload}\n\n"
_MCP_SURFACE_FIELDS = (
    "payload_integrity",
    "payload_truth_contract",
    "budget",
    "timing",
    "latency_contract",
    "completion_contract",
    "run_lifecycle_contract",
    "runtime_state_contract",
    "tool_boundary_contract",
    "ai_materialization_resilience_contract",
    "ai_critical_path_contract",
    "route_arbitration_contract",
    "first_package_background_contract",
    "run_projection_event_stream_contract",
    "mcp_delivery_contract",
    "run_projection_truth",
    "document_refs",
    "document_ref_contract",
    "document_delivery_contract",
    "document_bundle",
    "hot_working_memory",
    "hot_working_memory_contract",
    "answer_demo_materialization",
)


def finalize_search_session(search_id: str, result_payload: dict[str, Any]) -> dict[str, Any]:
    final = _store_finalize_search_session(search_id, result_payload) or dict(result_payload or {})
    record_search_terminal(
        brain_id=str(current_brain_id() or final.get("brain_id") or ""),
        session_id=search_id,
        result=final,
    )
    return final


def fail_search_session(search_id: str, error_message: str) -> None:
    _store_fail_search_session(search_id, error_message)
    record_search_terminal(
        brain_id=str(current_brain_id() or ""),
        session_id=search_id,
        failed_reason=error_message,
    )


def cancel_search_session(
    search_id: str,
    *,
    expected_state_revision: int | None = None,
    idempotency_key: str | None = None,
    reason: str = "user_cancelled",
) -> dict[str, Any] | None:
    return _store_cancel_search_session(
        search_id,
        expected_state_revision=expected_state_revision,
        idempotency_key=idempotency_key,
        reason=reason,
    )


def _search_stream_url(search_id: str) -> str:
    return f"/memory/query-stream/{search_id}?brain_id={current_brain_id()}"


def _search_result_url(search_id: str) -> str:
    return f"/memory/query-result/{search_id}?brain_id={current_brain_id()}"


def _search_accept_payload(search_id: str, payload: RetrieveRequest) -> dict[str, Any]:
    return {
        "schema_version": "agvm.search_accept.v1",
        "status": "accepted",
        "search_id": search_id,
        "brain_id": current_brain_id(),
        "thread_id": payload.thread_id,
        "query_text": payload.query_text,
        "stream_url": _search_stream_url(search_id),
        "result_url": _search_result_url(search_id),
    }


def create_core_retrieve_router() -> APIRouter:
    router = APIRouter()

    @router.post("/retrieve", response_model=RetrieveResponse)
    def retrieve_endpoint(payload: RetrieveRequest) -> RetrieveResponse:
        return memory_query_endpoint(payload)

    @router.post("/memory/query", response_model=RetrieveResponse)
    def memory_query_endpoint(payload: RetrieveRequest) -> RetrieveResponse:
        with _brain_request_scope(_payload_brain_id(payload), require_retrieval_ready=True):
            normalized_payload = _normalized_retrieve_request(payload)
            admission = _public_search_ai_admission(normalized_payload)
            if str(admission.get("status") or "") != "admitted":
                blocked = build_search_ai_blocked_result(normalized_payload, admission)
                return RetrieveResponse(**_retrieve_response_schema_safe(blocked))
            search_id, _plan = _create_planned_search_session(
                normalized_payload,
                ai_admission=admission,
            )
            result = _run_search_session_sync(search_id)
            return RetrieveResponse(**_retrieve_response_schema_safe(result))

    @router.post("/memory/query-create")
    def memory_query_create_endpoint(payload: RetrieveRequest) -> JSONResponse:
        with _brain_request_scope(_payload_brain_id(payload), require_retrieval_ready=True) as brain_record:
            normalized_payload = _normalized_retrieve_request(payload)
            search_id = _create_unplanned_search_session(
                normalized_payload,
                plan_transport="early_search_identity_background_plan",
            )
            # Provider admission and planning belong to the accepted Search worker.
            # Keeping them off the request thread preserves the early-identity
            # contract even when provider readiness checks are slow or blocked.
            _start_accepted_search_pipeline(search_id, brain_record)
            return JSONResponse(
                status_code=202,
                content=_search_accept_payload(search_id, normalized_payload),
            )

    @router.post("/memory/query-plan", response_model=SearchPlanResponse)
    def memory_query_plan_endpoint(
        payload: RetrieveRequest,
        respond_async: bool = Query(default=False),
    ) -> SearchPlanResponse | JSONResponse:
        with _brain_request_scope(_payload_brain_id(payload), require_retrieval_ready=True):
            normalized_payload = _normalized_retrieve_request(payload)
            if respond_async:
                brain_record = _resolve_brain_record(current_brain_id())
                search_id = _create_unplanned_search_session(
                    normalized_payload,
                    plan_transport="early_query_plan_background_plan",
                )
                _start_accepted_search_pipeline(search_id, brain_record)
                return JSONResponse(
                    status_code=202,
                    content=_search_accept_payload(search_id, normalized_payload),
                )
            admission = _public_search_ai_admission(normalized_payload)
            if str(admission.get("status") or "") != "admitted":
                return JSONResponse(
                    status_code=503,
                    content=build_search_ai_http_block_payload(admission),
                )
            try:
                search_id, plan = _create_planned_search_session(
                    normalized_payload,
                    ai_admission=admission,
                )
            except SearchAiExecutionError as exc:
                blocked = _planning_failure_http_payload(exc)
                return JSONResponse(
                    status_code=503,
                    content=blocked,
                )
            return _search_plan_response(search_id, normalized_payload, plan)

    @router.post("/memory/query-run", response_model=SearchRunResponse)
    def memory_query_run_endpoint(payload: SearchRunRequest) -> SearchRunResponse:
        with _brain_request_scope(_payload_brain_id(payload)) as brain_record:
            session = fetch_search_session(payload.search_id)
            if not session:
                raise HTTPException(status_code=404, detail="search_not_found")
            request_payload = dict(session.get("request") or {})
            session_brain_id = str(request_payload.get("brain_id") or current_brain_id() or "").strip() or None
            thread_brain_record = brain_record
            if session_brain_id and session_brain_id != current_brain_id():
                thread_brain_record = _resolve_brain_record(session_brain_id)
            stored_plan = dict(session.get("plan") or {})
            stored_admission = dict(stored_plan.get("search_ai_admission") or {})
            if stored_plan and str(stored_admission.get("status") or "") != "admitted":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "search_ai_admission_missing",
                        "message": "Create a new search plan with an available AI provider before running it.",
                        "charged_units": 0,
                    },
                )
            if stored_plan:
                _start_search_thread(payload.search_id, thread_brain_record)
            else:
                _start_accepted_search_pipeline(payload.search_id, thread_brain_record)
            refreshed = fetch_search_session(payload.search_id)
            status = str((refreshed or {}).get("status") or "running")
            if status not in {"created", "running", "completed", "failed"}:
                status = "running"
            return SearchRunResponse(
                search_id=payload.search_id,
                brain_id=current_brain_id(),
                status=status,  # type: ignore[arg-type]
                stream_url=_search_stream_url(payload.search_id),
                result_url=_search_result_url(payload.search_id),
            )

    @router.post("/memory/query-cancel/{search_id}")
    def memory_query_cancel_endpoint(
        search_id: str,
        payload: dict[str, Any] | None = None,
        brain_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        with _brain_request_scope(brain_id):
            body = dict(payload or {})
            try:
                expected_revision = body.get("expected_state_revision")
                outcome = cancel_search_session(
                    search_id,
                    expected_state_revision=int(expected_revision) if expected_revision is not None else None,
                    idempotency_key=str(body.get("idempotency_key") or "").strip() or None,
                    reason=str(body.get("reason") or "user_cancelled"),
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not outcome:
            raise HTTPException(status_code=404, detail="search_not_found")
        return outcome

    @router.get("/memory/query-result/{search_id}", response_model=RetrieveResponse)
    def memory_query_result_endpoint(
        search_id: str,
        brain_id: str | None = Query(default=None),
    ) -> RetrieveResponse:
        with _brain_request_scope(brain_id):
            result = _completed_search_result(search_id)
            return RetrieveResponse(**_retrieve_response_schema_safe(result))

    @router.get("/memory/run-ledger", response_model=SearchRunLedgerResponse)
    def memory_run_ledger_endpoint(
        brain_id: str | None = Query(default=None),
        limit: int = Query(default=12, ge=1, le=50),
    ) -> SearchRunLedgerResponse:
        with _brain_request_scope(brain_id):
            sessions = fetch_recent_search_sessions(limit=limit)
            return SearchRunLedgerResponse(
                brain_id=current_brain_id(),
                entries=[_run_ledger_entry_from_session(session) for session in sessions],
            )

    @router.post("/memory/runtime-retention")
    def memory_runtime_retention_endpoint(
        brain_id: str | None = Query(default=None),
        apply: bool = Query(default=False),
        keep_recent_sessions: int = Query(default=30, ge=1, le=1000),
        keep_failed_sessions: int = Query(default=10, ge=0, le=200),
        max_events_per_kept_session: int = Query(default=220, ge=1, le=5000),
        active_session_grace_minutes: int = Query(default=360, ge=1, le=10080),
        checkpoint_wal: bool = Query(default=True),
        vacuum: bool = Query(default=False),
        busy_timeout_ms: int = Query(default=2000, ge=50, le=60000),
    ) -> dict[str, Any]:
        with _brain_request_scope(brain_id):
            if apply:
                return apply_runtime_retention_policy(
                    keep_recent_sessions=keep_recent_sessions,
                    keep_failed_sessions=keep_failed_sessions,
                    max_events_per_kept_session=max_events_per_kept_session,
                    active_session_grace_minutes=active_session_grace_minutes,
                    checkpoint_wal=checkpoint_wal,
                    vacuum=vacuum,
                    busy_timeout_ms=busy_timeout_ms,
                )
            return preview_runtime_retention_policy(
                keep_recent_sessions=keep_recent_sessions,
                keep_failed_sessions=keep_failed_sessions,
                max_events_per_kept_session=max_events_per_kept_session,
                active_session_grace_minutes=active_session_grace_minutes,
                busy_timeout_ms=busy_timeout_ms,
            )

    @router.get("/memory/get-trace/{search_id}", response_model=SearchTraceResponse)
    def memory_get_trace_endpoint(
        search_id: str,
        brain_id: str | None = Query(default=None),
    ) -> SearchTraceResponse:
        with _brain_request_scope(brain_id):
            trace = fetch_search_trace(search_id)
            if not trace:
                raise HTTPException(status_code=404, detail="search_not_found")
            result = dict(trace.get("result") or {})
            planner_metadata = dict(trace.get("planner_metadata") or {})
            return SearchTraceResponse(
                search_id=search_id,
                brain_id=current_brain_id(),
                thread_id=str((trace.get("session") or {}).get("thread_id") or "") or None,
                session=dict(trace.get("session") or {}),
                events=list(trace.get("events") or []),
                corrections=list(trace.get("corrections") or []),
                timing=dict(trace.get("timing") or {}),
                planner_metadata=planner_metadata,
                answer_strands=list(result.get("answer_strands") or planner_metadata.get("answer_strands") or []),
                planner_seed_runtime=dict(result.get("planner_seed_runtime") or planner_metadata.get("planner_seed_runtime") or {}),
                seed_goal_coverage=dict(result.get("seed_goal_coverage") or planner_metadata.get("seed_goal_coverage") or {}),
                seed_destination_presence=dict(result.get("seed_destination_presence") or planner_metadata.get("seed_destination_presence") or {}),
                landing_metadata=list(trace.get("landing_metadata") or []),
                context_waves=list(trace.get("context_waves") or []),
                worker_stop_reasons=dict(trace.get("worker_stop_reasons") or {}),
                follow_up_candidates=list(trace.get("follow_up_candidates") or []),
                blackboard=dict(trace.get("blackboard") or {}),
            )

    @router.post("/memory/query-feedback")
    def memory_query_feedback_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        event_kind = str(payload.get("event_kind") or "").strip().lower()
        if event_kind not in {"explicit_review", "explicit_correct"}:
            raise HTTPException(status_code=422, detail="feedback_event_kind_invalid")
        with _brain_request_scope(str(payload.get("brain_id") or "").strip() or None):
            search_id = str(payload.get("search_id") or "").strip() or None
            if search_id:
                session = fetch_search_session(search_id)
                if not session:
                    raise HTTPException(status_code=404, detail="search_not_found")
                session_brain_id = str(dict(session.get("request") or {}).get("brain_id") or "").strip()
                if session_brain_id and session_brain_id != current_brain_id():
                    raise HTTPException(status_code=403, detail="feedback_search_brain_scope_mismatch")
            outcome = record_feedback_event(
                event_kind=event_kind,
                brain_id=str(current_brain_id() or ""),
                session_id=search_id,
                source_event_id=str(payload.get("event_id") or "").strip() or None,
                node_ids=payload.get("node_ids") or payload.get("evidence_node_ids"),
                document_ids=payload.get("document_ids"),
                verdict=payload.get("verdict"),
                status=payload.get("status"),
            )
            if outcome.get("status") != "recorded":
                raise HTTPException(status_code=503, detail=outcome)
            return outcome

    @router.get("/memory/query-stream/{search_id}")
    async def memory_query_stream_endpoint(
        search_id: str,
        request: Request,
        brain_id: str | None = Query(default=None),
        after_seq: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        with _brain_request_scope(brain_id):
            if not fetch_search_session(search_id):
                raise HTTPException(status_code=404, detail="search_not_found")
            stream_brain_record = _resolve_brain_record(current_brain_id())

        async def event_generator() -> AsyncIterator[str]:
            with use_runtime_brain(stream_brain_record):
                last_seq = _query_stream_after_seq(after_seq, last_event_id)
                last_heartbeat = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        return
                    events = fetch_search_events(search_id, after_seq=last_seq, limit=200)
                    for event in events:
                        last_seq = int(event.get("seq") or last_seq)
                        served_event = annotate_stream_event(dict(event))
                        yield _sse_search_event_frame(served_event)
                        event_type = str(event.get("event_type") or "")
                        event_payload = dict(event.get("payload") or {})
                        if event_type == "result_ready" and not bool(event_payload.get("final_materialization_pending")):
                            return
                        if event_type in {"search_cancelled", "search_failed"}:
                            return
                    if events:
                        last_heartbeat = time.monotonic()
                    refreshed = fetch_search_session(search_id)
                    if not refreshed:
                        return
                    if refreshed.get("status") in {"blocked", "cancelled", "completed", "failed", "review_required", "superseded"} and not events:
                        if refreshed.get("status") == "cancelled":
                            return
                        if refreshed.get("result"):
                            fallback_result = _completed_search_result(search_id)
                            payload = {
                                "seq": last_seq + 1,
                                "event_type": "result_ready",
                                "payload": {
                                    "search_id": search_id,
                                    "brain_id": fallback_result.get("brain_id") or current_brain_id(),
                                    "result": fallback_result,
                                    "result_ref": search_result_ref(search_id, fallback_result),
                                    "snapshot_kind": fallback_result.get("snapshot_kind") or "final",
                                    "parent_package_revision": fallback_result.get("parent_package_revision"),
                                    "package_revision": fallback_result.get("package_revision"),
                                    "snapshot_counters": dict(fallback_result.get("snapshot_counters") or {}),
                                    "visited_current": fallback_result.get("visited_current", 0),
                                    "visited_total": fallback_result.get("visited_total", 0),
                                    "promoted": fallback_result.get("promoted", 0),
                                    "hydrated": fallback_result.get("hydrated", 0),
                                    "package": fallback_result.get("package", 0),
                                    "result_materialization_state": "finalized",
                                    "final_materialization_pending": False,
                                    "result_ready_terminal": True,
                                },
                                "created_at": utc_timestamp(),
                                "terminal": True,
                            }
                            yield _sse_search_event_frame(annotate_stream_event(payload))
                        return
                    if time.monotonic() - last_heartbeat >= 5.0:
                        yield f": heartbeat search_id={search_id} last_seq={last_seq}\n\n"
                        last_heartbeat = time.monotonic()
                    await asyncio.sleep(0.35)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.post("/memory/mcp/retrieve-context", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-context", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_context_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_context", payload)

    @router.post("/memory/mcp/retrieve-document", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-document", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_document_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        with _brain_request_scope(_payload_brain_id(payload), require_retrieval_ready=True):
            response = _run_mcp_retrieval_tool("retrieve_document", payload)
            record_mcp_evidence(
                brain_id=str(response.brain_id or current_brain_id() or ""),
                event_kind="evidence_hydrated",
                payload=_model_dump(response),
                session_id=response.search_id,
            )
            return response

    @router.post("/memory/mcp/retrieve-document-workspace", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-document-workspace", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_document_workspace_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_document_workspace", payload)

    @router.post("/memory/mcp/retrieve-project-workspace", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-project-workspace", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_project_workspace_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_project_workspace", payload)

    @router.post("/memory/mcp/retrieve-path-corridor", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-path-corridor", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_path_corridor_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_path_corridor", payload)

    @router.post("/memory/mcp/retrieve-source-trace", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-source-trace", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_source_trace_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_source_trace", payload)

    @router.post("/memory/mcp/inspect-context-package", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/inspect-context-package", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_inspect_context_package_endpoint(payload: McpInspectionRequest) -> McpToolExecutionResponse:
        return _inspect_mcp_result("inspect_context_package", payload)

    @router.post("/memory/mcp/inspect-route", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/inspect-route", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_inspect_route_endpoint(payload: McpInspectionRequest) -> McpToolExecutionResponse:
        with _brain_request_scope(_payload_brain_id(payload)):
            trace = fetch_search_trace(payload.search_id)
            if not trace:
                raise HTTPException(status_code=404, detail="search_not_found")

            originating_tool = ""
            for event in list(dict(trace).get("events") or []):
                event_payload = dict(dict(event or {}).get("payload") or {})
                candidate_tool = str(event_payload.get("tool_name") or "").strip()
                if candidate_tool:
                    originating_tool = candidate_tool
                    break
            if originating_tool == "retrieve_source_trace":
                try:
                    result = _completed_search_result(payload.search_id)
                except HTTPException:
                    result = {}
                if result:
                    output = build_mcp_retrieval_tool_output(
                        "retrieve_source_trace",
                        result,
                        include_answer_demo=payload.include_answer_demo,
                        include_raw_text=payload.include_raw_text,
                    )
                    response = McpToolExecutionResponse(**_attach_tool_brain_metadata(output))
                    record_mcp_evidence(
                        brain_id=str(response.brain_id or current_brain_id() or ""),
                        event_kind="evidence_opened",
                        payload=_model_dump(response),
                        session_id=payload.search_id,
                    )
                    return response

            output = build_mcp_route_trace_output(
                search_id=payload.search_id,
                trace=trace,
                include_debug=payload.include_debug,
            )
            response = McpToolExecutionResponse(**_attach_tool_brain_metadata(output))
            record_mcp_evidence(
                brain_id=str(response.brain_id or current_brain_id() or ""),
                event_kind="evidence_opened",
                payload=_model_dump(response),
                session_id=payload.search_id,
            )
            return response

    @router.post("/memory/mcp/inspect-path-corridor", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/inspect-path-corridor", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_inspect_path_corridor_endpoint(payload: McpInspectionRequest) -> McpToolExecutionResponse:
        return _inspect_mcp_result("inspect_path_corridor", payload)

    @router.post("/memory/mcp/inspect-memory-object", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/inspect-memory-object", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_inspect_memory_object_endpoint(payload: McpMemoryObjectInspectionRequest) -> McpToolExecutionResponse:
        with _brain_request_scope(_payload_brain_id(payload)):
            cluster = fetch_cluster_runtime(payload.node_id)
            output = build_mcp_memory_object_output(
                node_id=payload.node_id,
                cluster=cluster,
                include_debug=payload.include_debug,
            )
            response = McpToolExecutionResponse(**_attach_tool_brain_metadata(output))
            record_mcp_evidence(
                brain_id=str(response.brain_id or current_brain_id() or ""),
                event_kind="evidence_opened",
                payload={**_model_dump(response), "node_id": payload.node_id},
            )
            return response

    @router.get("/memory/brain-health")
    def memory_brain_health_endpoint(
        brain_id: str | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
        include_issue_samples: bool = Query(default=True),
    ) -> dict[str, Any]:
        with _brain_request_scope(brain_id):
            return _build_memory_brain_health_payload(limit=limit, include_issue_samples=include_issue_samples)

    @router.get("/memory/large-brain-validation")
    def memory_large_brain_validation_endpoint(
        brain_id: str | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        with _brain_request_scope(brain_id):
            health = _build_memory_brain_health_payload(limit=limit, include_issue_samples=False)
            return {
                "schema_version": "agvm.public_core_large_brain_validation.v1",
                "brain_id": current_brain_id(),
                "status": "health_preflight_available",
                "proof_scope": "public_core_health_and_benchmark_preflight",
                "health_report": health,
                "benchmark_preflight": dict(health.get("benchmark_preflight") or {}),
                "private_validator_excluded": True,
            }

    @router.post("/memory/mcp/brain-health", response_model=McpBrainHealthToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/brain-health", response_model=McpBrainHealthToolExecutionResponse, response_model_exclude_none=True)
    def mcp_brain_health_endpoint(payload: McpBrainHealthRequest) -> McpBrainHealthToolExecutionResponse:
        with _brain_request_scope(_payload_brain_id(payload)):
            report = _build_memory_brain_health_payload(
                limit=payload.limit,
                include_issue_samples=payload.include_issue_samples,
            )
            return McpBrainHealthToolExecutionResponse(
                **_attach_tool_brain_metadata(build_mcp_brain_health_output(report))
            )

    return router


def _resolve_brain_record(brain_id: str | None) -> dict[str, Any]:
    try:
        return resolve_brain_scope(str(brain_id or "").strip() or None)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_retrieval_ready(brain_record: dict[str, Any]) -> None:
    if int(brain_record.get("node_count") or 0) > 0:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "brain_bootstrap_required",
            "message": "Complete Brain Bootstrap before using Context, Search or MCP retrieval.",
            "brain_id": str(brain_record.get("brain_id") or "") or None,
        },
    )


@contextmanager
def _brain_request_scope(
    brain_id: str | None,
    *,
    require_retrieval_ready: bool = False,
) -> Iterator[dict[str, Any]]:
    brain_record = _resolve_brain_record(brain_id)
    if require_retrieval_ready:
        _require_retrieval_ready(brain_record)
    with use_runtime_brain(brain_record):
        yield brain_record


def _payload_brain_id(payload: Any) -> str | None:
    value = getattr(payload, "brain_id", None)
    return str(value or "").strip() or None


def _model_dump(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return dict(payload.model_dump(mode="python"))
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def _normalized_retrieve_request(payload: RetrieveRequest) -> RetrieveRequest:
    data = _model_dump(payload)
    data["thread_id"] = str(data.get("thread_id") or uuid.uuid4())
    data["brain_id"] = str(data.get("brain_id") or current_brain_id() or "").strip() or None
    return RetrieveRequest(**data)


def _serialize_retrieve_request(payload: RetrieveRequest) -> dict[str, Any]:
    return _model_dump(payload)


def _runtime_graph() -> dict[str, Any]:
    return fetch_graph_snapshot()


def _runtime_atlas() -> dict[str, Any]:
    return fetch_atlas()


def _attach_brain_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload or {})
    output["brain_id"] = current_brain_id()
    output["runtime_scope"] = runtime_scope_summary()
    planner_runtime = dict(output.get("planner_runtime") or {})
    planner_runtime["brain_id"] = current_brain_id()
    output["planner_runtime"] = planner_runtime
    return output


def _attach_tool_brain_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload or {})
    output["brain_id"] = current_brain_id()
    return output


def _public_search_ai_admission(payload: RetrieveRequest) -> dict[str, Any]:
    identity_nucleus = fetch_identity_nucleus()
    identity_source = search_identity_nucleus_for_named_targets(payload.query_text, identity_nucleus)
    timeout_seconds = max(
        0.1,
        min(
            95.0,
            _search_ai_admission_timeout_seconds(payload.retrieval_mode, payload.query_text)
            + _planner_seed_transport_guard_seconds()
            + 0.5,
        ),
    )
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    context = copy_context()

    def run_admission() -> None:
        try:
            result_queue.put_nowait(
                (
                    "ok",
                    require_search_ai_admission(
                        payload,
                        identity_source,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            result_queue.put_nowait(("error", exc))

    thread = threading.Thread(
        target=lambda: context.run(run_admission),
        name="agvm-core-search-ai-admission",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return {
            "schema_version": "agvm.search_ai_admission.v2",
            "status": "blocked",
            "reason": "blocked_ai_provider_timeout",
            "provider_error": f"search_ai_admission_lifecycle_timeout:{timeout_seconds:.2f}s",
            "chargeable": False,
            "charged_units": 0,
        }
    try:
        status, value = result_queue.get_nowait()
    except queue.Empty:
        return {
            "schema_version": "agvm.search_ai_admission.v2",
            "status": "blocked",
            "reason": "blocked_ai_provider_timeout",
            "provider_error": "search_ai_admission_empty_worker_result",
            "chargeable": False,
            "charged_units": 0,
        }
    if status == "error":
        raise value
    return dict(value or {})


def _search_ai_failure_code(provider_error: str | None) -> str:
    payload = build_search_ai_execution_http_block_payload(provider_error)
    return str(payload.get("code") or "blocked_ai_provider_error")


def _planning_failure_http_payload(exc: SearchAiExecutionError) -> dict[str, Any]:
    blocked = build_search_ai_execution_http_block_payload(exc.provider_error)
    search_id = str(getattr(exc, "search_id", "") or "").strip()
    if not search_id:
        return blocked
    result_ref = dict(getattr(exc, "result_ref", {}) or {})
    result_url = str(result_ref.get("endpoint") or f"/memory/query-result/{search_id}").strip()
    blocked.update(
        {
            "search_id": search_id,
            "session_created": True,
            "terminal_result_persisted": True,
            "terminal_for_client": True,
            "result_available": True,
            "result_url": result_url,
            "result_ref": result_ref or None,
            "receipt": {
                "search_id": search_id,
                "result_ref": result_ref or None,
                "terminal_state": "failed",
            },
        }
    )
    blocked["detail"] = {
        key: value
        for key, value in blocked.items()
        if key != "detail"
    }
    return blocked


def _build_planning_failed_search_result(
    search_id: str,
    request: RetrieveRequest,
    exc: SearchAiExecutionError,
    *,
    elapsed_ms: float,
) -> dict[str, Any]:
    reason = _search_ai_failure_code(exc.provider_error)
    provider_error = str(exc.provider_error or reason)
    human_message = (
        "Search planning failed because the AI spatial planner returned output that could not be used. "
        "No heuristic or keyword fallback was run, no context was returned, and no usage was charged. "
        "Retry the same query to create a fresh plan."
    )
    result = build_search_ai_blocked_result(
        request,
        {
            "schema_version": "agvm.search_ai_admission.v2",
            "status": "blocked",
            "reason": reason,
            "provider_error": provider_error,
            "chargeable": False,
            "charged_units": 0,
        },
        search_id=search_id,
    )
    result.update(
        {
            "search_id": search_id,
            "brain_id": current_brain_id(),
            "status": "failed",
            "completion_state": "planning_failed",
            "canonical_search_state": "failed",
            "stop_reason": reason,
            "answer_surface_state": "not_ready",
            "answerability_state": "insufficient",
            "closure_state": "bounded_partial",
            "final_materialization_pending": False,
            "result_ready_terminal": True,
            "terminal_for_client": True,
            "result_materialization_state": "finalized",
            "detwin_credits_charged": 0,
            "timing": {"total_ms": round(float(elapsed_ms), 2), "planning_ms": round(float(elapsed_ms), 2)},
        }
    )
    result["planning_failure"] = {
        "schema_version": "agvm.search_planning_failure.v1",
        "state": "planning_failed",
        "failed_call": exc.call_name,
        "reason": reason,
        "provider_error": provider_error,
        "message": human_message,
        "heuristic_fallback_used": False,
        "keyword_fallback_used": False,
        "new_search_created": False,
        "charged_units": 0,
    }
    result["context_package"] = {
        "status": "planning_failed",
        "agent_markdown": human_message,
        "contract": {
            "passed": False,
            "state": "planning_failed",
            "reason": reason,
        },
    }
    materialization = dict(result.get("context_package_materialization") or {})
    materialization.update(
        {
            "state": "planning_failed",
            "terminal": True,
            "terminal_for_mcp_client": True,
            "contract_passed": False,
            "final_materialization_pending": False,
        }
    )
    result["context_package_materialization"] = materialization
    completion = dict(result.get("completion_contract") or {})
    completion.update(
        {
            "state": "failed",
            "status": "failed",
            "completion_state": "planning_failed",
            "canonical_search_state": "failed",
            "visible_reason": reason,
            "operator_message": human_message,
            "result_materialization_state": "planning_failed",
            "final_materialization_pending": False,
            "result_ready_terminal": True,
        }
    )
    result["completion_contract"] = completion
    result["mcp_delivery_contract"] = {
        "schema_version": "agvm.mcp_delivery_contract.v1",
        "search_id": search_id,
        "brain_id": current_brain_id(),
        "canonical_search_state": "failed",
        "client_payload_state": "failed",
        "completion_state": "planning_failed",
        "result_materialization_state": "planning_failed",
        "terminal_for_client": True,
        "partial_for_client": False,
        "final_materialization_pending": False,
        "more_evidence_needed": False,
        "missing_reasons": [reason],
        "operator_message": human_message,
        "result_ref": search_result_ref(search_id, result),
    }
    runtime = dict(result.get("planner_runtime") or {})
    runtime.update(
        {
            "planner_path": "ai_planning_failed",
            "ai_execution_attested": False,
            "heuristic_fallback_used": False,
            "keyword_fallback_used": False,
            "failed_call": exc.call_name,
            "provider_error": provider_error,
            "planning_failure_reason": reason,
        }
    )
    result["planner_runtime"] = runtime
    return _retrieve_response_schema_safe(project_search_result_lifecycle(result, "failed"))


def _create_planned_search_session(
    payload: RetrieveRequest,
    *,
    persist_plan: bool = True,
    ai_admission: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    plan_started_at = time.perf_counter()
    normalized_payload = _normalized_retrieve_request(payload)
    search_id = str(uuid.uuid4())
    request_payload = _serialize_retrieve_request(normalized_payload)
    existing_thread_session = fetch_active_search_session_by_thread(
        str(normalized_payload.thread_id or ""),
        exclude_search_id=search_id,
    )
    if existing_thread_session and str(existing_thread_session.get("search_id") or "") != search_id:
        request_search_supersede(str(existing_thread_session.get("search_id") or ""), replacement_search_id=search_id)
    create_search_session(search_id, request_payload)
    append_search_event(
        search_id,
        "planning_started",
        {
            "query_text": normalized_payload.query_text,
            "thread_id": normalized_payload.thread_id,
            "brain_id": current_brain_id(),
            "started_at": utc_timestamp(),
        },
    )
    try:
        plan = prepare_runtime_plan(
            normalized_payload,
            _runtime_atlas(),
            fetch_identity_nucleus(),
            defer_planner_seed=True,
            ai_admission=ai_admission,
        )
    except SearchAiExecutionError as exc:
        plan_ms = round((time.perf_counter() - plan_started_at) * 1000.0, 2)
        reason = _search_ai_failure_code(exc.provider_error)
        append_search_event(
            search_id,
            "planning_failed",
            {
                "brain_id": current_brain_id(),
                "failed_call": exc.call_name,
                "reason": reason,
                "provider_error": exc.provider_error,
                "elapsed_ms": plan_ms,
                "terminal_result_persisted": True,
            },
        )
        fail_search_session(search_id, str(exc))
        terminal_result = _build_planning_failed_search_result(
            search_id,
            normalized_payload,
            exc,
            elapsed_ms=plan_ms,
        )
        persisted_terminal_result = finalize_search_session(search_id, terminal_result)
        exc.search_id = search_id
        exc.result_ref = search_result_ref(search_id, persisted_terminal_result)
        append_search_event(
            search_id,
            "result_ready",
            {
                "brain_id": current_brain_id(),
                "result": persisted_terminal_result,
                "result_ref": search_result_ref(search_id, persisted_terminal_result),
                "canonical_search_state": "failed",
                "completion_state": "planning_failed",
                "result_materialization_state": "planning_failed",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
                "terminal_for_client": True,
            },
        )
        raise
    plan_ms = round((time.perf_counter() - plan_started_at) * 1000.0, 2)
    landing_metadata = build_landing_metadata(list(plan.get("probes") or []), list(plan.get("branches") or []))
    search_map_2d_truth = build_search_map_2d_truth(
        search_id=search_id,
        thread_id=normalized_payload.thread_id,
        probes=list(plan.get("probes") or []),
        branches=list(plan.get("branches") or []),
        landing_metadata=landing_metadata,
        route_truth_summary={},
        phase="planning",
    )
    map_stream_state = {
        "schema_version": "agvm.search_map_progressive_stream.v1",
        "phase": "planning",
        "landing_count": int((search_map_2d_truth.get("metrics") or {}).get("landing_count") or 0),
        "route_plan_count": int((search_map_2d_truth.get("metrics") or {}).get("route_plan_count") or 0),
        "route_step_count": int((search_map_2d_truth.get("metrics") or {}).get("route_step_count") or 0),
        "travel_step_count": int((search_map_2d_truth.get("metrics") or {}).get("travel_event_count") or 0),
        "route_segment_count": int((search_map_2d_truth.get("metrics") or {}).get("route_segment_count") or 0),
        "ai_material_contribution": False,
        "terminal_frozen": False,
    }
    plan["landing_metadata"] = landing_metadata
    plan["route_truth_summary"] = {}
    plan["search_map_2d_truth"] = search_map_2d_truth
    plan["map_stream_state"] = map_stream_state
    plan["brain_id"] = current_brain_id()
    plan["planner_runtime"] = {
        **dict(plan.get("planner_runtime") or {}),
        "brain_id": current_brain_id(),
        "plan_ms": plan_ms,
        "landing_metadata_count": len(landing_metadata),
        "route_truth_summary": {},
        "search_map_2d_truth": search_map_2d_truth,
        "map_stream_state": map_stream_state,
        "core_retrieve_router": True,
    }
    if persist_plan:
        save_search_plan(search_id, plan)
    semantic_contract_event = dict(plan.get("semantic_contract") or {})
    planner_runtime_event = dict(plan.get("planner_runtime") or {})
    probes_event = list(plan.get("probes") or [])
    branches_event = list(plan.get("branches") or [])
    landing_metadata_event = landing_metadata
    search_map_event = search_map_2d_truth
    if not persist_plan:
        semantic_contract_event = {
            key: semantic_contract_event.get(key)
            for key in (
                "schema_version",
                "contract_version",
                "intent",
                "expected_evidence",
                "context_contract",
                "document_contract",
                "answer_contract",
                "stop_contract",
                "ai_required",
            )
            if key in semantic_contract_event
        }
        planner_runtime_event = {
            key: planner_runtime_event.get(key)
            for key in (
                "planner_mode",
                "decomposition_mode",
                "branch_count",
                "query_class",
                "retrieval_mode",
                "plan_ms",
                "runtime_stage_timing",
                "phase_timings",
                "semantic_contract_status",
                "semantic_contract_source",
                "semantic_contract_material",
            )
            if key in planner_runtime_event
        }
        planner_runtime_event["plan_transport"] = "in_memory_until_first_package"
        probes_event = []
        branches_event = []
        landing_metadata_event = []
        search_map_event = {
            "schema_version": "agvm.search_map_2d_truth.v1",
            "phase": "planning",
            "metrics": dict(search_map_2d_truth.get("metrics") or {}),
        }
    if persist_plan:
        append_search_event(
            search_id,
            "planning_complete",
            {
                "search_id": search_id,
                "thread_id": normalized_payload.thread_id,
                "brain_id": current_brain_id(),
                "query_text": normalized_payload.query_text,
                "retrieval_mode": _retrieval_mode_from_plan(plan, normalized_payload),
                "planner_mode": plan.get("planner_mode"),
                "decomposition_mode": plan.get("decomposition_mode"),
                "semantic_contract": semantic_contract_event,
                "semantic_contract_runtime": dict(plan.get("semantic_contract_runtime") or {}),
                "planner_runtime": planner_runtime_event,
                "probes": probes_event,
                "branches": branches_event,
                "landing_metadata": landing_metadata_event,
                "route_truth_summary": {},
                "search_map_2d_truth": search_map_event,
                "map_stream_state": map_stream_state,
            },
        )
    return search_id, plan


def _search_plan_response(search_id: str, payload: RetrieveRequest, plan: dict[str, Any]) -> SearchPlanResponse:
    planner_mode = str(plan.get("planner_mode") or "heuristic")
    decomposition_mode = str(plan.get("decomposition_mode") or "heuristic")
    existing_planner_runtime = dict(plan.get("planner_runtime") or {})
    planner_runtime = {
        **existing_planner_runtime,
        "planner_path": str(existing_planner_runtime.get("planner_path") or "ai_attested"),
    }
    return SearchPlanResponse(
        search_id=search_id,
        brain_id=current_brain_id(),
        thread_id=payload.thread_id,
        query_text=payload.query_text,
        response_mode=payload.response_mode,
        retrieval_mode=_retrieval_mode_from_plan(plan, payload),  # type: ignore[arg-type]
        decomposition_mode=decomposition_mode,  # type: ignore[arg-type]
        planner_mode=planner_mode,  # type: ignore[arg-type]
        semantic_contract=dict(plan.get("semantic_contract") or {}),
        semantic_contract_runtime=dict(plan.get("semantic_contract_runtime") or {}),
        metamemory_snapshot=dict(plan.get("metamemory_snapshot") or {}),
        metamemory_spatial_brief=dict(plan.get("metamemory_spatial_brief") or {}),
        metamemory_spatial_brief_summary=dict(plan.get("metamemory_spatial_brief_summary") or {}),
        metamemory_spatial_readiness=dict(plan.get("metamemory_spatial_readiness") or {}),
        ai_spatial_landing_contract=dict(plan.get("ai_spatial_landing_contract") or {}),
        ai_spatial_landing_contract_runtime=dict(plan.get("ai_spatial_landing_contract_runtime") or {}),
        path_mission_contract=dict(plan.get("path_mission_contract") or {}),
        path_missions=list(plan.get("path_missions") or []),
        mission_aware_merge_summary=dict(
            plan.get("mission_aware_merge_summary")
            or planner_runtime.get("mission_aware_merge_summary")
            or planner_runtime.get("ai_spatial_merge_summary")
            or {}
        ),
        mission_evidence_ledger=dict(plan.get("mission_evidence_ledger") or {}),
        master_judgement=dict(plan.get("master_judgement") or {}),
        mission_learning_rollup=dict(plan.get("mission_learning_rollup") or {}),
        probe_limit_reason=str(planner_runtime.get("probe_limit_reason") or "") or None,
        answer_strands=list(plan.get("answer_strands") or []),
        planner_seed_runtime=dict(plan.get("planner_seed_runtime") or {}),
        seed_goal_coverage=dict(plan.get("seed_goal_coverage") or {}),
        seed_destination_presence=dict(plan.get("seed_destination_presence") or {}),
        probes=list(plan.get("probes") or []),
        branches=list(plan.get("branches") or []),
        landing_metadata=list(plan.get("landing_metadata") or []),
        route_truth_summary=dict(plan.get("route_truth_summary") or {}),
        search_map_2d_truth=dict(plan.get("search_map_2d_truth") or {}),
        map_stream_state=dict(plan.get("map_stream_state") or {}),
        planner_runtime=planner_runtime,
    )


def _retrieval_mode_from_plan(plan: dict[str, Any], payload: RetrieveRequest | None = None) -> str:
    value = str((plan.get("planner_runtime") or {}).get("retrieval_mode") or getattr(payload, "retrieval_mode", None) or "balanced")
    return value if value in {"flash", "balanced", "heavy", "forensic"} else "balanced"


def _run_search_session_sync(
    search_id: str,
    *,
    prepared_plan: dict[str, Any] | None = None,
    prepared_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if prepared_request is None:
        session = fetch_search_session(search_id)
        if not session:
            raise HTTPException(status_code=404, detail="search_not_found")
        request_payload = dict(session.get("request") or {})
    else:
        session = {}
        request_payload = dict(prepared_request)
    query = _normalized_retrieve_request(RetrieveRequest(**request_payload))
    search_brain_id = str(
        request_payload.get("brain_id") or query.brain_id or current_brain_id() or ""
    ).strip() or None
    atlas_payload = _runtime_atlas()
    identity_nucleus = fetch_identity_nucleus()
    plan = (
        dict(prepared_plan or {})
        or dict(session.get("plan") or {})
        or prepare_runtime_plan(query, atlas_payload, identity_nucleus, defer_planner_seed=True)
    )
    plan["brain_id"] = current_brain_id()
    plan["planner_runtime"] = {
        **dict(plan.get("planner_runtime") or {}),
        "brain_id": current_brain_id(),
        "core_retrieve_router": True,
    }
    plan_admission = dict(plan.get("search_ai_admission") or {})
    if str(plan_admission.get("status") or "") != "admitted":
        blocked = _attach_brain_metadata(
            normalize_retrieve_response_payload(
                build_search_ai_blocked_result(
                    query,
                    {
                        "status": "blocked",
                        "reason": "blocked_ai_attestation_invalid",
                        "provider_error": "search_ai_admission_missing",
                        "chargeable": False,
                        "charged_units": 0,
                    },
                    search_id=search_id,
                )
            )
        )
        blocked = _attach_mcp_surface_fields(
            blocked,
            tool_name=_tool_name_for_session(request_payload, plan),
        )
        finalize_search_session(search_id, blocked)
        append_search_event(
            search_id,
            "search_failed",
            {
                "brain_id": current_brain_id(),
                "error": "search_ai_admission_missing",
                "charged_units": 0,
            },
        )
        return blocked
    if prepared_plan is None and prepared_request is None:
        save_search_plan(search_id, plan)
    mark_search_running(search_id)
    append_search_event(
        search_id,
        "worker_started",
        {"search_id": search_id, "brain_id": current_brain_id(), "started_at": utc_timestamp()},
    )

    def append_lifecycle_event(event_type: str, event_payload: Mapping[str, Any] | None) -> None:
        payload = {
            **dict(event_payload or {}),
            "brain_id": search_brain_id,
        }
        if search_brain_id and search_brain_id != current_brain_id():
            # EvidenceJudge, MasterJudge, and final-materialization heartbeats
            # execute in raw threads created below the router boundary.  Those
            # threads do not inherit ContextVars, so always re-enter the
            # request's authoritative brain before touching the Search store.
            with _brain_request_scope(search_brain_id):
                append_search_event(search_id, event_type, payload)
            return
        append_search_event(search_id, event_type, payload)

    def persist_lifecycle_event(event_type: str, event_payload: dict[str, Any]) -> None:
        if search_brain_id and search_brain_id != current_brain_id():
            with _brain_request_scope(search_brain_id):
                persist_lifecycle_event(
                    event_type,
                    {**dict(event_payload or {}), "brain_id": search_brain_id},
                )
            return
        append_lifecycle_event(event_type, event_payload)
        lifecycle_payload = {
            **dict(event_payload or {}),
            "brain_id": search_brain_id,
        }
        FINAL_MATERIALIZATION_HEARTBEATS.after_event(
            search_id,
            event_type,
            lifecycle_payload,
            persist_heartbeat=lambda heartbeat_type, heartbeat_payload: append_lifecycle_event(
                heartbeat_type,
                heartbeat_payload,
            ),
            persist_diagnostic=lambda diagnostic_type, diagnostic_payload: append_lifecycle_event(
                diagnostic_type,
                diagnostic_payload,
            ),
        )

    try:
        def persist_runtime_event(event_type: str, event_payload: dict[str, Any]) -> None:
            if search_brain_id and search_brain_id != current_brain_id():
                with _brain_request_scope(search_brain_id):
                    persist_runtime_event(
                        event_type,
                        {**dict(event_payload or {}), "brain_id": search_brain_id},
                    )
                return
            if event_type == "context_update":
                snapshot = _mcp_snapshot_from_context_update(
                    search_id=search_id,
                    request=query,
                    plan=plan,
                    event_payload=event_payload,
                )
            elif event_type == "result_snapshot_ready":
                snapshot = dict(event_payload.get("result_snapshot") or event_payload.get("result") or {})
            else:
                persist_lifecycle_event(event_type, event_payload)
                return
            if not snapshot:
                persist_lifecycle_event(event_type, event_payload)
                return
            snapshot = _attach_brain_metadata(
                normalize_retrieve_response_payload({**snapshot, "search_id": search_id})
            )
            snapshot = _attach_mcp_surface_fields(
                snapshot,
                tool_name=_tool_name_for_session(request_payload, plan),
            )
            persisted = save_search_result_snapshot(search_id, snapshot) or snapshot
            enriched_event_payload = {
                **dict(event_payload or {}),
                "search_id": search_id,
                "brain_id": persisted.get("brain_id") or current_brain_id(),
                "snapshot_kind": persisted.get("snapshot_kind"),
                "parent_package_revision": persisted.get("parent_package_revision"),
                "package_revision": persisted.get("package_revision"),
                "snapshot_counters": dict(persisted.get("snapshot_counters") or {}),
                "visited_current": persisted.get("visited_current", 0),
                "visited_total": persisted.get("visited_total", 0),
                "promoted": persisted.get("promoted", 0),
                "hydrated": persisted.get("hydrated", 0),
                "package": persisted.get("package", 0),
                "result_ref": search_result_ref(search_id, persisted),
            }
            if event_type == "result_snapshot_ready":
                enriched_event_payload["result_snapshot"] = persisted
            persist_lifecycle_event(event_type, enriched_event_payload)
            _publish_mcp_first_package(search_id, persisted)

        result = retrieve_runtime(
            query,
            atlas_payload,
            identity_nucleus,
            prepared_plan=plan,
            search_id=search_id,
            event_callback=persist_runtime_event,
        )
        authoritative_runtime_result = dict(result or {})
        result = _attach_brain_metadata(
            normalize_retrieve_response_payload({**dict(result or {}), "search_id": search_id})
        )
        result = _attach_mcp_surface_fields(result, tool_name=_tool_name_for_session(request_payload, plan))
        # MCP surface construction is a renderer/transport adapter, not a
        # second sufficiency judge. Reapply the already validated bounded
        # Master partial from its untouched runtime snapshot so score/DTO
        # normalization cannot invalidate the ledger digest it judged.
        result, _ = _apply_plan_first_usable_partial_public_projection(
            result,
            master_judgement=dict(
                authoritative_runtime_result.get("master_judgement") or {}
            ),
            plan_first_runtime=dict(
                dict(authoritative_runtime_result.get("planner_runtime") or {}).get(
                    "plan_first_runtime"
                )
                or {}
            ),
            mission_evidence_ledger=dict(
                authoritative_runtime_result.get("mission_evidence_ledger") or {}
            ),
        )
        result = _retrieve_response_schema_safe(
            project_search_result_lifecycle(result, str(result.get("status") or "completed"))
        )
        if prepared_plan is not None or prepared_request is not None:
            # The MCP path starts traversal from the in-memory plan so large-plan
            # serialization cannot delay its first useful context package.
            save_search_plan(search_id, _mcp_plan_storage_snapshot(plan))
        result = finalize_search_session(search_id, result)
        persist_lifecycle_event(
            "result_ready",
            {
                "search_id": search_id,
                "brain_id": current_brain_id(),
                "result": result,
                "result_materialization_state": "finalized",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
            },
        )
        return result
    except Exception as exc:  # noqa: BLE001
        persist_lifecycle_event(
            "search_failed",
            {"brain_id": current_brain_id(), "error": str(exc)},
        )
        fail_search_session(search_id, str(exc))
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        FINAL_MATERIALIZATION_HEARTBEATS.stop(search_id)


def _mcp_snapshot_from_context_update(
    *,
    search_id: str,
    request: RetrieveRequest,
    plan: dict[str, Any],
    event_payload: dict[str, Any],
) -> dict[str, Any]:
    context = dict(event_payload.get("context") or {})
    matches = [dict(item) for item in list(event_payload.get("matches") or []) if isinstance(item, dict)]
    document_packets = [
        dict(item)
        for item in list(event_payload.get("document_packets") or event_payload.get("supporting_documents") or [])
        if isinstance(item, dict)
    ]
    if not context and not matches and not document_packets:
        return {}

    semantic_contract = dict(event_payload.get("semantic_contract") or plan.get("semantic_contract") or {})
    planner_runtime = {
        **dict(plan.get("planner_runtime") or {}),
        **dict(event_payload.get("planner_runtime") or {}),
        "nonblocking_first_package_returned": True,
        "first_package_source": "runtime_context_update",
        "http_response_policy": "first_package_with_background_completion",
    }
    context_package = build_mcp_context_package(
        query_text=request.query_text,
        context=context,
        context_structured=context,
        matches=matches,
        evidence_reservoir=dict(event_payload.get("evidence_reservoir") or {}),
        document_packets=document_packets,
        semantic_contract=semantic_contract,
        retrieval_mode=str(request.retrieval_mode or "balanced"),
        path_corridors=dict(event_payload.get("path_corridors") or {}),
        path_truth_required=bool(request.complete_paths),
        document_workspace=dict(event_payload.get("document_workspace") or {}),
        package_mode=request.context_package_mode,
        document_text_policy=request.document_text_policy,
        mission_evidence_ledger=dict(
            event_payload.get("mission_evidence_ledger")
            or plan.get("mission_evidence_ledger")
            or {}
        ),
    )
    materialization = {
        "schema_version": "agvm.context_package_materialization.v1",
        "state": "first_useful_package_ready",
        "source": "runtime_context_update",
        "terminal": False,
        "terminal_for_mcp_client": True,
        "final_materialization_pending": True,
        "contract_passed": str(context_package.get("status") or "") in {"ready", "ok", "partial"},
    }
    return {
        **dict(event_payload or {}),
        "search_id": search_id,
        "query_text": request.query_text,
        "thread_id": request.thread_id,
        "response_mode": request.response_mode,
        "retrieval_mode": request.retrieval_mode,
        "document_text_policy": request.document_text_policy,
        "semantic_contract": semantic_contract,
        "semantic_contract_runtime": dict(
            event_payload.get("semantic_contract_runtime")
            or plan.get("semantic_contract_runtime")
            or {}
        ),
        "planner_runtime": planner_runtime,
        "context_package": context_package,
        "context_package_materialization": materialization,
        "matches": matches,
        "document_packets": document_packets,
        "result_materialization_state": "first_package_ready_background_running",
        "final_materialization_pending": True,
        "result_ready_terminal": False,
        "stop_reason": "first_useful_context_package_ready_background_running",
        "answerability_state": "grounded" if matches else "insufficient",
        "closure_state": "open",
        "final_closure_ready": False,
    }


def _attach_mcp_surface_fields(result: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    payload = _attach_brain_metadata(normalize_retrieve_response_payload(result))
    include_raw_text = str(payload.get("document_text_policy") or "") in {"top_raw", "all_raw"}
    include_answer_demo = str(payload.get("response_mode") or "") in {"answer", "both"}
    try:
        output = build_mcp_retrieval_tool_output(
            tool_name,
            payload,
            include_answer_demo=include_answer_demo,
            include_raw_text=include_raw_text,
        )
    except Exception as exc:  # noqa: BLE001
        planner_runtime = dict(payload.get("planner_runtime") or {})
        planner_runtime["mcp_surface_contract_error"] = str(exc)
        payload["planner_runtime"] = planner_runtime
        return payload
    for key in _MCP_SURFACE_FIELDS:
        if key in output:
            payload[key] = output[key]
    context_package = dict(payload.get("context_package") or {})
    if context_package:
        if payload.get("document_ref_contract"):
            context_package["document_ref_contract"] = dict(payload.get("document_ref_contract") or {})
        if payload.get("document_delivery_contract"):
            context_package["document_delivery_contract"] = dict(payload.get("document_delivery_contract") or {})
        payload["context_package"] = context_package
    return payload


def _register_mcp_first_package_waiter(search_id: str) -> threading.Event:
    signal = threading.Event()
    with _MCP_FIRST_PACKAGE_LOCK:
        _MCP_FIRST_PACKAGE_SIGNALS[search_id] = signal
        _MCP_FIRST_PACKAGE_CACHE.pop(search_id, None)
    return signal


def _publish_mcp_first_package(search_id: str, snapshot: dict[str, Any]) -> None:
    if not _mcp_first_package_ready(snapshot):
        return
    with _MCP_FIRST_PACKAGE_LOCK:
        signal = _MCP_FIRST_PACKAGE_SIGNALS.get(search_id)
        if signal is None:
            return
        _MCP_FIRST_PACKAGE_CACHE[search_id] = dict(snapshot)
        signal.set()


def _consume_mcp_first_package(search_id: str) -> dict[str, Any]:
    with _MCP_FIRST_PACKAGE_LOCK:
        _MCP_FIRST_PACKAGE_SIGNALS.pop(search_id, None)
        return dict(_MCP_FIRST_PACKAGE_CACHE.pop(search_id, {}) or {})


def _mcp_plan_storage_snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    planner_runtime = dict(plan.get("planner_runtime") or {})
    compact_planner_runtime = {
        key: planner_runtime.get(key)
        for key in (
            "planner_mode",
            "decomposition_mode",
            "branch_count",
            "planner_path",
            "probe_limit_reason",
            "query_class",
            "retrieval_mode",
            "retrieval_mode_selected_by",
            "plan_ms",
            "runtime_stage_timing",
            "phase_timings",
            "semantic_contract_status",
            "semantic_contract_source",
            "semantic_contract_material",
            "semantic_contract_cache_status",
            "semantic_contract_cache_hit",
        )
        if key in planner_runtime
    }
    compact_planner_runtime["plan_transport"] = "in_memory_first_package_then_compact_persistence"
    return {
        "brain_id": plan.get("brain_id"),
        "search_ai_admission": dict(plan.get("search_ai_admission") or {}),
        "ai_execution_attestation": dict(plan.get("ai_execution_attestation") or {}),
        "planner_mode": plan.get("planner_mode"),
        "decomposition_mode": plan.get("decomposition_mode"),
        "stop_threshold": plan.get("stop_threshold"),
        "probes": [dict(item) for item in list(plan.get("probes") or []) if isinstance(item, dict)],
        "branches": [dict(item) for item in list(plan.get("branches") or []) if isinstance(item, dict)],
        "landing_metadata": [
            dict(item) for item in list(plan.get("landing_metadata") or []) if isinstance(item, dict)
        ],
        "route_truth_summary": dict(plan.get("route_truth_summary") or {}),
        "search_map_2d_truth": dict(plan.get("search_map_2d_truth") or {}),
        "map_stream_state": dict(plan.get("map_stream_state") or {}),
        "planner_runtime": compact_planner_runtime,
    }


def _start_search_thread(
    search_id: str,
    brain_record: dict[str, Any],
    *,
    prepared_plan: dict[str, Any] | None = None,
    prepared_request: dict[str, Any] | None = None,
) -> None:
    with _SEARCH_THREAD_LOCK:
        for existing_search_id, existing_thread in list(_SEARCH_THREADS.items()):
            if not existing_thread.is_alive():
                _SEARCH_THREADS.pop(existing_search_id, None)
        existing = _SEARCH_THREADS.get(search_id)
        if existing and existing.is_alive():
            return

        def worker() -> None:
            try:
                with use_runtime_brain(brain_record):
                    _run_search_session_sync(
                        search_id,
                        prepared_plan=prepared_plan,
                        prepared_request=prepared_request,
                    )
            finally:
                with _SEARCH_THREAD_LOCK:
                    _SEARCH_THREADS.pop(search_id, None)

        thread = threading.Thread(target=worker, name=f"agvm-core-query-{search_id}", daemon=True)
        _SEARCH_THREADS[search_id] = thread
        thread.start()


def _search_session_status(search_id: str) -> str:
    try:
        session = fetch_search_session(search_id, busy_timeout_ms=45, return_on_busy=True) or {}
    except Exception:  # noqa: BLE001
        return ""
    return str(session.get("status") or "").strip().lower()


def _prepare_accepted_search_plan(
    search_id: str,
    payload: RetrieveRequest,
    *,
    ai_admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan_started_at = time.perf_counter()
    try:
        plan = prepare_runtime_plan(
            payload,
            _runtime_atlas(),
            fetch_identity_nucleus(),
            defer_planner_seed=True,
            ai_admission=ai_admission,
        )
    except SearchAiExecutionError as exc:
        plan_ms = round((time.perf_counter() - plan_started_at) * 1000.0, 2)
        reason = _search_ai_failure_code(exc.provider_error)
        append_search_event(
            search_id,
            "planning_failed",
            {
                "brain_id": current_brain_id(),
                "failed_call": exc.call_name,
                "reason": reason,
                "provider_error": exc.provider_error,
                "elapsed_ms": plan_ms,
                "terminal_result_persisted": True,
                "plan_transport": "early_search_identity_background_plan",
            },
        )
        fail_search_session(search_id, str(exc))
        terminal_result = _build_planning_failed_search_result(
            search_id,
            payload,
            exc,
            elapsed_ms=plan_ms,
        )
        persisted_terminal_result = finalize_search_session(search_id, terminal_result)
        exc.search_id = search_id
        exc.result_ref = search_result_ref(search_id, persisted_terminal_result)
        append_search_event(
            search_id,
            "result_ready",
            {
                "brain_id": current_brain_id(),
                "result": persisted_terminal_result,
                "result_ref": search_result_ref(search_id, persisted_terminal_result),
                "canonical_search_state": "failed",
                "completion_state": "planning_failed",
                "result_materialization_state": "planning_failed",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
                "terminal_for_client": True,
                "plan_transport": "early_search_identity_background_plan",
            },
        )
        raise
    if _search_session_status(search_id) == "cancelled":
        return {}
    plan_ms = round((time.perf_counter() - plan_started_at) * 1000.0, 2)
    landing_metadata = build_landing_metadata(list(plan.get("probes") or []), list(plan.get("branches") or []))
    search_map_2d_truth = build_search_map_2d_truth(
        search_id=search_id,
        thread_id=payload.thread_id,
        probes=list(plan.get("probes") or []),
        branches=list(plan.get("branches") or []),
        landing_metadata=landing_metadata,
        route_truth_summary={},
        phase="planning",
    )
    map_stream_state = {
        "schema_version": "agvm.search_map_progressive_stream.v1",
        "phase": "planning",
        "landing_count": int((search_map_2d_truth.get("metrics") or {}).get("landing_count") or 0),
        "route_plan_count": int((search_map_2d_truth.get("metrics") or {}).get("route_plan_count") or 0),
        "route_step_count": int((search_map_2d_truth.get("metrics") or {}).get("route_step_count") or 0),
        "travel_step_count": int((search_map_2d_truth.get("metrics") or {}).get("travel_event_count") or 0),
        "route_segment_count": int((search_map_2d_truth.get("metrics") or {}).get("route_segment_count") or 0),
        "ai_material_contribution": False,
        "terminal_frozen": False,
    }
    plan["landing_metadata"] = landing_metadata
    plan["route_truth_summary"] = {}
    plan["search_map_2d_truth"] = search_map_2d_truth
    plan["map_stream_state"] = map_stream_state
    plan["brain_id"] = current_brain_id()
    plan["planner_runtime"] = {
        **dict(plan.get("planner_runtime") or {}),
        "brain_id": current_brain_id(),
        "plan_ms": plan_ms,
        "landing_metadata_count": len(landing_metadata),
        "route_truth_summary": {},
        "search_map_2d_truth": search_map_2d_truth,
        "map_stream_state": map_stream_state,
        "core_retrieve_router": True,
        "plan_transport": "early_search_identity_background_plan",
    }
    save_search_plan(search_id, plan)
    append_search_event(
        search_id,
        "planning_complete",
        {
            "search_id": search_id,
            "thread_id": payload.thread_id,
            "brain_id": current_brain_id(),
            "query_text": payload.query_text,
            "retrieval_mode": _retrieval_mode_from_plan(plan, payload),
            "planner_mode": plan.get("planner_mode"),
            "decomposition_mode": plan.get("decomposition_mode"),
            "semantic_contract": dict(plan.get("semantic_contract") or {}),
            "semantic_contract_runtime": dict(plan.get("semantic_contract_runtime") or {}),
            "planner_runtime": dict(plan.get("planner_runtime") or {}),
            "probes": list(plan.get("probes") or []),
            "branches": list(plan.get("branches") or []),
            "landing_metadata": landing_metadata,
            "route_truth_summary": {},
            "search_map_2d_truth": search_map_2d_truth,
            "map_stream_state": map_stream_state,
            "plan_transport": "early_search_identity_background_plan",
        },
    )
    return plan


def _start_accepted_search_pipeline(
    search_id: str,
    brain_record: dict[str, Any],
    *,
    ai_admission: dict[str, Any] | None = None,
) -> None:
    with _SEARCH_THREAD_LOCK:
        for existing_search_id, existing_thread in list(_SEARCH_THREADS.items()):
            if not existing_thread.is_alive():
                _SEARCH_THREADS.pop(existing_search_id, None)
        existing = _SEARCH_THREADS.get(search_id)
        if existing and existing.is_alive():
            return

        def worker() -> None:
            try:
                with use_runtime_brain(brain_record):
                    session = fetch_search_session(search_id)
                    if not session:
                        return
                    request_payload = dict(session.get("request") or {})
                    request = _normalized_retrieve_request(RetrieveRequest(**request_payload))
                    admission = ai_admission or _public_search_ai_admission(request)
                    if str(admission.get("status") or "") != "admitted":
                        result = _attach_brain_metadata(
                            normalize_retrieve_response_payload(
                                build_search_ai_blocked_result(request, admission, search_id=search_id)
                            )
                        )
                        result = _attach_mcp_surface_fields(
                            result,
                            tool_name=_tool_name_for_session(request_payload, {}),
                        )
                        final_result = finalize_search_session(search_id, result)
                        append_search_event(
                            search_id,
                            "search_failed",
                            {
                                "brain_id": current_brain_id(),
                                "error": str(admission.get("reason") or "search_ai_admission_missing"),
                                "charged_units": 0,
                            },
                        )
                        append_search_event(
                            search_id,
                            "result_ready",
                            {
                                "search_id": search_id,
                                "brain_id": current_brain_id(),
                                "result": final_result,
                                "result_ref": search_result_ref(search_id, final_result),
                                "canonical_search_state": "failed",
                                "completion_state": "planning_failed",
                                "result_materialization_state": "planning_failed",
                                "final_materialization_pending": False,
                                "result_ready_terminal": True,
                                "terminal_for_client": True,
                            },
                        )
                        return
                    plan = _prepare_accepted_search_plan(search_id, request, ai_admission=admission)
                    if not plan or _search_session_status(search_id) == "cancelled":
                        return
                    _run_search_session_sync(
                        search_id,
                        prepared_plan=plan,
                        prepared_request=_serialize_retrieve_request(request),
                    )
            except SearchAiExecutionError:
                return
            except Exception as exc:  # noqa: BLE001
                append_search_event(
                    search_id,
                    "search_failed",
                    {"brain_id": current_brain_id(), "error": str(exc)},
                )
                fail_search_session(search_id, str(exc))
            finally:
                with _SEARCH_THREAD_LOCK:
                    current = _SEARCH_THREADS.get(search_id)
                    if current is threading.current_thread() or (current and not current.is_alive()):
                        _SEARCH_THREADS.pop(search_id, None)

        thread = threading.Thread(target=worker, name=f"agvm-core-query-accepted-{search_id}", daemon=True)
        _SEARCH_THREADS[search_id] = thread
        thread.start()


def _mcp_retrieve_request(tool_name: str, payload: McpRetrievalToolRequest) -> RetrieveRequest:
    query_text = payload.query_text
    if tool_name == "retrieve_document":
        document_target = payload.document_hint or payload.document_id or payload.query_text
        query_text = f"Recupera documenti e materiale sorgente relativo a: {document_target}"
        if payload.document_id and payload.document_id not in query_text:
            query_text = f"{query_text}\nDocument id MCP: {payload.document_id}"
        if payload.document_hint and payload.query_text and payload.query_text.lower() not in payload.document_hint.lower():
            query_text = f"{query_text}\nRichiesta MCP: {payload.query_text}"
    elif tool_name in {"retrieve_document_workspace", "retrieve_project_workspace"}:
        workspace_target = payload.document_hint or payload.document_id or payload.query_text
        query_text = str(workspace_target or payload.query_text or "").strip()
        if payload.document_id and payload.document_id not in query_text:
            query_text = f"{query_text}\nDocument id MCP: {payload.document_id}"
        if payload.document_hint and payload.query_text and payload.query_text.lower() not in payload.document_hint.lower():
            query_text = f"{query_text}\nRichiesta MCP: {payload.query_text}"
    elif payload.document_hint and payload.document_hint.lower() not in payload.query_text.lower():
        query_text = f"{payload.query_text} {payload.document_hint}"

    context_package_mode = payload.context_package_mode
    if context_package_mode is None:
        if tool_name == "retrieve_document":
            context_package_mode = "document_full"
        elif tool_name in {"retrieve_document_workspace", "retrieve_project_workspace"}:
            context_package_mode = "broad_dossier"
        elif tool_name in {"retrieve_path_corridor", "retrieve_source_trace"}:
            context_package_mode = "forensic_trace"
        else:
            context_package_mode = "mcp_operational"

    return RetrieveRequest(
        brain_id=payload.brain_id,
        query_text=query_text,
        thread_id=payload.thread_id,
        mcp_tool_name=tool_name,
        response_mode="both" if payload.include_answer_demo else "context",
        retrieval_mode=payload.retrieval_mode,
        context_package_mode=context_package_mode,
        document_text_policy=(
            "top_raw"
            if payload.include_raw_text and payload.document_text_policy == "refs_only"
            else payload.document_text_policy
        ),
        document_id=payload.document_id,
        complete_paths=bool(payload.complete_paths or tool_name == "retrieve_path_corridor"),
        max_matches=payload.max_matches,
    )


def _create_unplanned_search_session(
    payload: RetrieveRequest,
    *,
    plan_transport: str = "background_after_index_first_package",
) -> str:
    normalized_payload = _normalized_retrieve_request(payload)
    search_id = str(uuid.uuid4())
    request_payload = _serialize_retrieve_request(normalized_payload)
    existing_thread_session = fetch_active_search_session_by_thread(
        str(normalized_payload.thread_id or ""),
        exclude_search_id=search_id,
    )
    if existing_thread_session and str(existing_thread_session.get("search_id") or "") != search_id:
        request_search_supersede(str(existing_thread_session.get("search_id") or ""), replacement_search_id=search_id)
    create_search_session(search_id, request_payload)
    append_search_event(
        search_id,
        "planning_started",
        {
            "query_text": normalized_payload.query_text,
            "thread_id": normalized_payload.thread_id,
            "brain_id": current_brain_id(),
            "started_at": utc_timestamp(),
            "plan_transport": plan_transport,
        },
    )
    return search_id


_MCP_INDEX_STOP_TERMS = {
    "about",
    "after",
    "alla",
    "alle",
    "anche",
    "before",
    "come",
    "con",
    "cosa",
    "dalla",
    "delle",
    "does",
    "for",
    "from",
    "how",
    "into",
    "nella",
    "per",
    "quale",
    "sono",
    "that",
    "the",
    "this",
    "what",
    "when",
    "which",
    "with",
}


def _mcp_index_query_terms(query_text: str) -> list[str]:
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", str(query_text or "").lower()):
        if raw in _MCP_INDEX_STOP_TERMS:
            continue
        candidates = [raw]
        for suffix in ("ing", "ed", "es", "s", "e"):
            if raw.endswith(suffix) and len(raw) - len(suffix) >= 5:
                candidates.append(raw[: -len(suffix)])
                break
        for candidate in candidates:
            if candidate and candidate not in terms:
                terms.append(candidate)
            if len(terms) >= 16:
                return terms
    return terms


def _mcp_index_complete_summary(node: dict[str, Any], *, limit: int = 720) -> str:
    summary = " ".join(str(node.get("summary") or "").split()).strip()
    if summary and not re.search(r"(?:\.\.\.|…)\s*$", summary):
        return summary[:limit].strip()
    source = " ".join(str(node.get("raw_text") or "").split()).strip()
    if not source:
        return ""
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", source)
        if sentence.strip()
    ]
    selected: list[str] = []
    selected_length = 0
    for sentence in sentences:
        if not re.search(r"[.!?]\s*$", sentence):
            continue
        projected_length = selected_length + len(sentence) + (1 if selected else 0)
        if projected_length > limit:
            break
        selected.append(sentence)
        selected_length = projected_length
    return " ".join(selected).strip()


def _mcp_index_first_package(
    *,
    search_id: str,
    request: RetrieveRequest,
) -> dict[str, Any]:
    query_terms = _mcp_index_query_terms(request.query_text)
    if not query_terms:
        return {}
    nodes = fetch_nodes_by_text_terms(
        query_terms,
        limit=int(request.max_matches),
        include_raw_text=True,
        busy_timeout_ms=500,
    )
    if not nodes:
        return {}
    folded_query_terms = [term.lower() for term in query_terms]
    matches: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        node_id = str(node.get("id") or node.get("node_id") or "").strip()
        summary = _mcp_index_complete_summary(node)
        if not node_id or not summary:
            continue
        safe_node = {**dict(node), "raw_text": "", "summary": summary}
        haystack = " ".join(
            str(value or "")
            for value in (
                node.get("raw_text"),
                summary,
                node.get("source_unit_title"),
                dict(node.get("provenance") or {}).get("source_label"),
            )
        ).lower()
        hit_count = sum(1 for term in folded_query_terms if term in haystack)
        lexical_fit = hit_count / max(1, len(folded_query_terms))
        confidence = max(float(node.get("memory_confidence") or 0.0), float(node.get("evidence_confidence") or 0.0))
        score = round(max(0.08, min(1.0, 0.72 * lexical_fit + 0.22 * confidence + 0.06 / (index + 1))), 6)
        matches.append(
            {
                "node_id": node_id,
                "summary": summary,
                "score": score,
                "raw_score": score,
                "identifier_exact_fit": 0.0,
                "probe_id": "index_first_package",
                "label": "Index first package",
                "reason": "bounded_summary_text_index",
                "sources": ["summary_text_index"],
                "evidence_snippet": summary,
                "node": safe_node,
                "goal": "knowledge",
                "answer_slot": "knowledge",
                "support_slot": "knowledge",
                "support_slots": ["knowledge"],
            }
        )
    if not matches:
        return {}
    matches.sort(key=lambda item: (-float(item.get("raw_score") or 0.0), str(item.get("node_id") or "")))
    context = build_context_payload(matches, query_text=request.query_text)
    semantic_contract = build_query_contract(
        request.query_text,
        retrieval_mode=str(request.retrieval_mode or "balanced"),
    )
    context_package = build_mcp_context_package(
        query_text=request.query_text,
        context=context,
        context_structured=context,
        matches=matches,
        evidence_reservoir={},
        document_packets=[],
        semantic_contract=semantic_contract,
        retrieval_mode=str(request.retrieval_mode or "balanced"),
        package_mode=request.context_package_mode,
        document_text_policy=request.document_text_policy,
    )
    snapshot = {
        "search_id": search_id,
        "query_text": request.query_text,
        "thread_id": request.thread_id,
        "response_mode": request.response_mode,
        "retrieval_mode": request.retrieval_mode,
        "document_text_policy": request.document_text_policy,
        "semantic_contract": semantic_contract,
        "semantic_contract_runtime": {
            "schema_version": "agvm.semantic_contract_runtime.v1",
            "status": "deterministic_first_package",
            "source": "deterministic_query_contract",
            "material": False,
        },
        "planner_runtime": {
            "nonblocking_first_package_returned": True,
            "first_package_source": "bounded_summary_text_index",
            "index_term_count": len(query_terms),
            "index_match_count": len(matches),
            "spatial_enrichment_pending": True,
            "http_response_policy": "index_first_package_with_full_background_traversal",
        },
        "context": context,
        "context_package": context_package,
        "context_package_materialization": {
            "schema_version": "agvm.context_package_materialization.v1",
            "state": "first_useful_package_ready",
            "source": "bounded_summary_text_index",
            "terminal": False,
            "terminal_for_mcp_client": True,
            "final_materialization_pending": True,
            "contract_passed": str(context_package.get("status") or "") in {"ready", "ok", "partial"},
        },
        "budget": {
            "max_matches": int(request.max_matches),
            "candidate_limit": min(128, max(24, int(request.max_matches) * 2)),
        },
        "matches": matches,
        "result_materialization_state": "first_package_ready_background_running",
        "final_materialization_pending": True,
        "result_ready_terminal": False,
        "stop_reason": "index_first_package_ready_full_traversal_background_running",
        "answerability_state": "grounded",
        "closure_state": "open",
        "final_closure_ready": False,
    }
    return snapshot if _mcp_first_package_ready(snapshot) else {}


def _run_mcp_retrieval_tool(tool_name: str, payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
    with _brain_request_scope(_payload_brain_id(payload), require_retrieval_ready=True) as brain_record:
        request = _mcp_retrieve_request(tool_name, payload)
        if tool_name == "retrieve_document" and (
            payload.include_raw_text or payload.document_text_policy in {"top_raw", "all_raw"}
        ):
            direct_response = _core_direct_document_response(payload=payload, request=request)
            if direct_response is not None:
                direct_payload = _model_dump(direct_response)
                direct_payload["semantic_contract_runtime"] = {
                    **dict(direct_payload.get("semantic_contract_runtime") or {}),
                    "schema_version": "agvm.semantic_contract_runtime.v2",
                    "status": "completed",
                    "source": "direct_document_lookup",
                    "material": True,
                    "ai_required": False,
                    "provider_state": "not_required",
                    "fallback_used": False,
                }
                return McpToolExecutionResponse(**direct_payload)
        admission = _public_search_ai_admission(request)
        if str(admission.get("status") or "") != "admitted":
            result = _attach_brain_metadata(
                normalize_retrieve_response_payload(
                    build_search_ai_blocked_result(request, admission)
                )
            )
            output = build_mcp_retrieval_tool_output(
                tool_name,
                result,
                include_answer_demo=payload.include_answer_demo,
                include_raw_text=payload.include_raw_text,
            )
            return McpToolExecutionResponse(**_attach_tool_brain_metadata(output))
        if tool_name == "retrieve_document" and (
            payload.include_raw_text or payload.document_text_policy in {"top_raw", "all_raw"}
        ):
            direct_response = _core_direct_document_response(payload=payload, request=request)
            if direct_response is not None:
                direct_payload = _model_dump(direct_response)
                direct_payload["semantic_contract_runtime"] = {
                    **dict(direct_payload.get("semantic_contract_runtime") or {}),
                    "schema_version": "agvm.semantic_contract_runtime.v2",
                    "status": "completed",
                    "source": "search_ai_admission_v2",
                    "material": True,
                    "ai_required": True,
                    "provider_state": "attested",
                    "fallback_used": False,
                    "ai_execution_attestation": dict(
                        admission.get("ai_execution_attestation") or {}
                    ),
                }
                return McpToolExecutionResponse(**direct_payload)
        search_id, plan = _create_planned_search_session(
            request,
            persist_plan=False,
            ai_admission=admission,
        )
        first_package_signal = _register_mcp_first_package_waiter(search_id)
        plan["mcp_tool_name"] = tool_name
        plan["planner_runtime"] = {
            **dict(plan.get("planner_runtime") or {}),
            "mcp_tool_name": tool_name,
            "mcp_entrypoint_tool": tool_name,
            "core_retrieve_router": True,
        }
        _start_search_thread(
            search_id,
            brain_record,
            prepared_plan=plan,
            prepared_request=_serialize_retrieve_request(request),
        )
        wait_seconds = _MCP_FIRST_PACKAGE_WAIT_SECONDS.get(str(payload.retrieval_mode or "balanced"), 4.8)
        first_package_signal.wait(timeout=wait_seconds)
        result = _consume_mcp_first_package(search_id)
        if not result:
            session = fetch_search_session(
                search_id,
                busy_timeout_ms=120,
                return_on_busy=True,
            ) or {}
            result = dict(session.get("result") or {})
        if not result:
            result = _mcp_pending_first_package_result(
                search_id=search_id,
                request=request,
                plan=plan,
                wait_seconds=wait_seconds,
            )
        result = _attach_brain_metadata(
            normalize_retrieve_response_payload({**result, "search_id": search_id})
        )
        result = _attach_mcp_surface_fields(result, tool_name=tool_name)
        output = build_mcp_retrieval_tool_output(
            tool_name,
            result,
            include_answer_demo=payload.include_answer_demo,
            include_raw_text=payload.include_raw_text or payload.document_text_policy in {"top_raw", "all_raw"},
        )
        return McpToolExecutionResponse(**_attach_tool_brain_metadata(output))


def _mcp_first_package_ready(result: dict[str, Any]) -> bool:
    package = dict(result.get("context_package") or {})
    if not package:
        return False
    return bool(
        str(package.get("agent_markdown") or "").strip()
        or list(package.get("hot_sections") or package.get("sections") or [])
        or list(package.get("document_refs") or [])
        or list(result.get("matches") or [])
        or list(result.get("document_refs") or [])
    )


def _mcp_pending_first_package_result(
    *,
    search_id: str,
    request: RetrieveRequest,
    plan: dict[str, Any],
    wait_seconds: float,
) -> dict[str, Any]:
    return {
        "search_id": search_id,
        "query_text": request.query_text,
        "thread_id": request.thread_id,
        "response_mode": request.response_mode,
        "retrieval_mode": request.retrieval_mode,
        "document_text_policy": request.document_text_policy,
        "semantic_contract": dict(plan.get("semantic_contract") or {}),
        "semantic_contract_runtime": dict(plan.get("semantic_contract_runtime") or {}),
        "planner_runtime": {
            **dict(plan.get("planner_runtime") or {}),
            "nonblocking_first_package_returned": True,
            "first_package_wait_seconds": wait_seconds,
            "http_response_policy": "first_package_with_background_completion",
        },
        "context_package": {},
        "context_package_materialization": {
            "schema_version": "agvm.context_package_materialization.v1",
            "state": "first_package_pending",
            "terminal": False,
            "final_materialization_pending": True,
        },
        "matches": [],
        "document_refs": [],
        "answerability_state": "ai_pending",
        "closure_state": "open",
        "final_closure_ready": False,
        "result_materialization_state": "first_package_pending",
        "final_materialization_pending": True,
        "result_ready_terminal": False,
        "stop_reason": "first_package_wait_budget_elapsed",
    }


_DIRECT_DOCUMENT_RAW_TEXT_LIMIT = 12000
_DIRECT_DOCUMENT_SKIP_TERMS = {
    "about",
    "complete",
    "context",
    "document",
    "documenti",
    "documento",
    "material",
    "materiale",
    "mcp",
    "raw",
    "recupera",
    "related",
    "retrieve",
    "source",
    "sources",
    "testo",
    "tool",
}


def _core_direct_document_terms(payload: McpRetrievalToolRequest, request: RetrieveRequest) -> list[str]:
    text = " ".join(
        [
            str(payload.document_hint or ""),
            str(payload.query_text or ""),
            str(request.query_text or ""),
        ]
    ).lower()
    terms: list[str] = []
    for token in re.findall(r"[\w'-]{3,}", text):
        if token in _DIRECT_DOCUMENT_SKIP_TERMS or token in terms:
            continue
        terms.append(token)
    return terms[:18]


def _core_direct_document_candidate(
    *,
    payload: McpRetrievalToolRequest,
    request: RetrieveRequest,
) -> tuple[dict[str, Any] | None, list[str]]:
    document_id = str(payload.document_id or "").strip()
    if document_id:
        nodes = fetch_nodes_by_ids([document_id], include_raw_text=True)
        if nodes:
            return dict(nodes[0] or {}), []
        preview_node = preview_document_node(document_id)
        if preview_node:
            return preview_node, []

    terms = _core_direct_document_terms(payload, request)
    if not terms:
        return None, []
    candidates = fetch_nodes_by_text_terms(terms, limit=max(36, int(payload.max_matches or 12) * 6), include_raw_text=True)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for candidate in list(candidates or []):
        node = dict(candidate or {})
        provenance = dict(node.get("provenance") or {})
        haystack = " ".join(
            [
                str(node.get("summary") or ""),
                str(node.get("raw_text") or ""),
                str(provenance.get("source_label") or node.get("source_label") or ""),
                str(provenance.get("source_uri") or node.get("source_uri") or ""),
            ]
        ).lower()
        matched = sum(1 for term in terms if term in haystack)
        raw_chars = len(str(node.get("raw_text") or ""))
        if matched < 2 or raw_chars == 0:
            continue
        ranked.append((matched, raw_chars, node))
    if not ranked:
        return None, terms
    ranked.sort(key=lambda item: (-item[0], -item[1], str(item[2].get("id") or "")))
    return ranked[0][2], terms


def _core_document_packet(node: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(node.get("provenance") or {})
    node_id = str(node.get("id") or "").strip()
    full_raw_text = str(node.get("raw_text") or "").strip()
    raw_text = full_raw_text[:_DIRECT_DOCUMENT_RAW_TEXT_LIMIT]
    title = str(node.get("summary") or provenance.get("source_label") or node_id or "Document").strip()
    source_label = str(provenance.get("source_label") or node.get("source_label") or title).strip()
    source_type = str(provenance.get("source_type") or node.get("source_type") or "document").strip()
    source_uri = str(provenance.get("source_uri") or node.get("source_uri") or "").strip() or None
    content_hash = (
        str(
            provenance.get("content_hash")
            or provenance.get("content_digest")
            or provenance.get("source_hash")
            or provenance.get("source_ref_id")
            or node.get("content_hash")
            or node.get("content_digest")
            or node.get("source_hash")
            or ""
        ).strip()
        or None
    )
    raw_text_truncated = len(full_raw_text) > len(raw_text)
    return {
        "anchor_node_id": node_id,
        "title": title,
        "source_label": source_label,
        "source_type": source_type,
        "source_uri": source_uri,
        "content_hash": content_hash,
        "source_trust": node.get("source_trust") or "user_asserted",
        "claim_status": node.get("claim_status") or "fact",
        "answer_eligible": bool(node.get("answer_eligible", True)),
        "profile_eligible": bool(node.get("profile_eligible", True)),
        "document_eligible": True,
        "direct_document_readable": bool(full_raw_text),
        "query_fit_score": 1.0,
        "exact_match_score": 1.0,
        "full_text": raw_text,
        "anchor_raw_text": raw_text,
        "full_text_mode": "anchor_raw" if raw_text else "none",
        "complete_text_available": bool(full_raw_text),
        "raw_text_char_count": len(full_raw_text),
        "available_raw_text_char_count": len(full_raw_text),
        "raw_text_included_char_count": len(raw_text),
        "raw_text_truncated": raw_text_truncated,
        "raw_text_limit": _DIRECT_DOCUMENT_RAW_TEXT_LIMIT,
        "ordered_chunk_sequence": [
            {
                "node_id": f"{node_id}::direct_raw",
                "source_node_id": node_id,
                "chunk_index": 1,
                "raw_text": raw_text,
                "evidence_snippet": raw_text,
                "source_kind": "direct_document_lookup",
            }
        ] if raw_text else [],
        "supported_fact_text": [],
        "source_trace": [
            {
                "node_id": node_id,
                "role": "anchor",
                "title": title,
                "text": raw_text or title,
                "text_preview": raw_text or title,
                "text_char_count": len(full_raw_text) if full_raw_text else len(title),
                "text_included_char_count": len(raw_text or title),
                "text_truncated": raw_text_truncated,
                "source_uri": source_uri,
                "content_hash": content_hash,
            }
        ],
        "project_tags": [],
        "entity_tags": [],
        "topic_tags": [],
        "lookup_role": "exact_document_lookup",
    }


def _core_direct_document_response(
    *,
    payload: McpRetrievalToolRequest,
    request: RetrieveRequest,
) -> McpToolExecutionResponse | None:
    started_at = time.perf_counter()
    node, terms = _core_direct_document_candidate(payload=payload, request=request)
    if not node:
        return None
    document_id = str(node.get("id") or "").strip()
    if not document_id:
        return None
    packet = _core_document_packet(node)
    requested_id = str(payload.document_id or "").strip()
    source = "direct_document_id_hit" if not requested_id or requested_id == document_id else "direct_document_reference_fallback_hit"
    document_lookup = {
        "kind": "exact_document_lookup",
        "state": source,
        "target_text": requested_id or request.query_text,
        "document_id": document_id,
        "requested_document_id": requested_id or None,
        "query_terms": terms,
        "supporting_document_count": 1,
        "source_trace_count": 1,
        "max_exact_match_score": 1.0,
    }
    retrieval_mode = str(request.retrieval_mode or "balanced")
    document_workspace = build_document_workspace_package(
        query_text=request.query_text,
        document_mode="lookup",
        document_lookup=document_lookup,
        document_packets=[packet],
        retrieval_mode=retrieval_mode,
    )
    context_package = build_mcp_context_package(
        query_text=request.query_text,
        context={},
        context_structured={},
        matches=[],
        evidence_reservoir={},
        document_packets=[],
        semantic_contract={"document_contract": {"mode": "lookup"}},
        retrieval_mode=retrieval_mode,
        document_workspace=document_workspace,
        package_mode=request.context_package_mode or "document_full",
        document_text_policy="all_raw",
    )
    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
    result = normalize_retrieve_response_payload(
        {
            "search_id": str(uuid.uuid4()),
            "query_text": request.query_text,
            "mcp_tool_name": "retrieve_document",
            "thread_id": request.thread_id,
            "response_mode": request.response_mode,
            "retrieval_mode": retrieval_mode,
            "document_mode": "lookup",
            "document_lookup_kind": "exact_document_lookup",
            "document_lookup": document_lookup,
            "supporting_documents": list(document_workspace.get("documents") or [])[:1],
            "source_trace": list(document_workspace.get("source_trace") or []),
            "document_workspace": document_workspace,
            "document_text_policy": "all_raw",
            "document_refs": list(context_package.get("document_refs") or []),
            "document_ref_contract": dict(context_package.get("document_ref_contract") or {}),
            "document_delivery_contract": dict(context_package.get("document_delivery_contract") or {}),
            "document_bundle": dict(context_package.get("document_bundle") or {}),
            "document_packets": [packet],
            "context_package": context_package,
            "context_package_materialization": {
                "schema_version": "agvm.context_package_materialization.v1",
                "state": "document_payload_ready",
                "terminal": True,
                "final_materialization_pending": False,
            },
            "probes": [],
            "branches": [],
            "landing_metadata": [],
            "steps": [],
            "matches": [],
            "visited_node_ids": [document_id],
            "visited_bucket_keys": [],
            "stop_reason": source,
            "answerability_state": "grounded",
            "closure_state": "final_sealed",
            "final_closure_ready": True,
            "result_ready_terminal": True,
            "final_materialization_pending": False,
            "planner_runtime": {
                "core_direct_document_fast_path": True,
                "full_route_planning_skipped": True,
                "document_id": document_id,
            },
            "timing": {"total_ms": elapsed_ms, "first_context_ms": elapsed_ms},
        }
    )
    output = build_mcp_retrieval_tool_output(
        "retrieve_document",
        result,
        include_answer_demo=payload.include_answer_demo,
        include_raw_text=True,
    )
    return McpToolExecutionResponse(**_attach_tool_brain_metadata(output))


def _inspect_mcp_result(tool_name: str, payload: McpInspectionRequest) -> McpToolExecutionResponse:
    with _brain_request_scope(_payload_brain_id(payload)):
        result = _completed_search_result(payload.search_id)
        result = _attach_mcp_surface_fields(result, tool_name=tool_name)
        output = build_mcp_retrieval_tool_output(
            tool_name,
            result,
            include_answer_demo=payload.include_answer_demo,
            include_raw_text=payload.include_raw_text,
        )
        response = McpToolExecutionResponse(**_attach_tool_brain_metadata(output))
        record_mcp_evidence(
            brain_id=str(response.brain_id or current_brain_id() or ""),
            event_kind="evidence_opened",
            payload=_model_dump(response),
            session_id=payload.search_id,
        )
        return response


def _completed_search_result(search_id: str) -> dict[str, Any]:
    session = fetch_search_session(search_id)
    if not session:
        raise HTTPException(status_code=404, detail="search_not_found")
    if not session.get("result"):
        raise HTTPException(status_code=409, detail="search_not_completed")
    request_payload = dict(session.get("request") or {})
    result = _attach_brain_metadata(normalize_retrieve_response_payload(dict(session.get("result") or {})))
    result = _attach_mcp_surface_fields(
        result,
        tool_name=_tool_name_for_session(request_payload, dict(session.get("plan") or {})),
    )
    return _retrieve_response_schema_safe(project_search_result_lifecycle(result, str(session.get("status") or "")))


def _tool_name_for_session(request_payload: dict[str, Any], plan: dict[str, Any]) -> str:
    tool_name = str(request_payload.get("mcp_tool_name") or plan.get("mcp_tool_name") or "").strip()
    if tool_name:
        return tool_name
    context_mode = str(request_payload.get("context_package_mode") or "").strip()
    if context_mode == "broad_dossier":
        return "retrieve_document_workspace"
    if context_mode == "forensic_trace" and bool(request_payload.get("complete_paths")):
        return "retrieve_path_corridor"
    return "retrieve_context"


def _master_final_seal_is_authoritative(result: Mapping[str, Any] | None) -> bool:
    payload = dict(result or {})
    master = dict(
        payload.get("master_judgement")
        or dict(payload.get("context_package") or {}).get("master_judgement")
        or dict(payload.get("planner_runtime") or {}).get("master_judgement")
        or {}
    )
    return bool(
        str(payload.get("status") or "").strip().lower() == "completed"
        and str(payload.get("canonical_search_state") or "").strip().lower() == "completed"
        and bool(payload.get("terminal_for_client"))
        and bool(payload.get("result_ready_terminal"))
        and not bool(payload.get("final_materialization_pending"))
        and bool(payload.get("final_closure_ready"))
        and str(payload.get("closure_state") or "").strip() == "final_sealed"
        and str(payload.get("answer_surface_state") or "").strip() == "final_sealed"
        and str(master.get("master_state") or "").strip() == "terminal"
        and bool(master.get("final_seal_allowed"))
        and not bool(master.get("review_required"))
        and not list(master.get("missing_goals") or [])
        and not list(master.get("unresolved_goals") or [])
        and not list(master.get("partial_goals") or [])
    )


def _master_grounded_answer_for_final_surface(payload: Mapping[str, Any]) -> dict[str, Any]:
    master = dict(
        payload.get("master_judgement")
        or dict(payload.get("context_package") or {}).get("master_judgement")
        or dict(payload.get("planner_runtime") or {}).get("master_judgement")
        or {}
    )
    sufficiency = dict(master.get("sufficiency_judge") or {})
    decision = dict(sufficiency.get("ai_master_decision") or master.get("ai_master_decision") or {})
    grounded = dict(decision.get("grounded_answer") or {})
    answer_text = str(grounded.get("answer_text") or "").strip()
    if not answer_text or not _master_final_seal_is_authoritative(payload):
        return {}
    evidence_ids = [
        str(item).strip()
        for item in list(
            grounded.get("evidence_node_ids")
            or grounded.get("supporting_evidence_node_ids")
            or grounded.get("support_ids")
            or []
        )
        if str(item).strip()
    ]
    snippets = [
        dict(item)
        for item in list(grounded.get("evidence_snippets") or [])
        if isinstance(item, Mapping)
    ]
    try:
        confidence = max(0.0, min(1.0, float(grounded.get("confidence") or 0.95)))
    except (TypeError, ValueError):
        confidence = 0.95
    try:
        support_node_count = max(0, int(grounded.get("support_node_count") or len(evidence_ids)))
    except (TypeError, ValueError):
        support_node_count = len(evidence_ids)
    try:
        support_slot_count = max(0, int(grounded.get("support_slot_count") or len(evidence_ids) or 1))
    except (TypeError, ValueError):
        support_slot_count = len(evidence_ids) or 1
    answerability_state = str(grounded.get("answerability_state") or payload.get("answerability_state") or "grounded").strip()
    if answerability_state not in {"grounded", "partial", "insufficient", "ai_pending"}:
        answerability_state = "grounded"
    mode = str(grounded.get("mode") or "llm").strip()
    if mode not in {
        "llm",
        "heuristic",
        "insufficient",
        "partial_known_insufficient",
        "grounded_facts",
        "document_packet",
        "document_lookup_guard",
        "human_synthesizer",
        "contract_human_synthesis",
    }:
        mode = "llm"
    semantic_authority = dict(
        grounded.get("semantic_authority")
        or master.get("semantic_authority")
        or {
            "mode": "ai_v2",
            "source": "master_judgement",
            "fallback_used": False,
        }
    )
    return {
        "answer_text": answer_text,
        "mode": mode,
        "confidence": confidence,
        "evidence_node_ids": evidence_ids,
        "reasoning_summary": str(
            grounded.get("reasoning_summary")
            or decision.get("reasoning_summary")
            or "Master-attested grounded answer materialized from the final seal."
        ),
        "insufficient": False,
        "answerability_state": answerability_state,
        "evidence_snippets": snippets,
        "support_node_count": support_node_count,
        "support_slot_count": support_slot_count,
        "semantic_authority": semantic_authority,
    }


def _materialize_master_grounded_answer_surface(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload or {})
    existing_answer = safe.get("answer")
    existing_text = ""
    if isinstance(existing_answer, Mapping):
        existing_text = str(existing_answer.get("answer_text") or "").strip()
    if existing_text and str(safe.get("answer_short") or "").strip() and str(safe.get("answer_full") or "").strip():
        return safe
    grounded_answer = _master_grounded_answer_for_final_surface(safe)
    if not grounded_answer:
        return safe
    answer_text = str(grounded_answer.get("answer_text") or "").strip()
    if not existing_text:
        safe["answer"] = grounded_answer
    if not str(safe.get("answer_short") or "").strip():
        safe["answer_short"] = answer_text
    if not str(safe.get("answer_full") or "").strip():
        safe["answer_full"] = answer_text
    materialization = dict(safe.get("answer_demo_materialization") or {})
    materialization.update(
        {
            "schema_version": "agvm.answer_demo_materialization.v1",
            "state": "ready",
            "reason": "master_grounded_answer_materialized",
            "source": "master_judgement.sufficiency_judge.ai_master_decision.grounded_answer",
            "blocked_by_ai_materialization_hard_gate": False,
            "context_package_is_source_of_truth": True,
            "primary_context_package_preserved": True,
        }
    )
    safe["answer_demo_materialization"] = materialization
    return safe


def _retrieve_response_schema_safe(result: dict[str, Any]) -> dict[str, Any]:
    safe = normalize_retrieve_response_payload(dict(result or {}))
    normalized_matches: list[dict[str, Any]] = []
    for value in list(safe.get("matches") or []):
        match = dict(value or {})
        node = dict(match.get("node") or {})
        document_hit = dict(match.get("document_hit") or {})
        node_id = str(match.get("node_id") or node.get("id") or "").strip()
        anchor_id = str(document_hit.get("document_anchor_id") or node.get("document_anchor_id") or node_id).strip()
        if not str(match.get("probe_id") or "").strip() and anchor_id:
            match["probe_id"] = f"document:{anchor_id}"
        if not str(match.get("reason") or "").strip():
            matched_terms = [
                str(term).strip()
                for term in list(document_hit.get("matched_terms") or [])[:8]
                if str(term).strip()
            ]
            source_label = str(document_hit.get("source_label") or dict(node.get("provenance") or {}).get("source_label") or "").strip()
            detail = ", ".join(matched_terms)
            match["reason"] = (
                f"Document evidence from {source_label} matched: {detail}."
                if source_label and detail
                else f"Document evidence matched: {detail}."
                if detail
                else str(match.get("evidence_snippet") or match.get("summary") or node_id)
            )
        normalized_matches.append(match)
    safe["matches"] = normalized_matches
    allowed_states = {"grounded", "partial", "insufficient", "ai_pending"}
    state = str(safe.get("answerability_state") or "").strip()
    if state and state not in allowed_states:
        safe["answerability_state"] = "grounded" if state in {"ready", "finalized"} else "partial"
    safe["brain_id"] = current_brain_id() or safe.get("brain_id")
    return _materialize_master_grounded_answer_surface(safe)


def _run_ledger_entry_from_session(session: dict[str, Any]) -> dict[str, Any]:
    request_payload = dict(session.get("request") or {})
    result = dict(session.get("result") or {})
    plan = dict(session.get("plan") or {})
    output: dict[str, Any] = {}
    if result:
        output = build_mcp_retrieval_tool_output(
            _tool_name_for_session(request_payload, plan),
            _attach_mcp_surface_fields(
                {
                    **result,
                    "search_id": session.get("search_id") or result.get("search_id"),
                    "thread_id": session.get("thread_id") or result.get("thread_id"),
                    "query_text": session.get("query_text") or result.get("query_text") or request_payload.get("query_text") or "",
                },
                tool_name=_tool_name_for_session(request_payload, plan),
            ),
        )
    completion = dict(output.get("completion_contract") or result.get("completion_contract") or {})
    lifecycle = dict(output.get("run_lifecycle_contract") or result.get("run_lifecycle_contract") or {})
    payload_truth = dict(output.get("payload_truth_contract") or result.get("payload_truth_contract") or {})
    primary_payload = dict(payload_truth.get("primary_mcp_payload") or {})
    semantic_runtime = dict(output.get("semantic_contract_runtime") or result.get("semantic_contract_runtime") or plan.get("semantic_contract_runtime") or {})
    status = str(session.get("status") or "created")
    completion_state = str(completion.get("state") or lifecycle.get("terminal_state") or "").strip()
    if not completion_state:
        completion_state = "failed" if status == "failed" else "finalized" if status == "completed" and result else status
    try:
        event_count = len(fetch_search_events(str(session.get("search_id") or ""), limit=1000))
    except Exception:  # noqa: BLE001
        event_count = 0
    return {
        "search_id": str(session.get("search_id") or ""),
        "brain_id": str(result.get("brain_id") or request_payload.get("brain_id") or current_brain_id() or "") or None,
        "thread_id": str(session.get("thread_id") or request_payload.get("thread_id") or result.get("thread_id") or "") or None,
        "query_text": str(session.get("query_text") or request_payload.get("query_text") or result.get("query_text") or ""),
        "response_mode": str(session.get("response_mode") or request_payload.get("response_mode") or result.get("response_mode") or "context"),
        "retrieval_mode": str(result.get("retrieval_mode") or request_payload.get("retrieval_mode") or (plan.get("planner_runtime") or {}).get("retrieval_mode") or "balanced"),
        "status": status,
        "terminal_state": completion_state,
        "completion_state": completion_state,
        "completion_reason": str(completion.get("visible_reason") or lifecycle.get("visible_reason") or session.get("stop_reason") or "") or None,
        "mcp_status": str(output.get("status") or "") or None,
        "provider_state": str(lifecycle.get("provider_state") or semantic_runtime.get("provider_state") or semantic_runtime.get("status") or "") or None,
        "provider_degraded": bool(lifecycle.get("provider_degraded") or semantic_runtime.get("provider_degraded") or semantic_runtime.get("degraded")),
        "ai_required": bool(lifecycle.get("ai_required") or semantic_runtime.get("ai_required")),
        "ai_material": bool(lifecycle.get("ai_material") or semantic_runtime.get("material")),
        "first_package_present": bool(primary_payload.get("present") or result.get("context_package")),
        "package_revision_id": str(primary_payload.get("package_revision_id") or lifecycle.get("package_revision_id") or dict(result.get("context_package_materialization") or {}).get("package_revision_id") or "") or None,
        "package_char_count": int(primary_payload.get("char_count") or len(str((result.get("context_package") or {}).get("agent_markdown") or "")) or 0),
        "result_present": bool(result),
        "final_materialization_pending": bool(completion.get("final_materialization_pending") or result.get("final_materialization_pending")),
        "result_ready_terminal": bool(completion.get("result_ready_terminal") or result.get("result_ready_terminal") or status in {"completed", "failed"}),
        "inspect_available": bool(result),
        "created_at": str(session.get("created_at") or ""),
        "updated_at": str(session.get("updated_at") or ""),
        "event_count": event_count,
    }


def _build_memory_brain_health_payload(*, limit: int = 25, include_issue_samples: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    graph = _runtime_graph()
    identity_nucleus = fetch_identity_nucleus()
    recent_search_sessions = fetch_recent_search_sessions(
        limit=limit,
        include_result=False,
        read_only=True,
        busy_timeout_ms=2000,
    )
    recent_maintenance_runs = fetch_recent_maintenance_runs(limit=limit, include_report=False)
    persisted_feedback_events = persisted_feedback_events_for_health(
        brain_id=str(current_brain_id() or ""),
        limit=max(limit * 8, 100),
    )
    metamemory = metamemory_snapshot() if callable(metamemory_snapshot) else {}
    report = build_brain_health_report(
        graph,
        brain_id=current_brain_id(),
        identity_nucleus=identity_nucleus,
        recent_search_sessions=recent_search_sessions,
        recent_feedback_events=persisted_feedback_events,
        recent_maintenance_runs=recent_maintenance_runs,
        metamemory=metamemory,
        calibration_snapshot=fetch_heuristic_calibration_snapshot(),
        health_ai_diagnoser=runtime_health_ai_diagnoser(),
    )
    if not include_issue_samples:
        checks = dict(report.get("checks") or {})
        for section in checks.values():
            if isinstance(section, dict):
                section.pop("issue_sample", None)
                section.pop("orphan_node_sample", None)
                section.pop("missing_anchor_child_sample", None)
                section.pop("failure_sample", None)
        report["checks"] = checks
    timing = {
        "schema_version": "agvm.health_payload_timing.v1",
        "timing_basis": "core_retrieve_router",
        "total_payload_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }
    report["health_payload_timing"] = timing
    summary = dict(report.get("summary") or {})
    summary["health_payload_total_ms"] = timing["total_payload_ms"]
    summary["health_payload_timing_basis"] = timing["timing_basis"]
    report["summary"] = summary
    return report
