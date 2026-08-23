# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .contracts import OPERATIONS
from .service import BrainBootstrapV1Service, BootstrapV1Error
from .store import BootstrapStoreError


def create_brain_bootstrap_v1_router(service: BrainBootstrapV1Service | None = None) -> APIRouter:
    router = APIRouter()
    runtime = service or BrainBootstrapV1Service()

    def execute(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return runtime.execute(operation, payload)
        except (BootstrapV1Error, BootstrapStoreError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    for operation in OPERATIONS:
        endpoint = _endpoint(operation, execute)
        route_operation = operation.replace("_", "-")
        router.add_api_route(
            f"/mcp/brain-bootstrap-{route_operation}",
            endpoint,
            methods=["POST"],
            name=f"mcp_brain_bootstrap_v1_{operation}",
        )
        router.add_api_route(
            f"/memory/mcp/brain-bootstrap-{route_operation}",
            endpoint,
            methods=["POST"],
            name=f"memory_mcp_brain_bootstrap_v1_{operation}",
        )
    return router


def _endpoint(operation: str, execute: Any) -> Any:
    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        return execute(operation, payload)

    handler.__name__ = f"brain_bootstrap_v1_{operation}"
    return handler
