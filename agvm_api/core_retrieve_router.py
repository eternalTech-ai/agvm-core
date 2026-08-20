from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from brain_health import build_brain_health_report, build_mcp_brain_health_output
from brain_registry import BrainRegistryError, resolve_brain_scope
from mcp_retrieval import build_mcp_retrieval_tool_output
from retrieval import (
    build_landing_metadata,
    build_search_map_2d_truth,
    normalize_retrieve_response_payload,
    prepare_runtime_plan,
    request_search_supersede,
    retrieve_runtime,
)
from runtime_scope import current_brain_id, runtime_scope_summary, use_runtime_brain
from schemas import (
    McpBrainHealthRequest,
    McpBrainHealthToolExecutionResponse,
    McpInspectionRequest,
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
    create_search_session,
    fail_search_session,
    fetch_active_search_session_by_thread,
    fetch_atlas,
    fetch_graph_snapshot,
    fetch_heuristic_calibration_snapshot,
    fetch_identity_nucleus,
    fetch_recent_maintenance_runs,
    fetch_recent_search_sessions,
    fetch_search_events,
    fetch_search_session,
    fetch_search_trace,
    finalize_search_session,
    mark_search_running,
    preview_runtime_retention_policy,
    save_search_plan,
)
from storage import utc_timestamp

try:
    from metamemory import metamemory_snapshot
except Exception:  # noqa: BLE001
    metamemory_snapshot = None  # type: ignore[assignment]


_SEARCH_THREADS: dict[str, threading.Thread] = {}
_SEARCH_THREAD_LOCK = threading.Lock()
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


