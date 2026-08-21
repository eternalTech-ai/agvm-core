# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from brain_registry import BrainRegistryError, resolve_brain_scope
from brain_health import build_brain_health_report
from geometry_calibration import (
    apply_matrix_calibration_position_plan_to_graph,
    build_brain_geometry_calibration_report,
    build_matrix_calibration_position_plan,
)
from local_module_manifest_router import MAINTAIN_MODULE_ID, ensure_local_module_entitled
from matrix_revisioning import build_matrix_calibration_revision_candidates
from runtime_scope import current_brain_id, use_runtime_brain
from schemas import (
    McpMatrixCalibrationApplyRequest,
    McpMatrixCalibrationRequest,
    McpMatrixCalibrationToolExecutionResponse,
)
from sqlite_store import (
    apply_matrix_calibration_position_updates_with_revisions,
    bootstrap_runtime_store,
    fetch_active_matrix_revision,
    fetch_active_topology_field_revision,
    fetch_graph_snapshot,
    store_maintenance_run,
)
from storage import utc_timestamp


def create_core_mcp_matrix_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/memory/mcp/matrix-calibration-preview",
        response_model=McpMatrixCalibrationToolExecutionResponse,
        response_model_exclude_none=True,
    )
    @router.post(
        "/mcp/matrix-calibration-preview",
        response_model=McpMatrixCalibrationToolExecutionResponse,
        response_model_exclude_none=True,
    )
    def matrix_calibration_preview(
        payload: McpMatrixCalibrationRequest,
    ) -> McpMatrixCalibrationToolExecutionResponse:
        _ensure_maintain_studio_entitled()
        with _brain_request_scope(payload.brain_id):
            return McpMatrixCalibrationToolExecutionResponse(
                **_build_matrix_payload(
                    tool_name="matrix_calibration_preview",
                    max_nodes_considered=payload.max_nodes_considered,
                    max_position_updates=payload.max_position_updates,
                    include_recommendations=payload.include_recommendations,
                )
            )

    @router.post(
        "/memory/mcp/matrix-calibration-apply",
        response_model=McpMatrixCalibrationToolExecutionResponse,
        response_model_exclude_none=True,
    )
    @router.post(
        "/mcp/matrix-calibration-apply",
        response_model=McpMatrixCalibrationToolExecutionResponse,
        response_model_exclude_none=True,
    )
    def matrix_calibration_apply(
        payload: McpMatrixCalibrationApplyRequest,
    ) -> McpMatrixCalibrationToolExecutionResponse:
        _ensure_maintain_studio_entitled()
        with _brain_request_scope(payload.brain_id):
            preview = _build_matrix_payload(
                tool_name="matrix_calibration_apply",
                max_nodes_considered=payload.max_nodes_considered,
                max_position_updates=payload.max_position_updates,
                include_recommendations=payload.include_recommendations,
                store_preview=False,
            )
            plan = dict(preview.get("position_update_plan") or {})
            plan_signature = str(plan.get("plan_signature") or "").strip()
            blocked_reasons: list[str] = []
            if not payload.confirm_apply:
                blocked_reasons.append("confirm_apply_required")
            if not payload.rollback_consent:
                blocked_reasons.append("rollback_consent_required")
            if payload.preview_signature and payload.preview_signature != plan_signature:
                blocked_reasons.append("preview_signature_mismatch")
            if not list(plan.get("updates") or []):
                blocked_reasons.append("no_position_updates")
            if blocked_reasons:
                preview["status"] = "blocked"
                preview["apply_policy_guard"] = _apply_guard(
                    applied=False,
                    blocked_reasons=blocked_reasons,
                    plan_signature=plan_signature,
                )
                preview["actions"] = _matrix_actions(plan_signature=plan_signature, blocked_reasons=blocked_reasons)
                return McpMatrixCalibrationToolExecutionResponse(**preview)

            apply_result = apply_matrix_calibration_position_updates_with_revisions(
                list(plan.get("updates") or []),
                revision_bundle=dict(preview.get("matrix_delta") or {}).get("revision_bundle"),
                rollback_snapshot=_rollback_snapshot(plan),
                plan_signature=plan_signature,
            )
            preview["status"] = "applied"
            preview["mutation_surface"] = {
                "schema_version": "agvm.core_matrix_mutation_surface.v1",
                "applied": True,
                "apply_result": apply_result,
                "touched_surfaces": list(apply_result.get("touched_fields") or []),
                "untouched_surfaces": list(apply_result.get("untouched_surfaces") or []),
            }
            preview["apply_policy_guard"] = _apply_guard(
                applied=True,
                blocked_reasons=[],
                plan_signature=plan_signature,
                apply_result=apply_result,
            )
            store_maintenance_run(
                maintenance_id=str(preview.get("maintenance_id") or f"matrix::{uuid.uuid4()}"),
                mode="matrix_calibration",
                applied=True,
                preview_only=False,
                focus_node_id=None,
                report=preview,
            )
            return McpMatrixCalibrationToolExecutionResponse(**preview)

    return router


