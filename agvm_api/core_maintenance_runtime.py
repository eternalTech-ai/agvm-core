# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Public/Local Core fail-closed boundary for Cloud-only Maintain operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter

from local_module_manifest_router import MAINTAIN_MODULE_ID, ensure_local_module_entitled


PUBLIC_CLOUD_ACTION_STUB = True


@dataclass(frozen=True)
class CoreMaintenanceCloudHandoffRuntime:
    def preview(
        self,
        *,
        graph: dict[str, Any],
        mode: str,
        focus_node_id: str | None,
        max_nodes_considered: int,
    ) -> dict[str, Any]:
        del graph, focus_node_id, max_nodes_considered
        return _cloud_handoff_maintenance_report(mode=mode, operation=f"{mode}_preview")

    def apply(
        self,
        *,
        graph: dict[str, Any],
        mode: str,
        focus_node_id: str | None,
        max_nodes_considered: int,
        expected_preview_signature: str | None = None,
        selected_proposal_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        del graph, focus_node_id, max_nodes_considered, expected_preview_signature, selected_proposal_ids
        return _cloud_handoff_maintenance_report(mode=mode, operation=f"{mode}_apply")

    def rollback(self, *, mode: str, preview_signature: str) -> dict[str, Any]:
        return {
            "rolled_back": False,
            "mode": mode,
            "preview_signature": preview_signature,
            "error": {
                "code": "detwin_cloud_execution_required",
                "action_contract": _cloud_maintenance_action(f"{mode}_rollback"),
            },
        }


def create_core_maintenance_cloud_handoff_router() -> APIRouter:
    router = APIRouter(tags=["agvm-local-maintain-cloud-handoff"])
    operations = (
        "brain_profile_preview",
        "brain_profile_apply",
        "brain_profile_rollback",
        "geometry_calibration_preview",
        "geometry_calibration_apply",
        "geometry_calibration_rollback",
        "matrix_calibration_preview",
        "matrix_calibration_apply",
        "matrix_calibration_rollback",
        "calibrate_brain_preview",
        "calibrate_brain_apply",
        "calibrate_brain_rollback",
    )
    for tool_name in operations:
        endpoint = _cloud_handoff_endpoint(tool_name)
        route_name = tool_name.replace("_", "-")
        router.add_api_route(f"/mcp/{route_name}", endpoint, methods=["POST"])
        router.add_api_route(f"/memory/mcp/{route_name}", endpoint, methods=["POST"])
        if tool_name.startswith("calibrate_brain_"):
            operation = tool_name.removeprefix("calibrate_brain_")
            router.add_api_route(f"/v1/brain/calibrate-brain-{operation}", endpoint, methods=["POST"])
    return router


def _cloud_handoff_endpoint(tool_name: str) -> Callable[[dict[str, Any] | None], dict[str, Any]]:
    def endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        del payload
        ensure_local_module_entitled(MAINTAIN_MODULE_ID)
        return _cloud_maintenance_response(tool_name)

    endpoint.__name__ = f"local_cloud_handoff_{tool_name}"
    return endpoint


def _cloud_maintenance_action(tool_name: str) -> dict[str, Any]:
    return {
        "schema_version": "agvm.local_mcp_paid_tool_action.v2",
        "action": "use_detwin_cloud_for_advanced_tool",
        "tool_name": tool_name,
        "capability": "maintain",
        "required_module_id": MAINTAIN_MODULE_ID,
        "execution_surface": "hosted_mcp",
        "requires_account": True,
        "requires_entitlement": True,
        "requires_credits": True,
        "dynamic_usage_settlement": True,
        "credential_environment_variable": "AGVM_HOSTED_MCP_API_KEY",
        "cloud_workspace_url": "https://cloud.detwin.ai/",
        "hosted_mcp_key_setup_url": "https://app.detwin.ai/account/mcp",
        "local_execution_available": False,
        "local_graph_mutation": "forbidden",
    }


def _cloud_maintenance_response(tool_name: str) -> dict[str, Any]:
    action_contract = _cloud_maintenance_action(tool_name)
    return {
        "schema_version": "agvm.local_core.cloud_maintain_handoff.v2",
        "tool_name": tool_name,
        "status": "blocked",
        "reason": "detwin_cloud_execution_required",
        "recovery": "Connect this runtime to Detwin Cloud and execute the reviewed operation with Hosted MCP.",
        "action_contract": action_contract,
        "mutation_surface": {
            "runtime": "local_core",
            "applied": False,
            "graph_mutation": "none",
            "local_execution_available": False,
        },
        "data": {"action_contract": action_contract},
    }


def _cloud_handoff_maintenance_report(*, mode: str, operation: str) -> dict[str, Any]:
    action_contract = _cloud_maintenance_action(operation)
    reason = "detwin_cloud_execution_required"
    return {
        "schema_version": "agvm.local_core.maintenance_handoff_report.v2",
        "mode": mode,
        "applied": False,
        "maintenance_proposals": [],
        "maintenance_store_error": {"code": reason, "action_contract": action_contract},
        "apply_policy_guard": {
            "applied": False,
            "guard_passed": False,
            "blocked": True,
            "blocked_reason": reason,
            "blocked_reasons": [reason],
            "graph_mutation": "none",
        },
        "maintenance_contract": {
            "preview_non_mutating": True,
            "hidden_mutation_allowed": False,
            "local_execution_available": False,
            "execution_surface": "hosted_mcp",
            "entitlement_bypass_allowed": False,
            "action_contract": action_contract,
        },
    }