def create_core_retrieve_router() -> APIRouter:
    router = APIRouter()

    @router.post("/retrieve", response_model=RetrieveResponse)
    def retrieve_endpoint(payload: RetrieveRequest) -> RetrieveResponse:
        return memory_query_endpoint(payload)

    @router.post("/memory/query", response_model=RetrieveResponse)
    def memory_query_endpoint(payload: RetrieveRequest) -> RetrieveResponse:
        with _brain_request_scope(_payload_brain_id(payload)):
            normalized_payload = _normalized_retrieve_request(payload)
            search_id, _plan = _create_planned_search_session(normalized_payload)
            result = _run_search_session_sync(search_id)
            return RetrieveResponse(**_retrieve_response_schema_safe(result))

    @router.post("/memory/query-plan", response_model=SearchPlanResponse)
    def memory_query_plan_endpoint(payload: RetrieveRequest) -> SearchPlanResponse:
        with _brain_request_scope(_payload_brain_id(payload)):
            normalized_payload = _normalized_retrieve_request(payload)
            search_id, plan = _create_planned_search_session(normalized_payload)
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
            _start_search_thread(payload.search_id, thread_brain_record)
            refreshed = fetch_search_session(payload.search_id)
            status = str((refreshed or {}).get("status") or "running")
            if status not in {"created", "running", "completed", "failed"}:
                status = "running"
            return SearchRunResponse(
                search_id=payload.search_id,
                brain_id=current_brain_id(),
                status=status,  # type: ignore[arg-type]
                stream_url=f"/memory/query-stream/{payload.search_id}?brain_id={current_brain_id()}",
                result_url=f"/memory/query-result/{payload.search_id}?brain_id={current_brain_id()}",
            )

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

    @router.get("/memory/query-stream/{search_id}")
    async def memory_query_stream_endpoint(
        search_id: str,
        brain_id: str | None = Query(default=None),
    ) -> StreamingResponse:
        with _brain_request_scope(brain_id):
            if not fetch_search_session(search_id):
                raise HTTPException(status_code=404, detail="search_not_found")
            stream_brain_record = _resolve_brain_record(current_brain_id())

        async def event_generator() -> AsyncIterator[str]:
            with use_runtime_brain(stream_brain_record):
                last_seq = 0
                while True:
                    events = fetch_search_events(search_id, after_seq=last_seq, limit=200)
                    for event in events:
                        last_seq = int(event.get("seq") or last_seq)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        event_type = str(event.get("event_type") or "")
                        event_payload = dict(event.get("payload") or {})
                        if event_type == "result_ready" and not bool(event_payload.get("final_materialization_pending")):
                            return
                        if event_type == "search_failed":
                            return
                    refreshed = fetch_search_session(search_id)
                    if not refreshed:
                        return
                    if refreshed.get("status") in {"completed", "failed"} and not events:
                        if refreshed.get("result"):
                            payload = {
                                "seq": last_seq + 1,
                                "event_type": "result_ready",
                                "payload": {
                                    "result": _attach_brain_metadata(
                                        normalize_retrieve_response_payload(dict(refreshed["result"]))
                                    ),
                                    "result_materialization_state": "finalized",
                                    "final_materialization_pending": False,
                                    "result_ready_terminal": True,
                                },
                                "created_at": utc_timestamp(),
                                "terminal": True,
                            }
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        return
                    await asyncio.sleep(0.35)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.post("/memory/mcp/retrieve-context", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-context", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_context_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_context", payload)

    @router.post("/memory/mcp/retrieve-document", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/retrieve-document", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_retrieve_document_endpoint(payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
        return _run_mcp_retrieval_tool("retrieve_document", payload)

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

    @router.post("/memory/mcp/inspect-path-corridor", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/inspect-path-corridor", response_model=McpToolExecutionResponse, response_model_exclude_none=True)
    def mcp_inspect_path_corridor_endpoint(payload: McpInspectionRequest) -> McpToolExecutionResponse:
        return _inspect_mcp_result("inspect_path_corridor", payload)

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


@contextmanager
def _brain_request_scope(brain_id: str | None) -> Iterator[dict[str, Any]]:
    brain_record = _resolve_brain_record(brain_id)
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


def _create_planned_search_session(payload: RetrieveRequest) -> tuple[str, dict[str, Any]]:
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
    plan = prepare_runtime_plan(normalized_payload, _runtime_atlas(), fetch_identity_nucleus(), defer_planner_seed=True)
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
    save_search_plan(search_id, plan)
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
            "semantic_contract": dict(plan.get("semantic_contract") or {}),
            "semantic_contract_runtime": dict(plan.get("semantic_contract_runtime") or {}),
            "planner_runtime": dict(plan.get("planner_runtime") or {}),
            "probes": list(plan.get("probes") or []),
            "branches": list(plan.get("branches") or []),
            "landing_metadata": landing_metadata,
            "route_truth_summary": {},
            "search_map_2d_truth": search_map_2d_truth,
            "map_stream_state": map_stream_state,
        },
    )
    return search_id, plan


