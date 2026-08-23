# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Public route stubs for Cloud-backed Geometry Calibration tools."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from public_cloud_action_contract import cloud_backed_tool_response


PUBLIC_CLOUD_ACTION_STUB = True
MAINTAIN_MODULE_ID = "agvm_maintain_studio"


def create_core_mcp_matrix_router() -> APIRouter:
    """Expose stable routes while keeping paid execution outside Public Core."""

    router = APIRouter()
    operations = (
        "geometry_calibration_preview",
        "geometry_calibration_apply",
        "geometry_calibration_rollback",
        "matrix_calibration_preview",
        "matrix_calibration_apply",
    )
    for tool_name in operations:
        endpoint = _cloud_endpoint(tool_name)
        route_name = tool_name.replace("_", "-")
        router.add_api_route(
            f"/mcp/{route_name}",
            endpoint,
            methods=["POST"],
            name=f"public_cloud_{tool_name}",
        )
        router.add_api_route(
            f"/memory/mcp/{route_name}",
            endpoint,
            methods=["POST"],
            name=f"public_memory_cloud_{tool_name}",
        )
    return router


def _cloud_endpoint(tool_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        _ = payload
        return cloud_backed_tool_response(
            tool_name,
            capability="geometry_calibration",
            required_module_id=MAINTAIN_MODULE_ID,
        )

    endpoint.__name__ = f"public_cloud_{tool_name}"
    return endpoint
