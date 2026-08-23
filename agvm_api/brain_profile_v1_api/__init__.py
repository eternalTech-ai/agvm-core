# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Public route stubs for Cloud-backed Brain Profile operations."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from public_cloud_action_contract import cloud_backed_tool_response


PUBLIC_CLOUD_ACTION_STUB = True
PROFILE_MODULE_ID = "agvm_maintain_studio"


def create_brain_profile_v1_router(service: Any | None = None) -> APIRouter:
    """Keep public contracts callable without shipping profile execution code."""

    _ = service
    router = APIRouter()
    for operation in ("preview", "apply", "rollback"):
        tool_name = f"brain_profile_{operation}"
        endpoint = _cloud_endpoint(tool_name, operation)
        router.add_api_route(
            f"/mcp/brain-profile-{operation}",
            endpoint,
            methods=["POST"],
            name=f"public_cloud_{tool_name}",
        )
        router.add_api_route(
            f"/memory/mcp/brain-profile-{operation}",
            endpoint,
            methods=["POST"],
            name=f"public_memory_cloud_{tool_name}",
        )
    return router


def _cloud_endpoint(tool_name: str, capability: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        _ = payload
        return cloud_backed_tool_response(
            tool_name,
            capability=f"brain_profile_{capability}",
            required_module_id=PROFILE_MODULE_ID,
        )

    endpoint.__name__ = f"public_cloud_{tool_name}"
    return endpoint
