# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException

from local_entitlements import LocalEntitlementError, module_entitlement_status

try:  # pragma: no cover - preferred in extracted core images.
    from agvm_sdk.module_manifest import AGVM_MODULE_MANIFEST_SCHEMA_VERSION, normalize_module_manifest
except ImportError:  # pragma: no cover - monolith compatibility path.
    from module_manifest_contracts import AGVM_MODULE_MANIFEST_SCHEMA_VERSION, normalize_module_manifest


GROW_MODULE_ID = "agvm_grow_studio"
MAINTAIN_MODULE_ID = "agvm_maintain_studio"
CLONE_MODULE_ID = "agvm_clone_app"


@dataclass(frozen=True)
class LocalModuleManifestDefinition:
    module_id: str
    module_version: str
    api_base_path: str
    route_id: str
    label: str
    path: str
    nav_group: str
    required_capability: str
    description: str
    capabilities: tuple[str, ...]
    uses_core_tools: tuple[str, ...]
    fallback_message: str


GROW_MANIFEST = LocalModuleManifestDefinition(
    module_id=GROW_MODULE_ID,
    module_version="0.1.0",
    api_base_path="/grow-studio",
    route_id="grow_studio",
    label="Grow",
    path="/grow",
    nav_group="operate",
    required_capability="grow_studio",
    description="Guided source intake and preview-first memory growth.",
    capabilities=("grow_studio", "source_intake", "preview_first_apply"),
    uses_core_tools=(
        "grow_source_preview",
        "grow_source_status",
        "grow_source_apply",
        "grow_preview",
        "grow_guided",
        "grow_apply",
        "grow_status",
        "write_memory_preview",
        "write_memory_commit",
    ),
    fallback_message="Grow Studio is locked. Sign in or activate a Pro local lease before opening this module.",
)

MAINTAIN_MANIFEST = LocalModuleManifestDefinition(
    module_id=MAINTAIN_MODULE_ID,
    module_version="0.1.0",
    api_base_path="/maintain-studio",
    route_id="maintain_studio",
    label="Maintain",
    path="/maintain",
    nav_group="operate",
    required_capability="maintain_studio",
    description="Preview-first Sleep, Evolve and Matrix maintenance.",
    capabilities=("maintain_studio", "sleep_preview", "evolve_preview", "matrix_calibration", "preview_first_apply"),
    uses_core_tools=(
        "matrix_calibration_preview",
        "matrix_calibration_apply",
        "sleep_preview",
        "sleep_apply",
        "evolve_preview",
        "evolve_apply",
        "list_open_questions",
        "list_hypotheses",
        "list_contradictions",
        "list_memory_os_processes",
    ),
    fallback_message="Maintain Studio is locked. Sign in or activate a Pro local lease before opening this module.",
)


def create_local_module_manifest_router() -> APIRouter:
    router = APIRouter(tags=["agvm-local-module-manifests"])

    @router.get("/grow-studio/module-manifest")
    def grow_studio_module_manifest() -> dict[str, Any]:
        return build_local_module_manifest(GROW_MANIFEST)

    @router.get("/maintain-studio/module-manifest")
    def maintain_studio_module_manifest() -> dict[str, Any]:
        return build_local_module_manifest(MAINTAIN_MANIFEST)

    return router


def build_local_module_manifest(definition: LocalModuleManifestDefinition) -> dict[str, Any]:
    status = _module_status(definition.module_id)
    granted = bool(status.get("granted"))
    license_state = _manifest_license_state(str(status.get("license_state") or "missing"))
    backend_status = "healthy"
    payload = {
        "schema_version": AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
        "module_id": definition.module_id,
        "module_version": definition.module_version,
        "edition": "paid",
        "backend_status": backend_status,
        "license_state": license_state,
        "available": granted and backend_status == "healthy",
        "api_base_path": definition.api_base_path,
        "ui": {
            "kind": "local_route" if granted and backend_status == "healthy" else "none",
            "entry_url": None,
            "integrity": None,
            "mounts": [
                {
                    "route_id": definition.route_id,
                    "label": definition.label,
                    "path": definition.path,
                    "nav_group": definition.nav_group,
                    "required_capability": definition.required_capability,
                    "description": definition.description,
                }
            ]
            if granted and backend_status == "healthy"
            else [],
        },
        "capabilities": {capability: granted and backend_status == "healthy" for capability in definition.capabilities},
        "mcp_tools": {
            "adds_tools": [],
            "uses_core_tools": list(definition.uses_core_tools),
        },
        "license": {
            "plan_required": "pro",
            "lease_expires_at": status.get("lease_expires_at"),
        },
        "safe_fallback_message": definition.fallback_message,
        "diagnostics": {
            "runtime": "local_core_manifest_projection",
            "entitlement_reason": status.get("reason"),
            "entitlement_module_state": status.get("module_state"),
            "local_license_present": bool(status.get("lease_present")),
            "local_module_token_present": bool(status.get("token_present")),
            "plan": status.get("plan"),
            "module_grant_present": bool(status.get("module_grant")),
            "module_grant": status.get("module_grant"),
            "dev_fixture_allowed": status.get("dev_fixture_allowed"),
            "source": "local_license_supervisor",
        },
    }
    return normalize_module_manifest(payload).as_dict()


def ensure_local_module_entitled(module_id: str) -> None:
    status = _module_status(module_id)
    if bool(status.get("granted")):
        return
    raise HTTPException(
        status_code=402,
        detail={
            "code": "module_not_available",
            "module_id": module_id,
            "module_state": status.get("module_state"),
            "license_state": status.get("license_state"),
            "reason": status.get("reason"),
            "plan": status.get("plan"),
            "lease_present": bool(status.get("lease_present")),
            "message": "This AGVM Pro module requires a valid local lease from the Detwin platform.",
        },
    )


def _module_status(module_id: str) -> dict[str, Any]:
    try:
        status = module_entitlement_status(module_id)
    except LocalEntitlementError as exc:
        return {
            "schema_version": "agvm.local_module_entitlement_status.v1",
            "module_id": module_id,
            "granted": False,
            "module_state": "unavailable",
            "license_state": "invalid",
            "reason": exc.code,
            "lease_present": False,
            "token_present": False,
        }
    return dict(status)


def _manifest_license_state(value: str) -> str:
    if value in {"installed", "missing", "expired", "invalid"}:
        return value
    return "invalid"
