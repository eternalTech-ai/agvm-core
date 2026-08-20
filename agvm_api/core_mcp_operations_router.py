from __future__ import annotations

import hashlib
import math
import uuid
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from brain_registry import BrainRegistryError, resolve_brain_scope
from projection import position_to_bucket
from runtime_scope import use_runtime_brain
from schemas import (
    McpClarificationRequest,
    McpGrowApplyRequest,
    McpGrowSourceRequest,
    McpGrowToolExecutionResponse,
    McpMaintenanceApplyRequest,
    McpMaintenanceRequest,
    McpMaintenanceToolExecutionResponse,
    McpMatrixCalibrationApplyRequest,
    McpMatrixCalibrationRequest,
    McpMatrixCalibrationToolExecutionResponse,
    McpMemoryOSListRequest,
    McpWriteMemoryCommitRequest,
    McpWriteMemoryPreviewRequest,
)
from storage import utc_timestamp


_GROW_RUNS: dict[str, dict[str, Any]] = {}
_MATRIX_PLANS: dict[str, dict[str, Any]] = {}


def create_core_mcp_operations_router() -> APIRouter:
    router = APIRouter()

    @router.post("/memory/mcp/grow-source-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_preview(payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_preview("grow_source_preview", payload)

    @router.post("/memory/mcp/grow-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_preview(payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_preview("grow_preview", payload)

    @router.post("/memory/mcp/grow-guided", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-guided", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_guided(payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_preview("grow_guided", payload, guided=True)

    @router.post("/memory/mcp/grow-source-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_status(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_status("grow_source_status", payload)

    @router.post("/memory/mcp/grow-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_status(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_status("grow_status", payload)

    @router.post("/memory/mcp/grow-source-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_apply(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_apply("grow_source_apply", payload)

    @router.post("/memory/mcp/grow-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_apply(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_apply("grow_apply", payload)

    @router.post("/memory/mcp/write-memory-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/write-memory-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def write_memory_preview(payload: McpWriteMemoryPreviewRequest) -> McpGrowToolExecutionResponse:
        return _write_memory_preview(payload)

    @router.post("/memory/mcp/write-memory-commit", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/write-memory-commit", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def write_memory_commit(payload: McpWriteMemoryCommitRequest) -> McpGrowToolExecutionResponse:
        return _write_memory_commit(payload)

    @router.post("/memory/mcp/ask-memory-clarification", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/ask-memory-clarification", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def ask_memory_clarification(payload: McpClarificationRequest) -> McpGrowToolExecutionResponse:
        return _ask_memory_clarification(payload)

    @router.post("/memory/mcp/sleep-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        return _sleep_evolve("sleep_preview", "sleep", payload, apply_requested=False)

    @router.post("/memory/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        return _sleep_evolve("evolve_preview", "evolve", payload, apply_requested=False)

    @router.post("/memory/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        return _sleep_evolve("sleep_apply", "sleep", payload, apply_requested=True)

    @router.post("/memory/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        return _sleep_evolve("evolve_apply", "evolve", payload, apply_requested=True)

    @router.post("/memory/mcp/matrix-calibration-preview", response_model=McpMatrixCalibrationToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/matrix-calibration-preview", response_model=McpMatrixCalibrationToolExecutionResponse, response_model_exclude_none=True)
    def matrix_calibration_preview(payload: McpMatrixCalibrationRequest) -> McpMatrixCalibrationToolExecutionResponse:
        return _matrix_calibration_preview(payload)

    @router.post("/memory/mcp/matrix-calibration-apply", response_model=McpMatrixCalibrationToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/matrix-calibration-apply", response_model=McpMatrixCalibrationToolExecutionResponse, response_model_exclude_none=True)
    def matrix_calibration_apply(payload: McpMatrixCalibrationApplyRequest) -> McpMatrixCalibrationToolExecutionResponse:
        return _matrix_calibration_apply(payload)

    @router.post("/memory/mcp/list-open-questions", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/list-open-questions", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def list_open_questions(payload: McpMemoryOSListRequest) -> McpMaintenanceToolExecutionResponse:
        return _memory_os_list("list_open_questions", "open_questions", payload)

    @router.post("/memory/mcp/list-hypotheses", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/list-hypotheses", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def list_hypotheses(payload: McpMemoryOSListRequest) -> McpMaintenanceToolExecutionResponse:
        return _memory_os_list("list_hypotheses", "hypotheses", payload)

    @router.post("/memory/mcp/list-contradictions", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/list-contradictions", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def list_contradictions(payload: McpMemoryOSListRequest) -> McpMaintenanceToolExecutionResponse:
        return _memory_os_list("list_contradictions", "contradictions", payload)

    @router.post("/memory/mcp/list-memory-os-processes", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/list-memory-os-processes", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def list_memory_os_processes(payload: McpMemoryOSListRequest) -> McpMaintenanceToolExecutionResponse:
        return _memory_os_list("list_memory_os_processes", "processes", payload)

    return router


def _with_brain(brain_id: str | None, fn: Callable[[dict[str, Any]], Any]) -> Any:
    try:
        record = resolve_brain_scope(brain_id=brain_id)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with use_runtime_brain(record):
        from sqlite_store import bootstrap_runtime_store

        bootstrap_runtime_store()
        return fn(record)


def _hash_payload(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _position_for(seed: str) -> dict[str, float]:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    parts = [int.from_bytes(digest[index : index + 2], "big") / 65535.0 for index in (0, 2, 4)]
    radius = 0.45 + (parts[0] * 0.9)
    theta = parts[1] * math.tau
    z = (parts[2] * 2.0 - 1.0) * 0.55
    xy = math.sqrt(max(0.0, radius * radius - z * z))
    return {"x": round(math.cos(theta) * xy, 6), "y": round(math.sin(theta) * xy, 6), "z": round(z, 6)}


def _node_from_text(
    *,
    brain_id: str | None,
    text: str,
    source_label: str | None,
    source_type: str | None,
    preview_id: str | None = None,
) -> dict[str, Any]:
    normalized_text = " ".join(str(text or "").split())
    summary = normalized_text[:220] if normalized_text else "Untitled memory"
    node_hash = _hash_payload(f"{brain_id or 'default'}::{source_label or ''}::{normalized_text}", 16)
    node_id = f"memory::{node_hash}"
    position = _position_for(node_id)
    return {
        "id": node_id,
        "node_kind": "memory",
        "memory_type": "fact",
        "raw_text": normalized_text,
        "summary": summary,
        "routing_semantic_scores": {},
        "routing_facets": {},
        "routing_brainhex": {},
        "semantic_color": {"r": 104, "g": 142, "b": 255},
        "base_position": position,
        "final_position": position,
        "topology_brainhex": {},
        "topology_color": {"r": 104, "g": 142, "b": 255},
        "bucket": position_to_bucket(position),
        "granularity": 0.55,
        "novelty": 0.75,
        "links": [],
        "highways": [],
        "provenance": {
            "mode": "core_raw_mcp_grow",
            "source_label": source_label or "Raw MCP input",
            "source_type": source_type or "manual_text",
            "created_at": utc_timestamp(),
        },
        "derivation_role": "primary",
        "derivation_confidence": 0.72,
        "derived_from_preview_id": preview_id,
        "memory_confidence": 0.72,
        "identity_resolution_confidence": 0.5,
        "evidence_confidence": 0.65,
        "stability_confidence": 0.55,
        "source_trust": "user_asserted",
        "claim_status": "fact",
        "answer_eligible": True,
        "profile_eligible": True,
        "document_eligible": True,
    }


def _preview_bundle(*, brain_id: str | None, text: str, source_label: str | None, source_type: str | None, learning_mode: str) -> dict[str, Any]:
    seed = f"{brain_id or 'default'}::{text}"
    preview_id = f"preview::{_hash_payload(seed, 16)}"
    node = _node_from_text(
        brain_id=brain_id,
        text=text,
        source_label=source_label,
        source_type=source_type,
        preview_id=preview_id,
    )
    preview_node = {
        **node,
        "id": preview_id,
        "preview_kind": "primary",
        "preview_label": source_label or "Raw MCP memory",
        "selected_by_default": True,
        "preview_confidence": 0.72,
        "persist_mode": "create",
        "requires_human_review": learning_mode == "strict_review",
        "learning_mode": learning_mode,
        "learning_action": "create_memory_node",
        "learning_policy_reasons": ["core_raw_mcp_preview_requires_explicit_commit"],
    }
    return {
        "brain_id": brain_id,
        "primary_node_preview": preview_node,
        "derived_nodes": [],
        "derived_edges": [],
        "derivation_mode": "heuristic",
        "warnings": [],
        "merge_decisions": [],
        "identity_resolution_decisions": [],
        "identity_nucleus": {},
        "preview_quality_contract": {
            "schema_version": "agvm.core_raw_mcp_preview_quality.v1",
            "status": "review_required",
            "llm_required": False,
        },
        "cognitive_write_plan": {
            "schema_version": "agvm.core_raw_mcp_write_plan.v1",
            "operation": "create_memory_node",
            "confirm_apply_required": True,
        },
        "learning_policy": {
            "learning_mode": learning_mode,
            "mutation_gate": "confirm_apply_required",
        },
        "write_trace": {
            "mode": "write_preview",
            "input_mode": "auto",
            "derivation_mode": "heuristic",
            "actors": [],
            "stages": [],
        },
    }


def _source_investigation_payload(*, brain_id: str | None, raw_input: str, source_label: str | None, source_uri: str | None, tool_name: str) -> dict[str, Any]:
    seed = f"{brain_id or 'default'}::{raw_input}"
    investigation_id = f"source_investigation::{_hash_payload(seed, 16)}"
    created_at = utc_timestamp()
    return {
        "schema_version": "agvm.source_investigation.core_raw.v1",
        "brain_id": brain_id,
        "investigation_id": investigation_id,
        "created_at": created_at,
        "status": "preview_ready",
        "source_request": {
            "source_label": source_label,
            "source_uri": source_uri,
            "tool_name": tool_name,
        },
        "source_detection": {
            "schema_version": "agvm.source_detection.v1",
            "source_kind": "manual_text",
            "confidence": 0.65,
            "signals": ["core_raw_mcp_text_input"],
            "url_count": 1 if source_uri else 0,
            "urls": [source_uri] if source_uri else [],
            "non_url_text_char_count": len(raw_input),
        },
        "source_units": [
            {
                "unit_id": f"source_unit::{_hash_payload(raw_input, 12)}",
                "kind": "text",
                "title": source_label or "Raw MCP source",
                "source_uri": source_uri,
                "raw_text": raw_input,
                "clean_text": raw_input,
                "summary": raw_input[:220],
                "language": "unknown",
                "char_count": len(raw_input),
                "token_estimate": max(1, len(raw_input.split())),
                "confidence": 0.65,
                "provenance": {
                    "source_label": source_label,
                    "source_type": "manual_text",
                    "retrieved_at": created_at,
                },
            }
        ],
        "open_questions": [],
        "clarification_questions": [],
        "compiler_handoff": {
            "handoff_version": "agvm.compiler_handoff.core_raw.v1",
            "source_summary": raw_input[:500],
            "mega_text": raw_input,
            "source_purpose": "unknown",
            "recommended_input_mode": "auto",
            "recommended_learning_mode": "strict_review",
            "must_preserve_raw_text": True,
            "preview_eligible": True,
        },
        "source_formation_contract": {
            "schema_version": "agvm.source_formation_contract.core_raw.v1",
            "mutation_gate": "confirm_apply_required",
        },
        "warnings": [],
        "blocked_reasons": [],
        "timeline": [{"at": created_at, "event": "core_raw_mcp_preview_ready"}],
    }


def _grow_source_preview(tool_name: str, payload: McpGrowSourceRequest, *, guided: bool = False) -> McpGrowToolExecutionResponse:
    def run(record: dict[str, Any]) -> McpGrowToolExecutionResponse:
        brain_id = str(record.get("brain_id") or payload.brain_id or "")
        learning_mode = "guided_learning" if guided else "strict_review"
        source = _source_investigation_payload(
            brain_id=brain_id,
            raw_input=payload.raw_input,
            source_label=payload.source_label,
            source_uri=payload.source_uri,
            tool_name=tool_name,
        )
        bundle = _preview_bundle(
            brain_id=brain_id,
            text=payload.raw_input,
            source_label=payload.source_label,
            source_type=payload.input_kind,
            learning_mode=learning_mode,
        )
        run_id = str(source["investigation_id"])
        _GROW_RUNS[run_id] = {"source_investigation": source, "preview_bundle": bundle}
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_execution.v1",
            brain_id=brain_id,
            tool_name=tool_name,
            status="preview_ready",
            source_investigation=source,
            source_formation_contract=dict(source.get("source_formation_contract") or {}),
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False),
            preview_bundle=bundle,
            learning_policy=dict(bundle.get("learning_policy") or {}),
            write_trace=dict(bundle.get("write_trace") or {}),
            completeness={"status": "preview_ready", "requires_confirm_apply": True},
            mcp_latency_profile={"mode": "core_raw_mcp", "llm_required": False},
            budget={"source_chars": len(payload.raw_input), "max_total_chars": getattr(payload, "max_total_chars", None)},
        )

    return _with_brain(payload.brain_id, run)


def _grow_status(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    run = _GROW_RUNS.get(str(payload.investigation_id or ""))
    source = dict(payload.source_investigation or dict(run or {}).get("source_investigation") or {})
    bundle = payload.preview_bundle or dict(run or {}).get("preview_bundle")
    status = "preview_ready" if source or bundle else "blocked"
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_execution.v1",
        brain_id=payload.brain_id,
        tool_name=tool_name,
        status=status,
        source_investigation=source,
        source_formation_contract=dict(payload.source_formation_contract or source.get("source_formation_contract") or {}),
        memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False),
        preview_bundle=bundle,
        completeness={"status": status, "investigation_found": bool(source or bundle)},
    )


def _grow_apply(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    if not payload.confirm_apply:
        return _blocked_grow(tool_name, payload.brain_id, "confirm_apply_required")
    run = _GROW_RUNS.get(str(payload.investigation_id or ""))
    bundle = payload.preview_bundle or dict(run or {}).get("preview_bundle")
    if not isinstance(bundle, dict):
        return _blocked_grow(tool_name, payload.brain_id, "preview_bundle_required")
    return _commit_bundle(
        tool_name=tool_name,
        brain_id=payload.brain_id,
        bundle=bundle,
        selected_preview_ids=payload.selected_preview_ids or payload.approved_preview_ids,
        source_investigation=payload.source_investigation or dict(run or {}).get("source_investigation") or {},
    )


def _write_memory_preview(payload: McpWriteMemoryPreviewRequest) -> McpGrowToolExecutionResponse:
    def run(record: dict[str, Any]) -> McpGrowToolExecutionResponse:
        brain_id = str(record.get("brain_id") or payload.brain_id or "")
        bundle = _preview_bundle(
            brain_id=brain_id,
            text=payload.text,
            source_label=payload.source_label,
            source_type=payload.source_type or "manual_text",
            learning_mode=payload.learning_mode,
        )
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_execution.v1",
            brain_id=brain_id,
            tool_name="write_memory_preview",
            status="preview_ready",
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False),
            preview_bundle=bundle,
            cognitive_write_plan=dict(bundle.get("cognitive_write_plan") or {}),
            learning_policy=dict(bundle.get("learning_policy") or {}),
            write_trace=dict(bundle.get("write_trace") or {}),
            completeness={"status": "preview_ready", "requires_confirm_apply": True},
        )

    return _with_brain(payload.brain_id, run)


def _write_memory_commit(payload: McpWriteMemoryCommitRequest) -> McpGrowToolExecutionResponse:
    if not payload.confirm_apply:
        return _blocked_grow("write_memory_commit", payload.brain_id, "confirm_apply_required")
    return _commit_bundle(
        tool_name="write_memory_commit",
        brain_id=payload.brain_id,
        bundle=payload.bundle,
        selected_preview_ids=payload.selected_preview_ids or payload.approved_preview_ids,
        source_investigation={},
    )


def _ask_memory_clarification(payload: McpClarificationRequest) -> McpGrowToolExecutionResponse:
    text = payload.raw_input or payload.text or ""
    questions = [
        {
            "question_id": "source_scope",
            "question": "What should this memory be used for?",
            "reason": "A scope helps AGVM route the new memory correctly.",
        },
        {
            "question_id": "source_trust",
            "question": "Is this user asserted, observed, or imported from another source?",
            "reason": "Trust level affects retrieval and future sleep/evolve review.",
        },
    ][: payload.question_limit]
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_execution.v1",
        brain_id=payload.brain_id,
        tool_name="ask_memory_clarification",
        status="asking_clarification",
        clarification_request={
            "schema_version": "agvm.mcp_clarification_request.core_raw.v1",
            "question_count": len(questions),
            "input_preview": text[:240],
        },
        clarification_questions=questions,
        completeness={"status": "questions_ready"},
    )


def _commit_bundle(
    *,
    tool_name: str,
    brain_id: str | None,
    bundle: dict[str, Any],
    selected_preview_ids: list[str],
    source_investigation: dict[str, Any],
) -> McpGrowToolExecutionResponse:
    def run(record: dict[str, Any]) -> McpGrowToolExecutionResponse:
        from sqlite_store import append_memory_learning_event, fetch_graph_snapshot, replace_runtime_graph

        resolved_brain_id = str(record.get("brain_id") or brain_id or "")
        selected = {str(item) for item in selected_preview_ids if str(item).strip()}
        preview_nodes = [dict(bundle.get("primary_node_preview") or {})]
        preview_nodes.extend(dict(item) for item in list(bundle.get("derived_nodes") or []) if isinstance(item, dict))
        nodes_to_persist: list[dict[str, Any]] = []
        for preview in preview_nodes:
            preview_id = str(preview.get("id") or "")
            if selected and preview_id not in selected:
                continue
            if not selected and preview is not preview_nodes[0] and not bool(preview.get("selected_by_default")):
                continue
            text = str(preview.get("raw_text") or preview.get("summary") or "").strip()
            if not text:
                continue
            nodes_to_persist.append(
                _node_from_text(
                    brain_id=resolved_brain_id,
                    text=text,
                    source_label=str(dict(preview.get("provenance") or {}).get("source_label") or preview.get("preview_label") or ""),
                    source_type=str(dict(preview.get("provenance") or {}).get("source_type") or "manual_text"),
                    preview_id=preview_id,
                )
            )
        if not nodes_to_persist:
            return _blocked_grow(tool_name, resolved_brain_id, "no_preview_nodes_selected")
        graph = fetch_graph_snapshot()
        existing = {str(node.get("id") or "") for node in list(graph.get("nodes") or []) if isinstance(node, dict)}
        graph["nodes"] = list(graph.get("nodes") or []) + [node for node in nodes_to_persist if str(node.get("id") or "") not in existing]
        graph["meta"] = {**dict(graph.get("meta") or {}), "updated_by": tool_name, "graph_updated_at": utc_timestamp()}
        replace_runtime_graph(graph)
        persisted_ids = [str(node["id"]) for node in nodes_to_persist]
        for node in nodes_to_persist:
            append_memory_learning_event(
                brain_id=resolved_brain_id,
                operation_id=tool_name,
                event_kind="memory_committed",
                event_source="core_raw_mcp",
                preview_id=node.get("derived_from_preview_id"),
                persisted_node_id=node.get("id"),
                memory_act_type="create",
                claim_status=node.get("claim_status"),
                source_trust=node.get("source_trust"),
                confidence=node.get("memory_confidence"),
                payload={"summary": node.get("summary"), "source_investigation": source_investigation},
            )
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_execution.v1",
            brain_id=resolved_brain_id,
            tool_name=tool_name,
            status="applied",
            source_investigation=source_investigation,
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=True),
            preview_bundle=bundle,
            persist_result={
                "schema_version": "agvm.core_raw_mcp_persist_result.v1",
                "persisted_node_ids": persisted_ids,
                "persisted_count": len(persisted_ids),
            },
            completeness={"status": "applied", "persisted_count": len(persisted_ids)},
        )

    return _with_brain(brain_id, run)


def _blocked_grow(tool_name: str, brain_id: str | None, reason: str) -> McpGrowToolExecutionResponse:
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_execution.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="blocked",
        memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False, blocked_reason=reason),
        completeness={"status": "blocked", "reason": reason},
    )


def _mutation_lifecycle(*, applied: bool, blocked_reason: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "agvm.memory_operation_lifecycle.core_raw.v1",
        "preview_required": True,
        "confirm_apply_required": True,
        "applied": bool(applied),
        "blocked_reason": blocked_reason,
        "hidden_mutation_allowed": False,
    }


def _sleep_evolve(
    tool_name: str,
    mode: str,
    payload: McpMaintenanceRequest | McpMaintenanceApplyRequest,
    *,
    apply_requested: bool,
) -> McpMaintenanceToolExecutionResponse:
    if apply_requested and not bool(getattr(payload, "confirm_apply", False)):
        return _blocked_maintenance(tool_name, payload.brain_id, "confirm_apply_required")

    def run(record: dict[str, Any]) -> McpMaintenanceToolExecutionResponse:
        from maintenance import evolve_graph, sleep_review_graph
        from sqlite_store import fetch_graph_snapshot, replace_runtime_graph, store_maintenance_run

        resolved_brain_id = str(record.get("brain_id") or payload.brain_id or "")
        graph = fetch_graph_snapshot()
        runner = sleep_review_graph if mode == "sleep" else evolve_graph
        updated_graph, report = runner(
            graph,
            preview_only=not apply_requested,
            focus_node_id=payload.focus_node_id,
            max_nodes_considered=payload.max_nodes_considered,
        )
        if apply_requested:
            replace_runtime_graph(updated_graph)
        maintenance_id = str(report.get("maintenance_id") or f"maintenance::{uuid.uuid4()}")
        report = {
            **dict(report or {}),
            "maintenance_id": maintenance_id,
            "mode": mode,
            "applied": bool(apply_requested),
            "core_raw_mcp": True,
        }
        store_maintenance_run(
            maintenance_id=maintenance_id,
            mode=mode,
            applied=bool(apply_requested),
            preview_only=not apply_requested,
            focus_node_id=payload.focus_node_id,
            report=report,
        )
        return McpMaintenanceToolExecutionResponse(
            schema_version="agvm.mcp_maintenance_tool_execution.v1",
            brain_id=resolved_brain_id,
            tool_name=tool_name,
            status="applied" if apply_requested else "preview_ready",
            maintenance_report=report,
            maintenance_proposals=list(report.get("maintenance_proposals") or []),
            elastic_topology_proposals=list(report.get("elastic_topology_proposals") or []),
            maintenance_truth_contract={
                "schema_version": "agvm.maintenance_truth_contract.core_raw.v1",
                "preview_only": not apply_requested,
                "hidden_mutation_allowed": False,
            },
            sleep_evolve_lifecycle_contract=_mutation_lifecycle(applied=apply_requested),
            maintenance_transaction=dict(report.get("maintenance_transaction") or {}),
            preview_budget_guard=dict(report.get("preview_budget_guard") or {}),
            maintenance_preview_plan=dict(report.get("maintenance_preview_plan") or {}),
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=apply_requested),
            proposal_review_table=list(report.get("proposal_review_table") or []),
            metamemory_snapshot=dict(report.get("metamemory_snapshot") or {}),
            apply_policy_guard=dict(report.get("apply_policy_guard") or {}),
            maintenance_latency_profile={"mode": "core_raw_mcp"},
        )

    return _with_brain(payload.brain_id, run)


def _blocked_maintenance(tool_name: str, brain_id: str | None, reason: str) -> McpMaintenanceToolExecutionResponse:
    return McpMaintenanceToolExecutionResponse(
        schema_version="agvm.mcp_maintenance_tool_execution.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="blocked",
        maintenance_report={"status": "blocked", "reason": reason},
        memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False, blocked_reason=reason),
        apply_policy_guard={"confirm_apply_required": True, "reason": reason},
    )


def _matrix_calibration_preview(payload: McpMatrixCalibrationRequest) -> McpMatrixCalibrationToolExecutionResponse:
    def run(record: dict[str, Any]) -> McpMatrixCalibrationToolExecutionResponse:
        from geometry_calibration import build_brain_geometry_calibration_report, build_matrix_calibration_position_plan
        from sqlite_store import fetch_graph_snapshot

        resolved_brain_id = str(record.get("brain_id") or payload.brain_id or "")
        graph = fetch_graph_snapshot()
        report = build_brain_geometry_calibration_report(graph, max_nodes=payload.max_nodes_considered)
        plan = build_matrix_calibration_position_plan(
            graph,
            max_nodes=payload.max_nodes_considered,
            max_updates=payload.max_position_updates,
        )
        signature = str(plan.get("plan_signature") or f"matrix_plan::{uuid.uuid4()}")
        _MATRIX_PLANS[signature] = {"brain_id": resolved_brain_id, "position_update_plan": plan}
        return McpMatrixCalibrationToolExecutionResponse(
            schema_version="agvm.mcp_matrix_calibration_tool_execution.v1",
            brain_id=resolved_brain_id,
            tool_name="matrix_calibration_preview",
            status="ok",
            maintenance_id=f"matrix_calibration::{signature}",
            brain_geometry_calibration=report,
            calibration_proposals=list(report.get("calibration_proposals") or []),
            recommendations=list(report.get("recommendations") or []),
            matrix_change_policy=dict(report.get("matrix_change_policy") or {}),
            maintenance_truth_contract={"preview_only": True, "hidden_mutation_allowed": False},
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False),
            position_update_plan=plan,
            apply_policy_guard={"confirm_apply_required": True, "preview_signature": signature},
            latency_profile={"mode": "core_raw_mcp"},
            completeness={"status": "preview_ready", "update_count": int(plan.get("update_count") or 0)},
        )

    return _with_brain(payload.brain_id, run)


def _matrix_calibration_apply(payload: McpMatrixCalibrationApplyRequest) -> McpMatrixCalibrationToolExecutionResponse:
    if not payload.confirm_apply:
        return McpMatrixCalibrationToolExecutionResponse(
            schema_version="agvm.mcp_matrix_calibration_tool_execution.v1",
            brain_id=payload.brain_id,
            tool_name="matrix_calibration_apply",
            status="blocked",
            apply_policy_guard={"confirm_apply_required": True, "reason": "confirm_apply_required"},
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=False, blocked_reason="confirm_apply_required"),
        )

    def run(record: dict[str, Any]) -> McpMatrixCalibrationToolExecutionResponse:
        from geometry_calibration import apply_matrix_calibration_position_plan_to_graph, build_matrix_calibration_position_plan
        from sqlite_store import fetch_graph_snapshot, replace_runtime_graph

        resolved_brain_id = str(record.get("brain_id") or payload.brain_id or "")
        graph = fetch_graph_snapshot()
        plan = dict(_MATRIX_PLANS.get(str(payload.preview_signature or ""), {}).get("position_update_plan") or {})
        if not plan:
            plan = build_matrix_calibration_position_plan(
                graph,
                max_nodes=payload.max_nodes_considered,
                max_updates=payload.max_position_updates,
            )
        updated_graph = apply_matrix_calibration_position_plan_to_graph(graph, plan)
        replace_runtime_graph(updated_graph)
        return McpMatrixCalibrationToolExecutionResponse(
            schema_version="agvm.mcp_matrix_calibration_tool_execution.v1",
            brain_id=resolved_brain_id,
            tool_name="matrix_calibration_apply",
            status="applied",
            maintenance_id=f"matrix_calibration::{plan.get('plan_signature') or uuid.uuid4()}",
            brain_geometry_calibration={"status": "applied"},
            matrix_change_policy={"mutates_graph": True, "confirm_apply": True},
            memory_operation_lifecycle_contract=_mutation_lifecycle(applied=True),
            position_update_plan=plan,
            matrix_delta={"applied_update_count": int(plan.get("update_count") or 0)},
            apply_policy_guard={"confirm_apply_required": True, "confirm_apply_received": True},
            before_after_audit={
                "before_node_count": len(list(graph.get("nodes") or [])),
                "after_node_count": len(list(updated_graph.get("nodes") or [])),
            },
            latency_profile={"mode": "core_raw_mcp"},
            completeness={"status": "applied"},
        )

    return _with_brain(payload.brain_id, run)


def _memory_os_list(tool_name: str, field_name: str, payload: McpMemoryOSListRequest) -> McpMaintenanceToolExecutionResponse:
    def run(record: dict[str, Any]) -> McpMaintenanceToolExecutionResponse:
        from sqlite_store import fetch_recent_maintenance_runs

        resolved_brain_id = str(record.get("brain_id") or payload.brain_id or "")
        processes = fetch_recent_maintenance_runs(limit=payload.limit, include_report=False)
        values: list[dict[str, Any]] = []
        if field_name == "processes":
            values = processes
        response_kwargs: dict[str, Any] = {
            "schema_version": "agvm.mcp_maintenance_tool_execution.v1",
            "brain_id": resolved_brain_id,
            "tool_name": tool_name,
            "status": "ok",
            "maintenance_report": {
                "schema_version": "agvm.memory_os_list.core_raw.v1",
                "field": field_name,
                "count": len(values),
                "recent_process_count": len(processes),
            },
            "summary": {"field": field_name, "count": len(values)},
            "process": {"recent_processes": processes[: payload.limit]},
        }
        if field_name == "open_questions":
            response_kwargs["open_questions"] = values
        elif field_name == "hypotheses":
            response_kwargs["hypotheses"] = values
        elif field_name == "contradictions":
            response_kwargs["entries"] = values
        return McpMaintenanceToolExecutionResponse(**response_kwargs)

    return _with_brain(payload.brain_id, run)
