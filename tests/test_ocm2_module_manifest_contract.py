from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
CLONE_APP_BACKEND_DIR = ROOT / "apps" / "agvm_clone_app" / "backend"
for path in (API_DIR, CLONE_APP_BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from agvm_clone_app.api import build_absent_clone_app_module_manifest, build_clone_app_module_manifest  # noqa: E402
from module_manifest_contracts import (  # noqa: E402
    AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
    LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION,
    ModuleManifestValidationError,
    build_absent_module_manifest,
    derive_module_state,
    normalize_module_manifest,
)
from module_registry import StaticModuleRegistry, build_static_module_registry  # noqa: E402


def test_generic_paid_healthy_manifest_normalizes_to_ready_state() -> None:
    manifest = normalize_module_manifest(_generic_manifest())

    assert manifest.schema_version == AGVM_MODULE_MANIFEST_SCHEMA_VERSION
    assert manifest.module_id == "agvm_clone_app"
    assert manifest.module_state == "healthy"
    assert manifest.available is True
    assert manifest.ui.kind == "remote_bundle"
    assert manifest.license.plan_required == "pro"
    assert manifest.as_dict()["ui"]["mounts"][0]["route_id"] == "clone_chat"


def test_unlicensed_incompatible_and_absent_states_are_safe() -> None:
    unlicensed = _generic_manifest(license_state="expired", available=False)
    incompatible = _generic_manifest(backend_status="incompatible", available=False)
    absent = build_absent_module_manifest(module_id="agvm_grow_studio", api_base_path="/grow")

    assert normalize_module_manifest(unlicensed).module_state == "unlicensed"
    assert normalize_module_manifest(unlicensed).ui.mounts == []
    assert normalize_module_manifest(incompatible).module_state == "incompatible"
    assert absent.module_state == "absent"
    assert absent.available is False


def test_manifest_validation_rejects_inconsistent_available_flag() -> None:
    with pytest.raises(ModuleManifestValidationError, match="available_must_match_module_state"):
        normalize_module_manifest(_generic_manifest(license_state="expired", available=True))


def test_manifest_validation_rejects_mounts_without_enabled_capability() -> None:
    payload = _generic_manifest(capabilities={"clone_chat": False}, available=True)

    with pytest.raises(ModuleManifestValidationError, match="ui_mount_requires_enabled_capability:clone_chat"):
        normalize_module_manifest(payload)


def test_static_registry_lists_states_and_rejects_duplicates() -> None:
    registry = build_static_module_registry(
        [
            _generic_manifest(module_id="agvm_clone_app"),
            build_absent_module_manifest(module_id="agvm_grow_studio", api_base_path="/grow"),
            _generic_manifest(module_id="agvm_maintain_studio", backend_status="incompatible", available=False),
        ]
    )
    summary = registry.summary()

    assert [manifest.module_id for manifest in registry.list_manifests()] == [
        "agvm_clone_app",
        "agvm_grow_studio",
        "agvm_maintain_studio",
    ]
    assert registry.require_manifest("agvm_clone_app").module_state == "healthy"
    assert summary.by_state["healthy"] == 1
    assert summary.by_state["absent"] == 1
    assert summary.by_state["incompatible"] == 1
    assert summary.healthy_module_ids == ["agvm_clone_app"]
    assert set(summary.unavailable_module_ids) == {"agvm_grow_studio", "agvm_maintain_studio"}

    with pytest.raises(ModuleManifestValidationError, match="duplicate_module_manifest:agvm_clone_app"):
        registry.register(_generic_manifest(module_id="agvm_clone_app"))


def test_legacy_clone_app_manifest_adapts_to_generic_contract() -> None:
    legacy_payload = build_clone_app_module_manifest().as_dict()
    manifest = normalize_module_manifest(legacy_payload)

    assert legacy_payload["schema_version"] == LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION
    assert manifest.schema_version == AGVM_MODULE_MANIFEST_SCHEMA_VERSION
    assert manifest.source_schema_version == LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION
    assert manifest.module_id == "agvm_clone_app"
    assert manifest.module_state == "healthy"
    assert manifest.available is True
    assert manifest.module_version == legacy_payload["version"]
    assert manifest.ui.kind == "local_route"
    assert {mount.route_id for mount in manifest.ui.mounts} == {
        "clone_chat",
        "teach_chat",
        "sources_memory",
        "clone_settings",
        "developer_proof",
    }
    assert manifest.capabilities["clone_chat"] is True
    assert manifest.mcp_tools.uses_core_tools == ["retrieve_context", "write_memory_preview"]
    assert manifest.license.plan_required == "pro"
    assert manifest.diagnostics["legacy_safety_flags"] == {
        "apply_locked": True,
        "mutates_agvm_memory": False,
        "mutation_allowed": False,
    }


def test_absent_legacy_clone_app_manifest_adapts_to_absent_state() -> None:
    legacy_payload = build_absent_clone_app_module_manifest(reason="not_installed").as_dict()
    manifest = normalize_module_manifest(legacy_payload)

    assert manifest.module_state == "absent"
    assert manifest.available is False
    assert manifest.ui.kind == "none"
    assert manifest.ui.mounts == []
    assert all(value is False for value in manifest.capabilities.values())


def test_state_derivation_is_explicit_for_all_host_cases() -> None:
    assert derive_module_state(edition="absent", backend_status="missing", license_state="missing") == "absent"
    assert derive_module_state(edition="paid", backend_status="healthy", license_state="missing") == "unlicensed"
    assert derive_module_state(edition="paid", backend_status="incompatible", license_state="installed") == "incompatible"
    assert derive_module_state(edition="paid", backend_status="degraded", license_state="installed") == "degraded"
    assert derive_module_state(edition="paid", backend_status="healthy", license_state="installed") == "healthy"


def _generic_manifest(
    *,
    module_id: str = "agvm_clone_app",
    backend_status: str = "healthy",
    license_state: str = "installed",
    capabilities: dict[str, bool] | None = None,
    available: bool = True,
) -> dict[str, object]:
    enabled_capabilities = {"clone_chat": True, **dict(capabilities or {})}
    mounts = (
        [
            {
                "route_id": "clone_chat",
                "label": "Clone Chat",
                "path": "/clone/chat",
                "nav_group": "modules",
                "required_capability": "clone_chat",
                "description": "Natural first-person conversation with the active AGVM brain.",
            }
        ]
        if available
        else []
    )
    return {
        "schema_version": AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
        "module_id": module_id,
        "module_version": "0.1.0",
        "edition": "paid",
        "backend_status": backend_status,
        "license_state": license_state,
        "available": available,
        "api_base_path": "/clone-app",
        "ui": {
            "kind": "remote_bundle" if available else "none",
            "entry_url": "http://agvm_clone_app_ui:3030/assets/module-entry.js" if available else None,
            "integrity": "sha256-test",
            "mounts": mounts,
        },
        "capabilities": enabled_capabilities if available else {key: False for key in enabled_capabilities},
        "mcp_tools": {
            "adds_tools": [],
            "uses_core_tools": ["retrieve_context", "write_memory_preview"],
        },
        "license": {
            "plan_required": "pro",
            "lease_expires_at": "2026-06-20T00:00:00Z",
        },
        "safe_fallback_message": "Module not active.",
        "diagnostics": {},
    }