def _ensure_maintain_studio_entitled() -> None:
    ensure_local_module_entitled(MAINTAIN_MODULE_ID)


def _resolve_brain_record(brain_id: str | None = None) -> dict[str, Any]:
    try:
        return resolve_brain_scope(brain_id=str(brain_id or "").strip() or None)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class _BrainScope:
    def __init__(self, brain_id: str | None) -> None:
        self._brain = _resolve_brain_record(brain_id)
        self._scope = use_runtime_brain(self._brain)

    def __enter__(self) -> dict[str, Any]:
        self._scope.__enter__()
        return self._brain

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._scope.__exit__(exc_type, exc, tb)


def _brain_request_scope(brain_id: str | None) -> _BrainScope:
    return _BrainScope(brain_id)


def _build_matrix_payload(
    *,
    tool_name: str,
    max_nodes_considered: int,
    max_position_updates: int,
    include_recommendations: bool,
    store_preview: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    bootstrap_runtime_store()
    graph = fetch_graph_snapshot()
    before_report = build_brain_geometry_calibration_report(graph)
    position_plan = build_matrix_calibration_position_plan(
        graph,
        max_nodes=max_nodes_considered,
        max_updates=max_position_updates,
    )
    projected_graph = apply_matrix_calibration_position_plan_to_graph(graph, position_plan)
    projected_report = build_brain_geometry_calibration_report(projected_graph)
    revision_bundle = _revision_bundle(
        graph=graph,
        before_report=before_report,
        position_plan=position_plan,
        projected_report=projected_report,
    )
    recommendations = list(before_report.get("recommendations") or []) if include_recommendations else []
    maintenance_id = f"matrix-calibration::{uuid.uuid4()}"
    update_count = int(position_plan.get("update_count") or 0)
    payload = {
        "schema_version": "agvm.mcp_matrix_calibration_tool_output.v1",
        "brain_id": current_brain_id(),
        "tool_name": tool_name,
        "status": "ok",
        "maintenance_id": maintenance_id,
        "brain_geometry_calibration": before_report,
        "calibration_proposals": list(before_report.get("calibration_proposals") or []),
        "recommendations": recommendations,
        "matrix_change_policy": {
            "schema_version": "agvm.core_matrix_change_policy.v1",
            "preview_only": True,
            "hidden_mutation_allowed": False,
            "apply_requires_confirm_apply": True,
            "apply_requires_rollback_consent": True,
            "position_update_count": update_count,
        },
        "maintenance_truth_contract": {
            "schema_version": "agvm.core_matrix_truth_contract.v1",
            "non_mutating_preview": True,
            "applies_only_on_confirmed_apply_endpoint": True,
            "core_safe_implementation": True,
        },
        "memory_operation_lifecycle_contract": {
            "schema_version": "agvm.core_matrix_lifecycle_contract.v1",
            "operation": "matrix_calibration",
            "preview_signature": position_plan.get("plan_signature"),
            "preview_before_apply_required": True,
        },
        "maintenance_transaction": {
            "schema_version": "agvm.core_matrix_transaction.v1",
            "preview_is_non_mutating": True,
            "apply_is_atomic": True,
            "rollback_snapshot_required": True,
        },
        "matrix_delta": {
            "schema_version": "agvm.core_matrix_delta.v1",
            "update_count": update_count,
            "plan_signature": position_plan.get("plan_signature"),
            "revision_bundle": revision_bundle,
        },
        "position_update_plan": position_plan,
        "projected_after": projected_report,
        "apply_policy_guard": _apply_guard(
            applied=False,
            blocked_reasons=["preview_only"],
            plan_signature=str(position_plan.get("plan_signature") or ""),
        ),
        "rollback_snapshot": _rollback_snapshot(position_plan),
        "before_after_audit": {
            "schema_version": "agvm.core_matrix_before_after_audit.v1",
            "before": _score_summary(before_report),
            "projected_after": _score_summary(projected_report),
            "update_count": update_count,
        },
        "mutation_surface": {
            "schema_version": "agvm.core_matrix_mutation_surface.v1",
            "applied": False,
            "hidden_mutation_allowed": False,
        },
        "safety_contract": {
            "schema_version": "agvm.core_matrix_safety_contract.v1",
            "non_mutating": True,
            "hidden_mutation_allowed": False,
            "matrix_updates_require_preview_apply_rollback": True,
        },
        "latency_profile": {
            "schema_version": "agvm.core_matrix_latency_profile.v1",
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        },
        "actions": _matrix_actions(plan_signature=str(position_plan.get("plan_signature") or "")),
        "budget": {
            "schema_version": "agvm.core_matrix_budget.v1",
            "local_core_credits_required": 0,
            "mutation_allowed": False,
        },
        "completeness": {
            "schema_version": "agvm.core_matrix_completeness.v1",
            "graph_node_count": len(list(graph.get("nodes") or [])),
            "update_count": update_count,
        },
    }
    if store_preview:
        store_maintenance_run(
            maintenance_id=maintenance_id,
            mode="matrix_calibration",
            applied=False,
            preview_only=True,
            focus_node_id=None,
            report=payload,
        )
    return payload


def _revision_bundle(
    *,
    graph: dict[str, Any],
    before_report: dict[str, Any],
    position_plan: dict[str, Any],
    projected_report: dict[str, Any],
) -> dict[str, Any]:
    brain_id = current_brain_id() or "default"
    active_matrix = fetch_active_matrix_revision(brain_id=brain_id) or {}
    active_topology = fetch_active_topology_field_revision(brain_id=brain_id) or {}
    return build_matrix_calibration_revision_candidates(
        graph=graph,
        before_report=before_report,
        position_plan=position_plan,
        projected_report=projected_report,
        brain_id=brain_id,
        parent_matrix_revision_id=active_matrix.get("matrix_revision_id"),
        parent_topology_revision_id=active_topology.get("topology_revision_id"),
        source_event_ids=[],
    )


def _score_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "agvm.core_matrix_score_summary.v1",
        "node_count": report.get("node_count"),
        "overall_score": report.get("overall_score"),
        "benchmarks": report.get("benchmarks"),
        "summary": report.get("summary"),
    }


