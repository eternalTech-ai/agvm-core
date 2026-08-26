# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Protocol
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
from sqlite_store import (
    bootstrap_runtime_store,
    fetch_atlas,
    fetch_graph_snapshot,
    replace_runtime_graph,
    store_maintenance_run,
)


_GROW_PREVIEW_RUNS: dict[str, dict[str, Any]] = {}
_GROW_PREVIEW_APPLY_LOCK = threading.RLock()


class MaintenanceMutationRuntime(Protocol):
    def preview(
        self,
        *,
        graph: dict[str, Any],
        mode: str,
        focus_node_id: str | None,
        max_nodes_considered: int,
    ) -> dict[str, Any]: ...

    def apply(
        self,
        *,
        graph: dict[str, Any],
        mode: str,
        focus_node_id: str | None,
        max_nodes_considered: int,
        expected_preview_signature: str | None = None,
        selected_proposal_ids: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def rollback(
        self,
        *,
        mode: str,
        preview_signature: str,
    ) -> dict[str, Any]: ...


def create_core_mcp_ops_router(
    *,
    maintenance_runtime: MaintenanceMutationRuntime | None = None,
) -> APIRouter:
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
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_preview(
            "sleep_preview",
            "sleep",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_preview(
            "evolve_preview",
            "evolve",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_apply(
            "sleep_apply",
            "sleep",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_apply(
            "evolve_apply",
            "evolve",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/sleep-rollback", response_model_exclude_none=True)
    @router.post("/mcp/sleep-rollback", response_model_exclude_none=True)
    def sleep_rollback(payload: dict[str, Any]) -> dict[str, Any]:
        brain_record = _resolve_bootstrap_ready_brain_record(str(payload.get("brain_id") or "").strip() or None)
        _ensure_maintain_studio_entitled()
        return _maintenance_rollback(
            "sleep_rollback",
            "sleep",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/evolve-rollback", response_model_exclude_none=True)
    @router.post("/mcp/evolve-rollback", response_model_exclude_none=True)
    def evolve_rollback(payload: dict[str, Any]) -> dict[str, Any]:
        brain_record = _resolve_bootstrap_ready_brain_record(str(payload.get("brain_id") or "").strip() or None)
        _ensure_maintain_studio_entitled()
        return _maintenance_rollback(
            "evolve_rollback",
            "evolve",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

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


def _resolve_bootstrap_ready_brain_record(brain_id: str | None = None) -> dict[str, Any]:
    brain_record = _resolve_brain_record(brain_id)
    if int(brain_record.get("node_count") or 0) > 0:
        return brain_record
    raise HTTPException(
        status_code=409,
        detail={
            "code": "brain_bootstrap_required",
            "message": "Complete Brain Bootstrap before using Grow, Sleep or Evolve.",
            "brain_id": _brain_record_id(brain_record) or None,
        },
    )


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
    ids = _normalized_grow_ids(payload_ids)
    if ids:
        return ids
    primary_id = str(dict(bundle.get("primary_node_preview") or {}).get("id") or "")
    derived_ids = [str(dict(node).get("id") or "") for node in list(bundle.get("derived_nodes") or [])]
    return _normalized_grow_ids([primary_id, *derived_ids])


def _normalized_grow_ids(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        node_id = str(value or "").strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        normalized.append(node_id)
    return normalized


def _grow_contract_fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _grow_apply_fingerprint(
    *,
    investigation_id: str,
    brain_id: str,
    preview_fingerprint: str,
    selected_preview_ids: list[str],
    payload: McpGrowApplyRequest,
) -> str:
    return _grow_contract_fingerprint(
        {
            "investigation_id": investigation_id,
            "brain_id": brain_id,
            "preview_fingerprint": preview_fingerprint,
            "selected_preview_ids": sorted(selected_preview_ids),
            "learning_mode": payload.learning_mode,
            "clarification_answers": payload.clarification_answers,
            "approved_preview_ids": sorted(_normalized_grow_ids(payload.approved_preview_ids)),
            "question_limit": payload.question_limit,
        }
    )


def _grow_idempotent_replay(
    tool_name: str,
    receipt: dict[str, Any],
    *,
    apply_fingerprint: str,
    started: float,
) -> McpGrowToolExecutionResponse | None:
    if str(receipt.get("apply_fingerprint") or "") != apply_fingerprint:
        return None
    response_payload = deepcopy(dict(receipt.get("response") or {}))
    if not response_payload:
        return None
    response_payload["tool_name"] = tool_name
    persist_result = dict(response_payload.get("persist_result") or {})
    persist_result["idempotent_replay"] = True
    response_payload["persist_result"] = persist_result
    completeness = dict(response_payload.get("completeness") or {})
    completeness["idempotent_replay"] = True
    response_payload["completeness"] = completeness
    lifecycle = dict(response_payload.get("memory_operation_lifecycle_contract") or {})
    lifecycle.update(
        {
            "phase": "applied",
            "receipt_id": receipt.get("receipt_id"),
            "idempotent_replay": True,
        }
    )
    response_payload["memory_operation_lifecycle_contract"] = lifecycle
    response_payload["mcp_latency_profile"] = {"elapsed_ms": int((time.perf_counter() - started) * 1000)}
    return McpGrowToolExecutionResponse(**response_payload)


def _local_core_source_unit(payload: McpGrowSourceRequest, investigation_id: str) -> dict[str, Any]:
    raw_text = str(payload.raw_input or "").strip()
    source_unit_id = f"src_{investigation_id.removeprefix('mcp-grow-')}"
    return {
        "unit_id": source_unit_id,
        "kind": "manual_block" if payload.input_kind == "manual_text" else "external_reference",
        "title": str(payload.source_label or "Local Grow source"),
        "source_uri": payload.source_uri,
        "source_type": str(payload.input_kind or "auto"),
        "raw_text": raw_text,
        "char_count": len(raw_text),
        "token_estimate": max(1, (len(raw_text) + 3) // 4),
        "confidence": 0.96 if payload.input_kind == "manual_text" else 0.74,
        "fact_eligible": True,
        "status": "available",
    }


def _bind_preview_bundle_to_source_unit(bundle: dict[str, Any], source_unit: dict[str, Any]) -> dict[str, Any]:
    source_unit_id = str(source_unit.get("unit_id") or "")
    if not source_unit_id:
        return bundle
    bound = dict(bundle)
    primary = dict(bound.get("primary_node_preview") or {})
    if primary:
        if not primary.get("source_unit_id"):
            primary["source_unit_id"] = source_unit_id
        if not primary.get("source_unit_title"):
            primary["source_unit_title"] = source_unit.get("title")
        if not primary.get("source_unit_kind"):
            primary["source_unit_kind"] = source_unit.get("kind")
        bound["primary_node_preview"] = primary
    derived_nodes: list[dict[str, Any]] = []
    for raw_node in list(bound.get("derived_nodes") or []):
        node = dict(raw_node or {})
        if not node.get("source_unit_id"):
            node["source_unit_id"] = source_unit_id
        if not node.get("source_unit_title"):
            node["source_unit_title"] = source_unit.get("title")
        if not node.get("source_unit_kind"):
            node["source_unit_kind"] = source_unit.get("kind")
        derived_nodes.append(node)
    bound["derived_nodes"] = derived_nodes
    return bound


def _grow_source_preview(tool_name: str, payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    investigation_id = f"mcp-grow-{uuid.uuid4()}"
    source_unit = _local_core_source_unit(payload, investigation_id)
    if not payload.run_preview:
        source_investigation = {
            "schema_version": "agvm.source_investigation_package.v1",
            "investigation_id": investigation_id,
            "brain_id": brain_id,
            "status": "preview_ready",
            "source_request": {
                "brain_id": brain_id,
                "source_label": payload.source_label,
                "source_uri": payload.source_uri,
                "input_kind": payload.input_kind,
                "run_preview": False,
            },
            "source_detection": {
                "source_kind": str(payload.input_kind or "auto"),
                "confidence": source_unit.get("confidence"),
            },
            "source_units": [source_unit],
            "compiler_handoff": {
                "preview_eligible": True,
                "recommended_input_mode": _input_mode(payload),
                "recommended_learning_mode": "guided_learning" if payload.options.pause_on_questions else "strict_review",
            },
            "compiler_handoff_proof": {"proof_passed": True},
            "budgets": {"max_units": payload.options.max_units},
            "budget_usage": {"source_units": 1},
        }
        source_formation_contract = {
            "schema_version": "agvm.core_source_formation_contract.v1",
            "mode": "local_core_source_unit_proof",
            "mutates_memory": False,
            "apply_requires_confirm_apply": True,
            "state": "handoff_ready",
            "source_kind": str(payload.input_kind or "auto"),
            "investigation_id": investigation_id,
            "apply_contract": {
                "preview_required": True,
                "explicit_confirm_apply_required": True,
                "apply_without_preview_allowed": False,
                "can_apply_now": False,
                "blocked_reasons": ["preview_bundle_missing"],
                "selected_preview_ids": [],
            },
        }
        latency_profile = {
            "schema_version": "agvm.mcp_grow_latency_profile.v1",
            "mode": "source_unit_only",
            "source_unit_only": True,
            "source_unit_proof_ready": True,
            "source_unit_count": 1,
            "compiler_handoff_visible": True,
            "preview_eligible": True,
            "full_preview_present": False,
            "apply_requires_preview_bundle": True,
            "apply_ready": False,
            "recommended_follow_up": "grow_source_preview",
            "recommended_follow_up_payload_patch": {"run_preview": True},
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
        _GROW_PREVIEW_RUNS[investigation_id] = {
            "brain_id": brain_id,
            "status": "preview_ready",
            "source_investigation": source_investigation,
            "source_formation_contract": source_formation_contract,
            "preview_bundle": None,
            "preview_fingerprint": None,
            "apply_receipt": None,
        }
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=brain_id,
            tool_name=tool_name,
            status="preview_ready",
            source_investigation=source_investigation,
            source_formation_contract=source_formation_contract,
            memory_operation_lifecycle_contract={
                "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
                "operation": "grow",
                "phase": "source_unit_proof",
                "next_action": "call grow_source_preview with run_preview=true before apply",
            },
            compiler_handoff_proof={"proof_passed": True},
            completeness={
                "preview_generated": False,
                "preview_present": False,
                "preview_node_count": 0,
                "selected_preview_count": 0,
                "source_status": "source_units_ready",
                "source_unit_count": 1,
            },
            mcp_latency_profile=latency_profile,
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action="call grow_source_preview with run_preview=true",
        )
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        index_payload = build_index(list(graph.get("nodes") or []))
        atlas_payload = fetch_atlas()
        options = payload.options
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
    bundle = _bind_preview_bundle_to_source_unit(bundle, source_unit)
    selected_preview_ids = _selected_preview_ids(bundle, [])
    source_investigation = {
        "schema_version": "agvm.mcp_source_investigation.v1",
        "investigation_id": investigation_id,
        "brain_id": brain_id,
        "source_label": payload.source_label,
        "source_uri": payload.source_uri,
        "input_kind": payload.input_kind,
        "created_at": _utc_now(),
        "status": "preview_ready",
        "source_units": [source_unit],
    }
    source_formation_contract = {
        "schema_version": "agvm.core_source_formation_contract.v1",
        "mode": "local_core_preview",
        "mutates_memory": False,
        "apply_requires_confirm_apply": True,
        "state": "preview_ready",
        "source_kind": str(payload.input_kind or "auto"),
        "investigation_id": investigation_id,
        "apply_contract": {
            "preview_required": True,
            "explicit_confirm_apply_required": True,
            "apply_without_preview_allowed": False,
            "can_apply_now": bool(selected_preview_ids),
            "blocked_reasons": [] if selected_preview_ids else ["preview_bundle_missing"],
            "selected_preview_ids": selected_preview_ids,
        },
    }
    _GROW_PREVIEW_RUNS[investigation_id] = {
        "brain_id": brain_id,
        "status": "preview_ready",
        "source_investigation": source_investigation,
        "source_formation_contract": source_formation_contract,
        "preview_bundle": bundle,
        "preview_fingerprint": _grow_contract_fingerprint(bundle),
        "apply_receipt": None,
    }
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="preview_ready",
        source_investigation=source_investigation,
        source_formation_contract=source_formation_contract,
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
        completeness={
            "preview_generated": True,
            "preview_node_count": len(selected_preview_ids),
            "selected_preview_count": len(selected_preview_ids),
            "source_status": "source_units_ready",
            "source_unit_count": 1,
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_source_apply(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    with _GROW_PREVIEW_APPLY_LOCK:
        return _grow_source_apply_locked(tool_name, payload)


def _grow_source_apply_locked(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    investigation = dict(payload.source_investigation or {})
    request_investigation_id = str(payload.investigation_id or "").strip()
    embedded_investigation_id = str(investigation.get("investigation_id") or "").strip()
    if request_investigation_id and embedded_investigation_id and request_investigation_id != embedded_investigation_id:
        return _grow_blocked(tool_name, payload.brain_id, "investigation_id_mismatch", started)
    investigation_id = request_investigation_id or embedded_investigation_id
    if not investigation_id:
        return _grow_blocked(tool_name, payload.brain_id, "server_preview_investigation_required", started)
    stored = dict(_GROW_PREVIEW_RUNS.get(investigation_id) or {})
    if not stored:
        return _grow_blocked(tool_name, payload.brain_id, "server_preview_not_found", started)
    bundle = deepcopy(dict(stored.get("preview_bundle") or {}))
    stored_brain_id = str(stored.get("brain_id") or "").strip()
    requested_brain_id = str(payload.brain_id or investigation.get("brain_id") or "").strip()
    if requested_brain_id and requested_brain_id != stored_brain_id:
        return _grow_blocked(tool_name, requested_brain_id, "server_preview_brain_mismatch", started)
    brain_id = stored_brain_id or None
    if not bundle:
        return _grow_blocked(tool_name, brain_id, "server_preview_bundle_required", started)
    preview_fingerprint = str(stored.get("preview_fingerprint") or _grow_contract_fingerprint(bundle))
    if payload.preview_bundle is not None and _grow_contract_fingerprint(payload.preview_bundle) != preview_fingerprint:
        return _grow_blocked(tool_name, brain_id, "server_preview_bundle_mismatch", started, preview_bundle=bundle)
    contract_investigation_id = str(dict(payload.source_formation_contract or {}).get("investigation_id") or "").strip()
    if contract_investigation_id and contract_investigation_id != investigation_id:
        return _grow_blocked(tool_name, brain_id, "server_preview_contract_mismatch", started, preview_bundle=bundle)
    if not payload.confirm_apply:
        return _grow_blocked(tool_name, brain_id, "confirm_apply_required", started, preview_bundle=bundle)
    available_ids = _selected_preview_ids(bundle, [])
    requested_ids = _normalized_grow_ids(payload.selected_preview_ids)
    unknown_selected_ids = [node_id for node_id in requested_ids if node_id not in set(available_ids)]
    if unknown_selected_ids:
        return _grow_blocked(tool_name, brain_id, "selected_preview_ids_not_server_issued", started, preview_bundle=bundle)
    unknown_approved_ids = [
        node_id
        for node_id in _normalized_grow_ids(payload.approved_preview_ids)
        if node_id not in set(available_ids)
    ]
    if unknown_approved_ids:
        return _grow_blocked(tool_name, brain_id, "approved_preview_ids_not_server_issued", started, preview_bundle=bundle)
    selected_ids = _selected_preview_ids(bundle, requested_ids)
    if not selected_ids:
        return _grow_blocked(tool_name, brain_id, "selected_preview_ids_required", started, preview_bundle=bundle)
    apply_fingerprint = _grow_apply_fingerprint(
        investigation_id=investigation_id,
        brain_id=stored_brain_id,
        preview_fingerprint=preview_fingerprint,
        selected_preview_ids=selected_ids,
        payload=payload,
    )
    receipt = dict(stored.get("apply_receipt") or {})
    if receipt:
        replay = _grow_idempotent_replay(
            tool_name,
            receipt,
            apply_fingerprint=apply_fingerprint,
            started=started,
        )
        if replay is not None:
            return replay
        return _grow_blocked(tool_name, brain_id, "apply_receipt_mismatch", started, preview_bundle=bundle)
    if str(stored.get("status") or "") == "applied":
        return _grow_blocked(tool_name, brain_id, "exact_apply_receipt_required", started, preview_bundle=bundle)
    brain_record = _resolve_bootstrap_ready_brain_record(brain_id)
    resolved_brain_id = _brain_record_id(brain_record)
    working_bundle = deepcopy(bundle)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        updated_graph, persisted_ids, persisted_edge_count, merged_ids, learning_policy = persist_selection(
            working_bundle,
            selected_ids,
            graph,
            build_index(list(graph.get("nodes") or [])),
            learning_mode=payload.learning_mode,
            clarification_answers=payload.clarification_answers,
            approved_preview_ids=payload.approved_preview_ids,
            question_limit=payload.question_limit,
        )
        persisted_ids = _normalized_grow_ids(persisted_ids)
        if not persisted_ids:
            return _grow_blocked(tool_name, resolved_brain_id, "zero_persisted_nodes", started, preview_bundle=bundle)
        before_node_ids = {
            str(dict(node).get("id") or "").strip()
            for node in list(graph.get("nodes") or [])
            if str(dict(node).get("id") or "").strip()
        }
        updated_node_ids = {
            str(dict(node).get("id") or "").strip()
            for node in list(updated_graph.get("nodes") or [])
            if str(dict(node).get("id") or "").strip()
        }
        if any(node_id in before_node_ids or node_id not in updated_node_ids for node_id in persisted_ids):
            return _grow_blocked(tool_name, resolved_brain_id, "persisted_node_proof_invalid", started, preview_bundle=bundle)
        committed_graph = replace_runtime_graph(updated_graph)
        committed_node_ids = {
            str(dict(node).get("id") or "").strip()
            for node in list(dict(committed_graph or {}).get("nodes") or [])
            if str(dict(node).get("id") or "").strip()
        }
        if len(committed_node_ids) <= len(before_node_ids) or any(node_id not in committed_node_ids for node_id in persisted_ids):
            return _grow_blocked(tool_name, resolved_brain_id, "persistence_mutation_not_verified", started, preview_bundle=bundle)
    applied_at = _utc_now()
    source_investigation = {
        **dict(stored.get("source_investigation") or {}),
        "investigation_id": investigation_id,
        "status": "applied",
        "applied_at": applied_at,
    }
    source_formation_contract = {
        **dict(stored.get("source_formation_contract") or {}),
        "state": "applied",
        "mutates_memory": True,
    }
    receipt_id = f"grow_apply_{_grow_contract_fingerprint({'apply': apply_fingerprint, 'persisted': persisted_ids})[:24]}"
    response = McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=resolved_brain_id,
        tool_name=tool_name,
        status="applied",
        source_investigation=source_investigation,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "grow",
            "phase": "applied",
            "confirm_apply": True,
            "partial_merge_allowed": False,
            "receipt_id": receipt_id,
            "idempotent_replay": False,
        },
        preview_bundle=bundle,
        persist_result={
            "schema_version": "agvm.core_grow_persist_result.v1",
            "persisted_node_ids": persisted_ids,
            "persisted_edge_count": persisted_edge_count,
            "merged_into_existing_ids": merged_ids,
            "selected_preview_ids": selected_ids,
            "idempotent_replay": False,
            "receipt_id": receipt_id,
        },
        learning_policy=learning_policy,
        completeness={
            "applied": True,
            "persisted_node_count": len(persisted_ids),
            "persisted_edge_count": persisted_edge_count,
            "idempotent_replay": False,
            "source_unit_count": len(list(dict(stored.get("source_investigation") or {}).get("source_units") or [])),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )
    receipt = {
        "schema_version": "agvm.core_grow_apply_receipt.v1",
        "receipt_id": receipt_id,
        "investigation_id": investigation_id,
        "brain_id": resolved_brain_id,
        "preview_fingerprint": preview_fingerprint,
        "apply_fingerprint": apply_fingerprint,
        "selected_preview_ids": selected_ids,
        "persisted_node_ids": persisted_ids,
        "applied_at": applied_at,
        "response": response.model_dump(exclude_none=True),
    }
    stored.update(
        {
            "brain_id": resolved_brain_id,
            "status": "applied",
            "source_investigation": source_investigation,
            "source_formation_contract": source_formation_contract,
            "preview_bundle": deepcopy(bundle),
            "preview_fingerprint": preview_fingerprint,
            "persist_result": deepcopy(response.persist_result),
            "apply_receipt": receipt,
        }
    )
    _GROW_PREVIEW_RUNS[investigation_id] = stored
    return response


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
    if str(stored.get("status") or "") == "applied":
        receipt = dict(stored.get("apply_receipt") or {})
        response_payload = deepcopy(dict(receipt.get("response") or {}))
        if response_payload:
            response_payload["tool_name"] = tool_name
            return McpGrowToolExecutionResponse(**response_payload)
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=str(stored.get("brain_id") or ""),
            tool_name=tool_name,
            status="blocked",
            source_investigation=dict(stored.get("source_investigation") or {}),
            source_formation_contract=dict(stored.get("source_formation_contract") or {}),
            preview_bundle=deepcopy(stored.get("preview_bundle")),
            memory_operation_lifecycle_contract={"blocked_reason": "exact_apply_receipt_required"},
            budget={"credits_required": 0, "runtime": "local_core"},
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=str(stored.get("brain_id") or ""),
        tool_name=tool_name,
        status="preview_ready",
        source_investigation=dict(stored.get("source_investigation") or {}),
        source_formation_contract=dict(stored.get("source_formation_contract") or {}),
        preview_bundle=deepcopy(stored.get("preview_bundle")),
        memory_operation_lifecycle_contract={
            "phase": "preview" if stored.get("preview_bundle") else "source_unit_proof",
            "next_action": "apply with confirm_apply=true" if stored.get("preview_bundle") else "run full preview before apply",
        },
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
    idempotent_replay = bool(selected_ids and not persisted_ids and merged_ids and persisted_edge_count == 0)
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
            "idempotent_replay": idempotent_replay,
        },
        learning_policy=learning_policy,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "write_memory",
            "phase": "applied",
            "confirm_apply": True,
            "partial_merge_allowed": False,
        },
        completeness={
            "applied": True,
            "persisted_node_count": len(persisted_ids),
            "idempotent_replay": idempotent_replay,
        },
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


def _maintenance_preview(
    tool_name: str,
    mode: str,
    payload: McpMaintenanceRequest,
    *,
    brain_record: dict[str, Any] | None = None,
    runtime: MaintenanceMutationRuntime | None = None,
) -> McpMaintenanceToolExecutionResponse:
    started = time.perf_counter()
    brain_record = brain_record or _resolve_bootstrap_ready_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        if runtime is None:
            report = _build_core_maintenance_report(
                graph,
                mode=mode,
                preview_only=True,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
                selected_proposal_ids=[],
            )
        else:
            report = runtime.preview(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
            )
        maintenance_id = str(report.get("maintenance_id") or uuid.uuid4())
        report["maintenance_id"] = maintenance_id
        store_maintenance_run(
            maintenance_id=maintenance_id,
            mode=mode,
            applied=False,
            preview_only=True,
            focus_node_id=payload.focus_node_id,
            report=report,
        )
    status = "blocked" if report.get("maintenance_store_error") else "preview_ready"
    return _maintenance_response(tool_name, brain_id, status, report, started)


def _maintenance_apply(
    tool_name: str,
    mode: str,
    payload: McpMaintenanceApplyRequest,
    *,
    brain_record: dict[str, Any] | None = None,
    runtime: MaintenanceMutationRuntime | None = None,
) -> McpMaintenanceToolExecutionResponse:
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
    if not payload.preview_signature:
        return _maintenance_response(
            tool_name,
            payload.brain_id,
            "blocked",
            {
                "applied": False,
                "mode": mode,
                "apply_policy_guard": {
                    "blocked": True,
                    "blocked_reason": "maintenance_preview_signature_required",
                    "blocked_reasons": ["maintenance_preview_signature_required"],
                    "partial_merge_allowed": False,
                    "graph_mutation": "none",
                },
            },
            started,
        )
    brain_record = brain_record or _resolve_bootstrap_ready_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    requested_ids = _normalized_proposal_ids(payload.proposal_ids)
    if payload.preview_signature and runtime is not None:
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph = fetch_graph_snapshot()
            applied_report = runtime.apply(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
                expected_preview_signature=payload.preview_signature,
                selected_proposal_ids=requested_ids,
            )
        available_ids = _normalized_proposal_ids(
            [item.get("proposal_id") for item in list(applied_report.get("maintenance_proposals") or [])]
        )
        missing_ids = [proposal_id for proposal_id in requested_ids if proposal_id not in available_ids]
        unselected_ids = [proposal_id for proposal_id in available_ids if proposal_id not in requested_ids]
        if not bool(applied_report.get("applied")):
            runtime_guard = dict(applied_report.get("apply_policy_guard") or {})
            runtime_reasons = [str(item) for item in list(runtime_guard.get("blocked_reasons") or []) if str(item)]
            runtime_blocked_reason = runtime_reasons[0] if runtime_reasons else "maintenance_safety_guard_blocked_apply"
            _mark_core_maintenance_apply_blocked(
                applied_report,
                blocked_reason=runtime_blocked_reason,
                requested_ids=requested_ids,
                available_ids=available_ids,
                missing_ids=missing_ids,
                unselected_ids=unselected_ids,
            )
            return _maintenance_response(tool_name, brain_id, "blocked", applied_report, started)
        _mark_core_maintenance_apply_succeeded(
            applied_report,
            requested_ids=requested_ids,
            available_ids=available_ids,
        )
        return _maintenance_response(tool_name, brain_id, "applied", applied_report, started)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        if runtime is None:
            report = _build_core_maintenance_report(
                graph,
                mode=mode,
                preview_only=True,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
                selected_proposal_ids=[],
            )
        else:
            report = runtime.preview(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
            )
    available_ids = _normalized_proposal_ids(
        [item.get("proposal_id") for item in list(report.get("maintenance_proposals") or [])]
    )
    missing_ids = [proposal_id for proposal_id in requested_ids if proposal_id not in available_ids]
    unselected_ids = [proposal_id for proposal_id in available_ids if proposal_id not in requested_ids]
    blocked_reason = _core_maintenance_apply_blocked_reason(
        requested_ids=requested_ids,
        available_ids=available_ids,
        missing_ids=missing_ids,
        unselected_ids=unselected_ids,
    )
    if blocked_reason is None and runtime is None:
        blocked_reason = "maintain_apply_runtime_not_configured"
    if blocked_reason is None:
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph = fetch_graph_snapshot()
            applied_report = runtime.apply(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
            )
        if not bool(applied_report.get("applied")):
            runtime_guard = dict(applied_report.get("apply_policy_guard") or {})
            runtime_reasons = [str(item) for item in list(runtime_guard.get("blocked_reasons") or []) if str(item)]
            runtime_blocked_reason = runtime_reasons[0] if runtime_reasons else "maintenance_safety_guard_blocked_apply"
            _mark_core_maintenance_apply_blocked(
                applied_report,
                blocked_reason=runtime_blocked_reason,
                requested_ids=requested_ids,
                available_ids=available_ids,
                missing_ids=[],
                unselected_ids=[],
            )
            return _maintenance_response(tool_name, brain_id, "blocked", applied_report, started)
        _mark_core_maintenance_apply_succeeded(
            applied_report,
            requested_ids=requested_ids,
            available_ids=available_ids,
        )
        return _maintenance_response(tool_name, brain_id, "applied", applied_report, started)
    _mark_core_maintenance_apply_blocked(
        report,
        blocked_reason=blocked_reason,
        requested_ids=requested_ids,
        available_ids=available_ids,
        missing_ids=missing_ids,
        unselected_ids=unselected_ids,
    )
    return _maintenance_response(tool_name, brain_id, "blocked", report, started)


def _maintenance_rollback(
    tool_name: str,
    mode: str,
    payload: dict[str, Any],
    *,
    brain_record: dict[str, Any] | None = None,
    runtime: MaintenanceMutationRuntime | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    brain_record = brain_record or _resolve_bootstrap_ready_brain_record(
        str(payload.get("brain_id") or "").strip() or None
    )
    brain_id = _brain_record_id(brain_record)
    preview_signature = str(payload.get("preview_signature") or "").strip()
    blocked_reason = None
    if payload.get("confirm_rollback") is not True:
        blocked_reason = "confirm_rollback_required"
    elif not preview_signature:
        blocked_reason = "preview_signature_required_for_rollback"
    elif runtime is None:
        blocked_reason = "reviewed_preview_rollback_not_configured"
    if blocked_reason:
        return _maintenance_rollback_response(
            tool_name=tool_name,
            brain_id=brain_id,
            mode=mode,
            preview_signature=preview_signature,
            status="blocked",
            started=started,
            error={"code": blocked_reason},
        )
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        result = runtime.rollback(mode=mode, preview_signature=preview_signature)
    error = dict(result.get("error") or {})
    if error or not bool(result.get("rolled_back")):
        return _maintenance_rollback_response(
            tool_name=tool_name,
            brain_id=brain_id,
            mode=mode,
            preview_signature=preview_signature,
            status="blocked",
            started=started,
            error=error or {"code": "maintenance_rollback_failed"},
            rollback_result=result,
        )
    return _maintenance_rollback_response(
        tool_name=tool_name,
        brain_id=brain_id,
        mode=mode,
        preview_signature=preview_signature,
        status="already_rolled_back" if bool(result.get("idempotent_replay")) else "rolled_back",
        started=started,
        rollback_result=result,
    )


def _maintenance_rollback_response(
    *,
    tool_name: str,
    brain_id: str,
    mode: str,
    preview_signature: str,
    status: str,
    started: float,
    rollback_result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "agvm.maintenance_preview_rollback.v1",
        "brain_id": brain_id,
        "tool_name": tool_name,
        "mode": mode,
        "preview_signature": preview_signature,
        "status": status,
        "rollback_result": dict(rollback_result or {}),
        "error": dict(error or {}) or None,
        "mutation_surface": {
            "runtime": "local_core",
            "credits_required": 0,
            "status": status,
            "rolled_back": status in {"rolled_back", "already_rolled_back"},
            "graph_mutation": "none" if status in {"blocked", "already_rolled_back"} else "restored",
            "revision_safe": True,
        },
        "maintenance_latency_profile": {
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
    }


def _mark_core_maintenance_apply_succeeded(
    report: dict[str, Any],
    *,
    requested_ids: list[str],
    available_ids: list[str],
) -> None:
    report["maintenance_proposal_summary"] = {
        **dict(report.get("maintenance_proposal_summary") or {}),
        "selected_for_apply_count": len(requested_ids),
    }
    report["apply_policy_guard"] = {
        **dict(report.get("apply_policy_guard") or {}),
        "applied": True,
        "blocked": False,
        "blocked_reason": None,
        "guard_passed": True,
        "partial_merge_allowed": False,
        "available_proposal_ids": available_ids,
        "selected_proposal_ids": requested_ids,
        "selected_missing_proposal_ids": [],
        "unselected_available_proposal_ids": [],
        "graph_mutation": "committed",
    }
    contract = dict(report.get("maintenance_contract") or {})
    contract.update(
        {
            "preview_non_mutating": False,
            "hidden_mutation_allowed": False,
            "apply_runtime": "local_core_maintain_runtime",
            "selection_exactness": {
                "exact": True,
                "requested_proposal_ids": requested_ids,
                "available_proposal_ids": available_ids,
                "missing_requested_proposal_ids": [],
                "unselected_available_proposal_ids": [],
            },
        }
    )
    report["maintenance_contract"] = contract


def _normalized_proposal_ids(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        proposal_id = str(value or "").strip()
        if not proposal_id or proposal_id in seen:
            continue
        seen.add(proposal_id)
        normalized.append(proposal_id)
    return normalized


def _core_maintenance_apply_blocked_reason(
    *,
    requested_ids: list[str],
    available_ids: list[str],
    missing_ids: list[str],
    unselected_ids: list[str],
) -> str | None:
    if not requested_ids:
        return "proposal_ids_required_for_exact_apply"
    if missing_ids:
        return "requested_proposal_ids_not_available"
    if not available_ids:
        return "no_applicable_proposals"
    if unselected_ids:
        return "partial_proposal_apply_not_supported"
    return None


def _mark_core_maintenance_apply_blocked(
    report: dict[str, Any],
    *,
    blocked_reason: str,
    requested_ids: list[str],
    available_ids: list[str],
    missing_ids: list[str],
    unselected_ids: list[str],
) -> None:
    exact_selection = bool(requested_ids and not missing_ids and not unselected_ids)
    report["applied"] = False
    report["maintenance_proposal_summary"] = {
        **dict(report.get("maintenance_proposal_summary") or {}),
        "selected_for_apply_count": len(requested_ids),
    }
    report["apply_policy_guard"] = {
        "applied": False,
        "blocked": True,
        "blocked_reason": blocked_reason,
        "blocked_reasons": list(
            dict.fromkeys(
                [blocked_reason]
                + [
                    str(reason)
                    for reason in list(dict(report.get("apply_policy_guard") or {}).get("blocked_reasons") or [])
                    if str(reason) and str(reason) != "preview_only"
                ]
            )
        ),
        "guard_passed": False,
        "partial_merge_allowed": False,
        "available_proposal_ids": available_ids,
        "selected_proposal_ids": requested_ids,
        "selected_missing_proposal_ids": missing_ids,
        "unselected_available_proposal_ids": unselected_ids,
        "graph_mutation": "none",
    }
    contract = dict(report.get("maintenance_contract") or {})
    contract.update(
        {
            "preview_non_mutating": True,
            "hidden_mutation_allowed": False,
            "apply_runtime": "maintain_module_or_detwin_cloud",
            "selection_exactness": {
                "exact": exact_selection,
                "requested_proposal_ids": requested_ids,
                "available_proposal_ids": available_ids,
                "missing_requested_proposal_ids": missing_ids,
                "unselected_available_proposal_ids": unselected_ids,
            },
        }
    )
    report["maintenance_contract"] = contract


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
            "state": status,
            "applied": bool(report.get("applied")),
            "partial_merge_allowed": False,
            "approval_gate": dict(report.get("apply_policy_guard") or {}),
        },
        maintenance_transaction=dict(report.get("maintenance_transaction") or {}),
        preview_budget_guard=dict(report.get("preview_budget_guard") or {}),
        maintenance_preview_plan=dict(report.get("maintenance_preview_plan") or {}),
        memory_operation_lifecycle_contract={
            "operation": "sleep_evolve",
            "phase": "blocked" if status == "blocked" else "applied" if bool(report.get("applied")) else "preview",
            "tool_name": tool_name,
            "requires_confirm_apply_for_mutation": True,
        },
        proposal_review_table=[
            {
                "proposal_id": str(item.get("proposal_id") or item.get("id") or ""),
                "kind": item.get("kind") or item.get("proposal_kind"),
                "summary": item.get("summary") or item.get("reason") or item.get("title") or item.get("proposed_action"),
            }
            for item in proposals[:40]
        ],
        metamemory_snapshot=dict(report.get("metamemory_snapshot") or {}),
        apply_policy_guard=dict(report.get("apply_policy_guard") or {}),
        rollback_snapshot=dict(report.get("rollback_snapshot") or {}),
        before_after_audit=dict(report.get("before_after_audit") or {}),
        no_corruption_guards=dict(report.get("no_corruption_guards") or {}),
        mutation_surface={
            "runtime": "local_core",
            "credits_required": 0,
            "status": status,
            "applied": bool(report.get("applied")),
            "graph_mutation": dict(report.get("apply_policy_guard") or {}).get("graph_mutation", "none"),
            "preview_non_mutating": not bool(report.get("applied")),
            "hidden_mutation_allowed": False,
        },
        maintenance_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        open_questions=list(report.get("open_questions") or []),
        hypotheses=list(report.get("hypotheses") or []),
        contradictions=list(report.get("contradictions") or []),
        processes=list(report.get("processes") or []),
        source_trace=list(report.get("source_trace") or []),
        completeness={
            "proposal_count": len(proposals),
            "status": status,
            "selected_proposal_ids": list(dict(report.get("apply_policy_guard") or {}).get("selected_proposal_ids") or []),
            "selected_missing_proposal_ids": list(
                dict(report.get("apply_policy_guard") or {}).get("selected_missing_proposal_ids") or []
            ),
        },
        budget={
            "credits_required": 0,
            "runtime": "local_core",
            "blocked_reason": dict(report.get("apply_policy_guard") or {}).get("blocked_reason"),
        },
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
    selected_ids = _normalized_proposal_ids(selected_proposal_ids)
    applied_ids = [] if preview_only else selected_ids
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
            "advanced_maintain_runtime": "maintain_module_or_detwin_cloud",
            "mutation_requires_confirm_apply": True,
            "preview_non_mutating": bool(preview_only),
            "hidden_mutation_allowed": False,
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