def _search_plan_response(search_id: str, payload: RetrieveRequest, plan: dict[str, Any]) -> SearchPlanResponse:
    planner_mode = str(plan.get("planner_mode") or "heuristic")
    decomposition_mode = str(plan.get("decomposition_mode") or "heuristic")
    planner_runtime = {
        **dict(plan.get("planner_runtime") or {}),
        "planner_path": "hybrid_initial" if planner_mode == "hybrid" else ("llm" if planner_mode == "llm" else "fallback"),
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


def _run_search_session_sync(search_id: str) -> dict[str, Any]:
    session = fetch_search_session(search_id)
    if not session:
        raise HTTPException(status_code=404, detail="search_not_found")
    request_payload = dict(session.get("request") or {})
    query = _normalized_retrieve_request(RetrieveRequest(**request_payload))
    atlas_payload = _runtime_atlas()
    identity_nucleus = fetch_identity_nucleus()
    plan = dict(session.get("plan") or {}) or prepare_runtime_plan(query, atlas_payload, identity_nucleus, defer_planner_seed=True)
    plan["brain_id"] = current_brain_id()
    plan["planner_runtime"] = {
        **dict(plan.get("planner_runtime") or {}),
        "brain_id": current_brain_id(),
        "core_retrieve_router": True,
    }
    save_search_plan(search_id, plan)
    mark_search_running(search_id)
    append_search_event(
        search_id,
        "worker_started",
        {"search_id": search_id, "brain_id": current_brain_id(), "started_at": utc_timestamp()},
    )
    try:
        result = retrieve_runtime(
            query,
            atlas_payload,
            identity_nucleus,
            prepared_plan=plan,
            search_id=search_id,
            event_callback=lambda event_type, event_payload: append_search_event(search_id, event_type, event_payload),
        )
        result = _attach_brain_metadata(
            normalize_retrieve_response_payload({**dict(result or {}), "search_id": search_id})
        )
        result = _attach_mcp_surface_fields(result, tool_name=_tool_name_for_session(request_payload, plan))
        finalize_search_session(search_id, result)
        append_search_event(
            search_id,
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
        append_search_event(search_id, "search_failed", {"brain_id": current_brain_id(), "error": str(exc)})
        fail_search_session(search_id, str(exc))
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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


def _start_search_thread(search_id: str, brain_record: dict[str, Any]) -> None:
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
                    _run_search_session_sync(search_id)
            finally:
                with _SEARCH_THREAD_LOCK:
                    _SEARCH_THREADS.pop(search_id, None)

        thread = threading.Thread(target=worker, name=f"agvm-core-query-{search_id}", daemon=True)
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


def _run_mcp_retrieval_tool(tool_name: str, payload: McpRetrievalToolRequest) -> McpToolExecutionResponse:
    with _brain_request_scope(_payload_brain_id(payload)):
        request = _mcp_retrieve_request(tool_name, payload)
        search_id, plan = _create_planned_search_session(request)
        plan["mcp_tool_name"] = tool_name
        plan["planner_runtime"] = {
            **dict(plan.get("planner_runtime") or {}),
            "mcp_tool_name": tool_name,
            "mcp_entrypoint_tool": tool_name,
            "core_retrieve_router": True,
        }
        save_search_plan(search_id, plan)
        result = _run_search_session_sync(search_id)
        output = build_mcp_retrieval_tool_output(
            tool_name,
            result,
            include_answer_demo=payload.include_answer_demo,
            include_raw_text=payload.include_raw_text or payload.document_text_policy in {"top_raw", "all_raw"},
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
        return McpToolExecutionResponse(**_attach_tool_brain_metadata(output))


def _completed_search_result(search_id: str) -> dict[str, Any]:
    session = fetch_search_session(search_id)
    if not session:
        raise HTTPException(status_code=404, detail="search_not_found")
    if not session.get("result"):
        raise HTTPException(status_code=409, detail="search_not_completed")
    request_payload = dict(session.get("request") or {})
    result = _attach_brain_metadata(normalize_retrieve_response_payload(dict(session.get("result") or {})))
    return _attach_mcp_surface_fields(result, tool_name=_tool_name_for_session(request_payload, dict(session.get("plan") or {})))


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


def _retrieve_response_schema_safe(result: dict[str, Any]) -> dict[str, Any]:
    safe = normalize_retrieve_response_payload(dict(result or {}))
    allowed_states = {"grounded", "partial", "insufficient", "ai_pending"}
    state = str(safe.get("answerability_state") or "").strip()
    if state and state not in allowed_states:
        safe["answerability_state"] = "grounded" if state in {"ready", "finalized"} else "partial"
    safe["brain_id"] = current_brain_id() or safe.get("brain_id")
    return safe


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
    metamemory = metamemory_snapshot() if callable(metamemory_snapshot) else {}
    report = build_brain_health_report(
        graph,
        brain_id=current_brain_id(),
        identity_nucleus=identity_nucleus,
        recent_search_sessions=recent_search_sessions,
        recent_maintenance_runs=recent_maintenance_runs,
        metamemory=metamemory,
        calibration_snapshot=fetch_heuristic_calibration_snapshot(),
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