def _rollback_snapshot(position_plan: dict[str, Any]) -> dict[str, Any]:
    updates = list(position_plan.get("updates") or [])
    return {
        "schema_version": "agvm.core_matrix_rollback_snapshot.v1",
        "snapshot_id": f"matrix-rollback::{position_plan.get('plan_signature') or 'empty'}",
        "created_at": utc_timestamp(),
        "plan_signature": position_plan.get("plan_signature"),
        "node_position_sample": [
            {
                "node_id": item.get("node_id"),
                "before": item.get("from_position"),
                "after": item.get("to_position"),
            }
            for item in updates[:50]
            if isinstance(item, dict)
        ],
    }


def _apply_guard(
    *,
    applied: bool,
    blocked_reasons: list[str],
    plan_signature: str,
    apply_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "agvm.core_matrix_apply_policy_guard.v1",
        "guard_passed": not blocked_reasons,
        "applied": applied,
        "blocked_reasons": blocked_reasons,
        "preview_signature": plan_signature,
        "apply_result": apply_result or {},
    }


def _matrix_actions(
    *,
    plan_signature: str,
    blocked_reasons: list[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "action": "matrix_calibration_apply",
            "endpoint_hint": "/mcp/matrix-calibration-apply",
            "requires_confirm_apply": True,
            "requires_rollback_consent": True,
            "preview_signature": plan_signature,
            "blocked_reasons": blocked_reasons or ["preview_only"],
        }
    ]
