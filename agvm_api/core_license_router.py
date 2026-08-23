# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from local_entitlements import (
    DEFAULT_PRO_MODULE_IDS,
    KNOWN_MODULE_IDS,
    LocalEntitlementError,
    activate_local_license,
    all_local_module_entitlements,
    local_license_status,
    module_entitlement_status,
)


class LocalLicenseActivateRequest(BaseModel):
    license_key: str | None = Field(default=None, max_length=4096)
    lease_token: str | None = Field(default=None, max_length=20000)
    module_ids: list[str] | None = None
    ttl_hours: int = Field(default=24 * 14, ge=1, le=24 * 90)


def create_core_license_router() -> APIRouter:
    router = APIRouter(tags=["agvm-core-license"])

    @router.get("/modules/local-license")
    def local_license_status_endpoint() -> dict[str, Any]:
        try:
            return local_license_status()
        except LocalEntitlementError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @router.post("/modules/local-license/activate")
    def activate_local_license_endpoint(payload: LocalLicenseActivateRequest) -> dict[str, Any]:
        try:
            return activate_local_license(
                license_key=payload.license_key,
                lease_token=payload.lease_token,
                module_ids=payload.module_ids or list(DEFAULT_PRO_MODULE_IDS),
                ttl_hours=payload.ttl_hours,
            )
        except LocalEntitlementError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    @router.get("/modules/local-license/entitlements")
    def local_module_entitlements_endpoint() -> dict[str, Any]:
        try:
            entitlements = all_local_module_entitlements(module_ids=sorted(KNOWN_MODULE_IDS))
        except LocalEntitlementError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc
        return {
            "schema_version": "agvm.local_module_entitlements.v1",
            "modules": entitlements,
        }

    @router.get("/modules/local-license/modules/{module_id}")
    def local_module_entitlement_endpoint(module_id: str) -> dict[str, Any]:
        try:
            return module_entitlement_status(module_id)
        except LocalEntitlementError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc

    return router
