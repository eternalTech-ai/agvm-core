# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import time
import uuid

from fastapi import APIRouter, HTTPException

from brain_registry import BrainRegistryError, resolve_brain_scope
from derivation import persist_selection, preview_bundle
from local_module_manifest_router import MAINTAIN_MODULE_ID, ensure_local_module_entitled
from retrieval import build_index
from runtime_scope import use_runtime_brain
from schemas import (
    McpClarificationRequest,
    McpGrowApplyRequest,
    McpGrowSourceRequest,
    McpGrowToolExecutionResponse,
    McpMaintenanceApplyRequest,
    McpMaintenanceRequest,
    McpMaintenanceToolExecutionResponse,
    McpWriteMemoryCommitRequest,
    McpWriteMemoryPreviewRequest,
)
from sqlite_store import bootstrap_runtime_store, fetch_atlas, fetch_graph_snapshot, replace_runtime_graph


_GROW_PREVIEW_RUNS: dict[str, dict[str, Any]] = {}


def create_core_mcp_ops_router() -> APIRouter:
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
        guided_options = payload.options.model_copy(
            update={"pause_on_questions": True, "clarification_default_policy": "pause_when_unanswered"}
        )
        return _grow_source_preview("grow_guided", payload.model_copy(update={"options": guided_options}))

    @router.post("/memory/mcp/grow-source-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_apply(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_apply("grow_source_apply", payload)

    @router.post("/memory/mcp/grow-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_apply(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_apply("grow_apply", payload)

    @router.post("/memory/mcp/grow-source-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_status(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_status("grow_source_status", payload)

    @router.post("/memory/mcp/grow-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_status(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_status("grow_status", payload)

    @router.post("/memory/mcp/write-memory-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/write-memory-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def write_memory_preview(payload: McpWriteMemoryPreviewRequest) -> McpGrowToolExecutionResponse:
        return _write_memory_preview("write_memory_preview", payload)

    @router.post("/memory/mcp/write-memory-commit", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/write-memory-commit", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def write_memory_commit(payload: McpWriteMemoryCommitRequest) -> McpGrowToolExecutionResponse:
        return _write_memory_commit("write_memory_commit", payload)

    @router.post("/memory/mcp/ask-memory-clarification", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/ask-memory-clarification", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def ask_memory_clarification(payload: McpClarificationRequest) -> McpGrowToolExecutionResponse:
        return _ask_memory_clarification("ask_memory_clarification", payload)

    @router.post("/memory/mcp/sleep-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        _ensure_maintain_studio_entitled()
        return _maintenance_preview("sleep_preview", "sleep", payload)

    @router.post("/memory/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        _ensure_maintain_studio_entitled()
        return _maintenance_preview("evolve_preview", "evolve", payload)

    @router.post("/memory/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        _ensure_maintain_studio_entitled()
        return _maintenance_apply("sleep_apply", "sleep", payload)

    @router.post("/memory/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        _ensure_maintain_studio_entitled()
        return _maintenance_apply("evolve_apply", "evolve", payload)

    return router


def _ensure_maintain_studio_entitled() -> None:
    ensure_local_module_entitled(MAINTAIN_MODULE_ID)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_brain_record(brain_id: str | None = None) -> dict[str, Any]:
    try:
        return resolve_brain_scope(brain_id=str(brain_id or "").strip() or None)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _brain_record_id(record: dict[str, Any]) -> str:
    return str(record.get("brain_id") or record.get("id") or "").strip()


def _input_mode(payload: McpGrowSourceRequest) -> str:
    kind = str(payload.input_kind or "auto")
    return "document" if kind in {"pdf", "docx", "website", "url", "transcript", "mixed_bundle"} else "manual"


def _source_type(payload: McpGrowSourceRequest) -> str:
    options = payload.options
    if payload.input_kind in {"website", "url"}:
        return "public_web_metadata" if options.metadata_only else "external_reference"
    if payload.input_kind in {"pdf", "docx", "transcript", "mixed_bundle"}:
        return "uploaded_document"
    return str(options.treat_as or "self_memory")


def _selected_preview_ids(bundle: dict[str, Any], payload_ids: list[str]) -> list[str]:
    ids = [str(item) for item in list(payload_ids or []) if str(item or "").strip()]
    if ids:
        return ids
    primary_id = str(dict(bundle.get("primary_node_preview") or {}).get("id") or "")
    derived_ids = [str(dict(node).get("id") or "") for node in list(bundle.get("derived_nodes") or [])]
    return [item for item in [primary_id, *derived_ids] if item]


def _grow_source_preview(tool_name: str, payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_record = _resolve_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        index_payload = build_index(list(graph.get("nodes") or []))
        atlas_payload = fetch_atlas()
        options = payload.options
        investigation_id = f"mcp-grow-{uuid.uuid4()}"
        bundle = preview_bundle(
            payload.raw_input,
            _input_mode(payload),
            graph,
            index_payload,
            atlas_payload,
            source_label=payload.source_label,
            source_type=_source_type(payload),
            source_trust=str(options.source_trust or "unknown"),
            learning_mode="guided_learning" if options.pause_on_questions else "strict_review",
            question_limit=options.question_limit,
            source_investigation_id=investigation_id,
            source_purpose=payload.user_instruction,
            operator_instruction=payload.user_instruction,
            compiler_timeout_seconds=options.compiler_preview_timeout_seconds,
        )
    source_investigation = {
        "schema_version": "agvm.mcp_source_investigation.v1",
        "investigation_id": investigation_id,
        "brain_id": brain_id,
        "source_label": payload.source_label,
        "source_uri": payload.source_uri,
        "input_kind": payload.input_kind,
        "created_at": _utc_now(),
        "status": "preview_ready",
    }
    _GROW_PREVIEW_RUNS[investigation_id] = {
        "brain_id": brain_id,
        "source_investigation": source_investigation,
        "preview_bundle": bundle,
    }
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="preview_ready",
        source_investigation=source_investigation,
        source_formation_contract={
            "schema_version": "agvm.core_source_formation_contract.v1",
            "mode": "local_core_preview",
            "mutates_memory": False,
            "apply_requires_confirm_apply": True,
        },
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "grow",
            "phase": "preview",
            "next_action": "call grow_source_apply with confirm_apply=true and selected_preview_ids",
        },
        preview_bundle=bundle,
        cognitive_write_plan=dict(bundle.get("cognitive_write_plan") or {}),
        learning_policy=dict(bundle.get("learning_policy") or {}),
        write_trace=dict(bundle.get("write_trace") or {}),
        completeness={"preview_generated": True, "selected_preview_count": len(_selected_preview_ids(bundle, []))},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_source_apply(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    investigation = dict(payload.source_investigation or {})
    investigation_id = str(payload.investigation_id or investigation.get("investigation_id") or "").strip()
    stored = dict(_GROW_PREVIEW_RUNS.get(investigation_id) or {}) if investigation_id else {}
    bundle = payload.preview_bundle or stored.get("preview_bundle")
    brain_id = str(payload.brain_id or stored.get("brain_id") or investigation.get("brain_id") or "").strip() or None
    if not bundle:
        return _grow_blocked(tool_name, brain_id, "preview_bundle_required", started)
    if not payload.confirm_apply:
        return _grow_blocked(tool_name, brain_id, "confirm_apply_required", started, preview_bundle=bundle)
    brain_record = _resolve_brain_record(brain_id)
    resolved_brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        selected_ids = _selected_preview_ids(bundle, payload.selected_preview_ids)
        updated_graph, persisted_ids, persisted_edge_count, merged_ids, learning_policy = persist_selection(
            bundle,
            selected_ids,
            graph,
            build_index(list(graph.get("nodes") or [])),
            learning_mode=payload.learning_mode,
            clarification_answers=payload.clarification_answers,
            approved_preview_ids=payload.approved_preview_ids,
            question_limit=payload.question_limit,
        )
        replace_runtime_graph(updated_graph)
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=resolved_brain_id,
        tool_name=tool_name,
        status="applied",
        source_investigation={
            **dict(stored.get("source_investigation") or investigation),
            "investigation_id": investigation_id,
            "status": "applied",
            "applied_at": _utc_now(),
        },
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "grow",
            "phase": "applied",
            "confirm_apply": True,
            "partial_merge_allowed": False,
        },
        preview_bundle=bundle,
        persist_result={
            "schema_version": "agvm.core_grow_persist_result.v1",
            "persisted_node_ids": persisted_ids,
            "persisted_edge_count": persisted_edge_count,
            "merged_into_existing_ids": merged_ids,
            "selected_preview_ids": selected_ids,
        },
        learning_policy=learning_policy,
        completeness={"applied": True, "persisted_node_count": len(persisted_ids)},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_source_status(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    investigation = dict(payload.source_investigation or {})
    investigation_id = str(payload.investigation_id or investigation.get("investigation_id") or "").strip()
    stored = dict(_GROW_PREVIEW_RUNS.get(investigation_id) or {}) if investigation_id else {}
    if not stored:
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=payload.brain_id,
            tool_name=tool_name,
            status="blocked",
            source_investigation={"investigation_id": investigation_id, "status": "not_found"},
            memory_operation_lifecycle_contract={"blocked_reason": "investigation_not_found"},
            budget={"credits_required": 0, "runtime": "local_core"},
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=str(stored.get("brain_id") or ""),
        tool_name=tool_name,
        status="preview_ready",
        source_investigation=dict(stored.get("source_investigation") or {}),
        preview_bundle=dict(stored.get("preview_bundle") or {}),
        memory_operation_lifecycle_contract={"phase": "preview", "next_action": "apply with confirm_apply=true"},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _write_memory_preview(tool_name: str, payload: McpWriteMemoryPreviewRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_record = _resolve_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        bundle = preview_bundle(
            payload.text,
            payload.input_mode,
            graph,
            build_index(list(graph.get("nodes") or [])),
            fetch_atlas(),
            source_label=payload.source_label,
            source_type=payload.source_type or "self_memory",
            source_trust=str(payload.source_trust or "user_asserted"),
            learning_mode=payload.learning_mode,
            question_limit=payload.question_limit,
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="preview_ready",
        preview_bundle=bundle,
        cognitive_write_plan=dict(bundle.get("cognitive_write_plan") or {}),
        learning_policy=dict(bundle.get("learning_policy") or {}),
        write_trace=dict(bundle.get("write_trace") or {}),
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "write_memory",
            "phase": "preview",
            "mutates_memory": False,
            "next_action": "call write_memory_commit with preview_bundle as bundle and confirm_apply=true",
        },
        completeness={
            "preview_generated": True,
            "selected_preview_count": len(_selected_preview_ids(bundle, [])),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _write_memory_commit(tool_name: str, payload: McpWriteMemoryCommitRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    bundle = payload.bundle
    brain_id = str(payload.brain_id or "").strip() or None
    if bundle is None and payload.text:
        preview = _write_memory_preview(
            "write_memory_preview",
            McpWriteMemoryPreviewRequest(
                brain_id=brain_id,
                text=payload.text,
                input_mode=payload.input_mode,
                source_label=payload.source_label,
                source_type=payload.source_type,
                source_trust=payload.source_trust,
                learning_mode=payload.learning_mode,
                question_limit=payload.question_limit,
            ),
        )
        bundle = dict(preview.preview_bundle or {})
        brain_id = preview.brain_id
    if not bundle:
        return _grow_blocked(tool_name, brain_id, "preview_bundle_or_text_required", started)
    if not payload.confirm_apply:
        return _grow_blocked(tool_name, brain_id, "confirm_apply_required", started, preview_bundle=bundle)
    brain_record = _resolve_brain_record(brain_id)
    resolved_brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        selected_ids = _selected_preview_ids(bundle, payload.selected_preview_ids)
        updated_graph, persisted_ids, persisted_edge_count, merged_ids, learning_policy = persist_selection(
            bundle,
            selected_ids,
            graph,
            build_index(list(graph.get("nodes") or [])),
            learning_mode=payload.learning_mode,
            clarification_answers=payload.clarification_answers,
            approved_preview_ids=payload.approved_preview_ids,
            question_limit=payload.question_limit,
        )
        replace_runtime_graph(updated_graph)
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=resolved_brain_id,
        tool_name=tool_name,
        status="applied",
        preview_bundle=bundle,
        persist_result={
            "schema_version": "agvm.core_write_persist_result.v1",
            "persisted_node_ids": persisted_ids,
            "persisted_edge_count": persisted_edge_count,
            "merged_into_existing_ids": merged_ids,
            "selected_preview_ids": selected_ids,
        },
        learning_policy=learning_policy,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "write_memory",
            "phase": "applied",
            "confirm_apply": True,
            "partial_merge_allowed": False,
        },
        completeness={"applied": True, "persisted_node_count": len(persisted_ids)},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _ask_memory_clarification(tool_name: str, payload: McpClarificationRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    text = payload.text or payload.raw_input or payload.user_instruction or ""
    questions = [
        {
            "question_id": "clarify-source-and-scope",
            "question": "What should this memory be used for, and should it be treated as a fact, preference, project note, or source-backed evidence?",
            "required": True,
        }
    ]
    if payload.source_uri:
        questions.append(
            {
                "question_id": "clarify-source-trust",
                "question": "Should this source be treated as verified public evidence or as user-provided context?",
                "required": False,
            }
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=payload.brain_id,
        tool_name=tool_name,
        status="asking_clarification",
        clarification_questions=questions[: max(1, min(payload.question_limit, len(questions)))],
        source_investigation={
            "schema_version": "agvm.mcp_clarification_request.v1",
            "input_preview": text[:240],
            "source_label": payload.source_label,
            "source_uri": payload.source_uri,
        },
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "write_memory",
            "phase": "clarification",
            "mutates_memory": False,
            "next_action": "answer clarification questions, then call write_memory_preview or grow_source_preview",
        },
        completeness={"question_count": len(questions[: max(1, min(payload.question_limit, len(questions)))])},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_blocked(
    tool_name: str,
    brain_id: str | None,
    reason: str,
    started: float,
    *,
    preview_bundle: dict[str, Any] | None = None,
) -> McpGrowToolExecutionResponse:
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="blocked",
        preview_bundle=preview_bundle,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "blocked_reason": reason,
            "mutates_memory": False,
        },
        completeness={"blocked": True, "reason": reason},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _maintenance_preview(tool_name: str, mode: str, payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
    started = time.perf_counter()
    brain_record = _resolve_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        report = _build_core_maintenance_report(
            graph,
            mode=mode,
            preview_only=True,
            focus_node_id=payload.focus_node_id,
            max_nodes_considered=payload.max_nodes_considered,
            selected_proposal_ids=[],
        )
    return _maintenance_response(tool_name, brain_id, "preview_ready", report, started)


def _maintenance_apply(tool_name: str, mode: str, payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
    started = time.perf_counter()
    if not payload.confirm_apply:
        return _maintenance_response(
            tool_name,
            payload.brain_id,
            "blocked",
            {
                "applied": False,
                "mode": mode,
                "apply_policy_guard": {
                    "blocked": True,
                    "blocked_reason": "confirm_apply_required",
                    "partial_merge_allowed": False,
                },
            },
            started,
        )
    brain_record = _resolve_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        report = _build_core_maintenance_report(
            graph,
            mode=mode,
            preview_only=False,
            focus_node_id=payload.focus_node_id,
            max_nodes_considered=payload.max_nodes_considered,
            selected_proposal_ids=payload.proposal_ids,
        )
        replace_runtime_graph(graph)
    return _maintenance_response(tool_name, brain_id, "applied", report, started)


def _maintenance_response(
    tool_name: str,
    brain_id: str | None,
    status: str,
    report: dict[str, Any],
    started: float,
) -> McpMaintenanceToolExecutionResponse:
    proposals = list(report.get("maintenance_proposals") or [])
    return McpMaintenanceToolExecutionResponse(
        schema_version="agvm.mcp_maintenance_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status=status,  # type: ignore[arg-type]
        maintenance_report=report,
        maintenance_proposals=proposals,
        elastic_topology_proposals=list(report.get("elastic_topology_proposals") or []),
        maintenance_truth_contract=dict(report.get("maintenance_contract") or {}),
        sleep_evolve_lifecycle_contract={
            "schema_version": "agvm.sleep_evolve_lifecycle_contract.v1",
            "tool_name": tool_name,
            "mode": report.get("mode"),
            "applied": bool(report.get("applied")),
            "partial_merge_allowed": False,
        },
        maintenance_transaction=dict(report.get("maintenance_transaction") or {}),
        preview_budget_guard=dict(report.get("preview_budget_guard") or {}),
        maintenance_preview_plan=dict(report.get("maintenance_preview_plan") or {}),
        memory_operation_lifecycle_contract={
            "operation": "sleep_evolve",
            "phase": "applied" if bool(report.get("applied")) else "preview",
            "tool_name": tool_name,
            "requires_confirm_apply_for_mutation": True,
        },
        proposal_review_table=[
            {
                "proposal_id": str(item.get("proposal_id") or item.get("id") or ""),
                "kind": item.get("kind") or item.get("proposal_kind"),
                "summary": item.get("summary") or item.get("reason") or item.get("title"),
            }
            for item in proposals[:40]
        ],
        metamemory_snapshot=dict(report.get("metamemory_snapshot") or {}),
        apply_policy_guard=dict(report.get("apply_policy_guard") or {}),
        rollback_snapshot=dict(report.get("rollback_snapshot") or {}),
        before_after_audit=dict(report.get("before_after_audit") or {}),
        no_corruption_guards=dict(report.get("no_corruption_guards") or {}),
        mutation_surface={"runtime": "local_core", "credits_required": 0},
        maintenance_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        open_questions=list(report.get("open_questions") or []),
        hypotheses=list(report.get("hypotheses") or []),
        contradictions=list(report.get("contradictions") or []),
        processes=list(report.get("processes") or []),
        source_trace=list(report.get("source_trace") or []),
        completeness={"proposal_count": len(proposals), "status": status},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _build_core_maintenance_report(
    graph: dict[str, Any],
    *,
    mode: str,
    preview_only: bool,
    focus_node_id: str | None,
    max_nodes_considered: int,
    selected_proposal_ids: list[str],
) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or [])]
    edges = [dict(edge) for edge in list(graph.get("edges") or [])]
    if focus_node_id:
        selected_nodes = [node for node in nodes if str(node.get("id") or "") == str(focus_node_id)]
    else:
        selected_nodes = nodes[: max(10, min(int(max_nodes_considered or 80), 500))]
    proposals: list[dict[str, Any]] = []
    if mode == "sleep":
        low_confidence = [
            node
            for node in selected_nodes
            if float(node.get("memory_confidence") or node.get("confidence") or 0.75) < 0.55
        ][:12]
        for node in low_confidence:
            proposals.append(
                {
                    "proposal_id": f"sleep-review-{node.get('id')}",
                    "kind": "confidence_review",
                    "node_id": node.get("id"),
                    "summary": "Review a low-confidence local memory before it influences retrieval.",
                    "preview_only": bool(preview_only),
                }
            )
        if not proposals and selected_nodes:
            proposals.append(
                {
                    "proposal_id": "sleep-index-refresh",
                    "kind": "local_consolidation",
                    "summary": "Refresh local retrieval posture and keep the current graph unchanged.",
                    "candidate_node_count": len(selected_nodes),
                    "preview_only": bool(preview_only),
                }
            )
    else:
        isolated = []
        linked_ids: set[str] = set()
        for edge in edges:
            linked_ids.add(str(edge.get("source") or edge.get("source_id") or ""))
            linked_ids.add(str(edge.get("target") or edge.get("target_id") or ""))
        for node in selected_nodes:
            if str(node.get("id") or "") not in linked_ids:
                isolated.append(node)
        for node in isolated[:12]:
            proposals.append(
                {
                    "proposal_id": f"evolve-connect-{node.get('id')}",
                    "kind": "connection_candidate",
                    "node_id": node.get("id"),
                    "summary": "Find a stronger local neighborhood for an isolated memory.",
                    "preview_only": bool(preview_only),
                }
            )
        if not proposals and selected_nodes:
            proposals.append(
                {
                    "proposal_id": "evolve-neighborhood-scan",
                    "kind": "topology_scan",
                    "summary": "Scan the selected local memory neighborhood for future structural improvements.",
                    "candidate_node_count": len(selected_nodes),
                    "preview_only": bool(preview_only),
                }
            )
    selected_ids = [str(item) for item in list(selected_proposal_ids or []) if str(item or "").strip()]
    applied_ids = selected_ids or [str(item.get("proposal_id") or "") for item in proposals[:3] if item.get("proposal_id")]
    return {
        "schema_version": "agvm.core_maintenance_report.v1",
        "applied": not preview_only,
        "mode": mode,
        "preview_budget_guard": {
            "schema_version": "agvm.core_maintenance_budget_guard.v1",
            "preview_only": bool(preview_only),
            "requested_max_nodes_considered": max_nodes_considered,
            "selected_node_count": len(selected_nodes),
            "policy": "local_core_sleep_evolve_is_bounded_and_never_consumes_detwin_credits",
        },
        "maintenance_preview_plan": {
            "focus_node_id": focus_node_id,
            "selected_node_count": len(selected_nodes),
            "graph_node_count": len(nodes),
            "graph_edge_count": len(edges),
        },
        "maintenance_contract": {
            "schema_version": "agvm.core_maintenance_contract.v1",
            "runtime": "local_core",
            "advanced_maintain_runtime": "detwin_cloud_only",
            "mutation_requires_confirm_apply": True,
        },
        "metamemory_snapshot": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "reviewed_node_count": len(selected_nodes),
        },
        "maintenance_proposals": proposals,
        "maintenance_proposal_summary": {
            "proposal_count": len(proposals),
            "selected_for_apply_count": 0 if preview_only else len(applied_ids),
        },
        "apply_policy_guard": {
            "applied": not preview_only,
            "partial_merge_allowed": False,
            "selected_proposal_ids": [] if preview_only else applied_ids,
            "graph_mutation": "none" if not preview_only else "preview_only",
        },
        "maintenance_transaction": {
            "schema_version": "agvm.core_maintenance_transaction.v1",
            "transaction_id": f"core-maintenance-{uuid.uuid4()}",
            "mode": mode,
            "preview_only": bool(preview_only),
            "created_at": _utc_now(),
        },
        "before_after_audit": {
            "before_node_count": len(nodes),
            "after_node_count": len(nodes),
            "before_edge_count": len(edges),
            "after_edge_count": len(edges),
        },
        "no_corruption_guards": {
            "document_anchor_delete_blocked": True,
            "raw_memory_delete_blocked": True,
            "cloud_data_accessed": False,
        },
    }
