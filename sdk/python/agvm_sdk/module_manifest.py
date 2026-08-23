# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: Apache-2.0

"""Generic AGVM module manifest contract.

This module is intentionally dependency-free. Public core, private modules and
the future platform can import the same validation rules without importing paid
module runtime code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal


JsonObject = dict[str, Any]

AGVM_MODULE_MANIFEST_SCHEMA_VERSION = "agvm.module_manifest.v1"
LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION = "agvm.clone.module_manifest.v1"

ModuleEdition = Literal["absent", "free_stub", "paid"]
ModuleBackendStatus = Literal["healthy", "degraded", "missing", "incompatible"]
ModuleLicenseState = Literal["installed", "missing", "expired", "invalid", "not_required"]
ModuleState = Literal["absent", "unlicensed", "incompatible", "degraded", "healthy"]
ModuleUiKind = Literal["none", "local_route", "remote_bundle", "hosted_route"]

MODULE_EDITIONS: set[str] = {"absent", "free_stub", "paid"}
MODULE_BACKEND_STATUSES: set[str] = {"healthy", "degraded", "missing", "incompatible"}
MODULE_LICENSE_STATES: set[str] = {"installed", "missing", "expired", "invalid", "not_required"}
MODULE_STATES: set[str] = {"absent", "unlicensed", "incompatible", "degraded", "healthy"}
MODULE_UI_KINDS: set[str] = {"none", "local_route", "remote_bundle", "hosted_route"}


class ModuleManifestValidationError(ValueError):
    """Raised when a module manifest is malformed or internally inconsistent."""


@dataclass(frozen=True)
class AgvmModuleUiMount:
    route_id: str
    label: str
    path: str
    nav_group: str
    required_capability: str
    description: str = ""

    def __post_init__(self) -> None:
        _require_text("route_id", self.route_id)
        _require_text("label", self.label)
        _require_text("path", self.path)
        _require_text("nav_group", self.nav_group)
        _require_text("required_capability", self.required_capability)
        if not self.path.startswith("/"):
            raise ModuleManifestValidationError("ui_mount_path_must_start_with_slash")

    def as_dict(self) -> JsonObject:
        return {
            "route_id": self.route_id,
            "label": self.label,
            "path": self.path,
            "nav_group": self.nav_group,
            "required_capability": self.required_capability,
            "description": self.description,
        }


@dataclass(frozen=True)
class AgvmModuleUiBundle:
    kind: ModuleUiKind
    mounts: list[AgvmModuleUiMount] = field(default_factory=list)
    entry_url: str | None = None
    integrity: str | None = None

    def __post_init__(self) -> None:
        _ensure_allowed("ui.kind", self.kind, MODULE_UI_KINDS)
        if self.kind == "remote_bundle":
            _require_text("ui.entry_url", self.entry_url or "")
        if self.kind == "none" and (self.entry_url or self.mounts):
            raise ModuleManifestValidationError("ui_none_must_not_define_entry_or_mounts")

    def as_dict(self) -> JsonObject:
        return {
            "kind": self.kind,
            "entry_url": self.entry_url,
            "integrity": self.integrity,
            "mounts": [mount.as_dict() for mount in self.mounts],
        }


@dataclass(frozen=True)
class AgvmModuleMcpTools:
    adds_tools: list[str] = field(default_factory=list)
    uses_core_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _ensure_text_sequence("mcp_tools.adds_tools", self.adds_tools)
        _ensure_text_sequence("mcp_tools.uses_core_tools", self.uses_core_tools)

    def as_dict(self) -> JsonObject:
        return {
            "adds_tools": list(self.adds_tools),
            "uses_core_tools": list(self.uses_core_tools),
        }


@dataclass(frozen=True)
class AgvmModuleLicense:
    plan_required: str | None = None
    lease_expires_at: str | None = None

    def as_dict(self) -> JsonObject:
        return {
            "plan_required": self.plan_required,
            "lease_expires_at": self.lease_expires_at,
        }


@dataclass(frozen=True)
class AgvmModuleManifest:
    module_id: str
    module_version: str
    edition: ModuleEdition
    backend_status: ModuleBackendStatus
    license_state: ModuleLicenseState
    available: bool
    api_base_path: str
    ui: AgvmModuleUiBundle
    capabilities: dict[str, bool]
    mcp_tools: AgvmModuleMcpTools
    license: AgvmModuleLicense
    safe_fallback_message: str
    diagnostics: JsonObject = field(default_factory=dict)
    module_state: ModuleState = "absent"
    schema_version: str = AGVM_MODULE_MANIFEST_SCHEMA_VERSION
    source_schema_version: str | None = None

    def __post_init__(self) -> None:
        _require_text("module_id", self.module_id)
        _require_text("module_version", self.module_version)
        _require_text("api_base_path", self.api_base_path)
        _require_text("safe_fallback_message", self.safe_fallback_message)
        if self.schema_version != AGVM_MODULE_MANIFEST_SCHEMA_VERSION:
            raise ModuleManifestValidationError(f"unsupported_module_manifest_schema:{self.schema_version}")
        if not self.api_base_path.startswith("/"):
            raise ModuleManifestValidationError("api_base_path_must_start_with_slash")
        _ensure_allowed("edition", self.edition, MODULE_EDITIONS)
        _ensure_allowed("backend_status", self.backend_status, MODULE_BACKEND_STATUSES)
        _ensure_allowed("license_state", self.license_state, MODULE_LICENSE_STATES)
        _ensure_allowed("module_state", self.module_state, MODULE_STATES)
        expected_state = derive_module_state(
            edition=self.edition,
            backend_status=self.backend_status,
            license_state=self.license_state,
        )
        if self.module_state != expected_state:
            raise ModuleManifestValidationError(f"module_state_mismatch:{self.module_state}!={expected_state}")
        expected_available = expected_state == "healthy"
        if self.available != expected_available:
            raise ModuleManifestValidationError(f"available_must_match_module_state:{expected_state}")
        if self.edition == "paid" and self.license.plan_required is None:
            raise ModuleManifestValidationError("paid_module_requires_license_plan")
        if self.available and not self.capabilities and not self.ui.mounts and not self.mcp_tools.adds_tools:
            raise ModuleManifestValidationError("healthy_module_requires_capability_mount_or_tool")
        for mount in self.ui.mounts:
            if not self.capabilities.get(mount.required_capability, False):
                raise ModuleManifestValidationError(f"ui_mount_requires_enabled_capability:{mount.required_capability}")

    def as_dict(self) -> JsonObject:
        payload: JsonObject = {
            "schema_version": self.schema_version,
            "module_id": self.module_id,
            "module_version": self.module_version,
            "module_state": self.module_state,
            "edition": self.edition,
            "backend_status": self.backend_status,
            "license_state": self.license_state,
            "available": self.available,
            "api_base_path": self.api_base_path,
            "ui": self.ui.as_dict(),
            "capabilities": dict(self.capabilities),
            "mcp_tools": self.mcp_tools.as_dict(),
            "license": self.license.as_dict(),
            "safe_fallback_message": self.safe_fallback_message,
            "diagnostics": dict(self.diagnostics),
        }
        if self.source_schema_version:
            payload["source_schema_version"] = self.source_schema_version
        return payload


def normalize_module_manifest(payload: Mapping[str, Any] | AgvmModuleManifest) -> AgvmModuleManifest:
    """Validate and normalize a module manifest into the generic contract."""

    if isinstance(payload, AgvmModuleManifest):
        return payload
    schema_version = _text(payload.get("schema_version"))
    if schema_version == AGVM_MODULE_MANIFEST_SCHEMA_VERSION:
        return _normalize_generic_manifest(payload)
    if schema_version == LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION:
        return _normalize_legacy_clone_app_manifest(payload)
    raise ModuleManifestValidationError(f"unsupported_module_manifest_schema:{schema_version or '<missing>'}")


def build_absent_module_manifest(
    *,
    module_id: str,
    api_base_path: str,
    module_version: str = "0.0.0",
    safe_fallback_message: str | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> AgvmModuleManifest:
    return AgvmModuleManifest(
        module_id=_clean_required_text("module_id", module_id),
        module_version=_clean_required_text("module_version", module_version),
        edition="absent",
        backend_status="missing",
        license_state="missing",
        available=False,
        api_base_path=_normalize_api_base_path(api_base_path),
        ui=AgvmModuleUiBundle(kind="none"),
        capabilities={},
        mcp_tools=AgvmModuleMcpTools(),
        license=AgvmModuleLicense(),
        safe_fallback_message=safe_fallback_message or f"{module_id} module is not installed.",
        diagnostics=dict(diagnostics or {}),
        module_state="absent",
    )


def derive_module_state(
    *,
    edition: ModuleEdition | str,
    backend_status: ModuleBackendStatus | str,
    license_state: ModuleLicenseState | str,
) -> ModuleState:
    clean_edition = _validate_choice("edition", edition, MODULE_EDITIONS)
    clean_backend_status = _validate_choice("backend_status", backend_status, MODULE_BACKEND_STATUSES)
    clean_license_state = _validate_choice("license_state", license_state, MODULE_LICENSE_STATES)
    if clean_edition == "absent" or clean_backend_status == "missing":
        return "absent"
    if clean_backend_status == "incompatible":
        return "incompatible"
    if clean_edition == "paid" and clean_license_state != "installed":
        return "unlicensed"
    if clean_license_state in {"expired", "invalid"}:
        return "unlicensed"
    if clean_backend_status == "degraded":
        return "degraded"
    return "healthy"


def _normalize_generic_manifest(payload: Mapping[str, Any]) -> AgvmModuleManifest:
    edition = _validate_choice("edition", payload.get("edition"), MODULE_EDITIONS)
    backend_status = _validate_choice("backend_status", payload.get("backend_status"), MODULE_BACKEND_STATUSES)
    license_state = _validate_choice("license_state", payload.get("license_state"), MODULE_LICENSE_STATES)
    module_state = derive_module_state(
        edition=edition,
        backend_status=backend_status,
        license_state=license_state,
    )
    declared_state = _text(payload.get("module_state"))
    if declared_state and declared_state != module_state:
        raise ModuleManifestValidationError(f"module_state_mismatch:{declared_state}!={module_state}")
    ui_payload = _mapping(payload.get("ui"), "ui")
    return AgvmModuleManifest(
        module_id=_clean_required_text("module_id", payload.get("module_id")),
        module_version=_clean_required_text("module_version", payload.get("module_version")),
        edition=edition,  # type: ignore[arg-type]
        backend_status=backend_status,  # type: ignore[arg-type]
        license_state=license_state,  # type: ignore[arg-type]
        available=bool(payload.get("available", False)),
        api_base_path=_normalize_api_base_path(_clean_required_text("api_base_path", payload.get("api_base_path"))),
        ui=_parse_ui_bundle(ui_payload),
        capabilities=_parse_capabilities(payload.get("capabilities")),
        mcp_tools=_parse_mcp_tools(payload.get("mcp_tools")),
        license=_parse_license(payload.get("license")),
        safe_fallback_message=_clean_required_text("safe_fallback_message", payload.get("safe_fallback_message")),
        diagnostics=dict(_mapping(payload.get("diagnostics", {}), "diagnostics")),
        module_state=module_state,
    )


def _normalize_legacy_clone_app_manifest(payload: Mapping[str, Any]) -> AgvmModuleManifest:
    edition = _validate_choice("edition", payload.get("edition"), MODULE_EDITIONS)
    backend_status = _validate_choice("backend_status", payload.get("backend_status"), MODULE_BACKEND_STATUSES)
    license_state = _validate_choice("license_state", payload.get("license_state"), MODULE_LICENSE_STATES - {"not_required"})
    ui_mounts = _parse_ui_mounts(payload.get("ui_mounts", []))
    ui = AgvmModuleUiBundle(kind="local_route" if ui_mounts else "none", mounts=ui_mounts)
    capabilities = _parse_capabilities(payload.get("capabilities"))
    diagnostics = dict(_mapping(payload.get("diagnostics", {}), "diagnostics"))
    diagnostics["legacy_schema_version"] = LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION
    legacy_flags = {
        "mutation_allowed": bool(payload.get("mutation_allowed", False)),
        "apply_locked": bool(payload.get("apply_locked", True)),
        "mutates_agvm_memory": bool(payload.get("mutates_agvm_memory", False)),
    }
    diagnostics["legacy_safety_flags"] = legacy_flags
    module_state = derive_module_state(
        edition=edition,
        backend_status=backend_status,
        license_state=license_state,
    )
    return AgvmModuleManifest(
        module_id=_clean_required_text("module_id", payload.get("module_id")),
        module_version=_clean_required_text("module_version", payload.get("version")),
        edition=edition,  # type: ignore[arg-type]
        backend_status=backend_status,  # type: ignore[arg-type]
        license_state=license_state,  # type: ignore[arg-type]
        available=bool(payload.get("available", False)),
        api_base_path=_normalize_api_base_path(_clean_required_text("api_base_path", payload.get("api_base_path"))),
        ui=ui,
        capabilities=capabilities,
        mcp_tools=AgvmModuleMcpTools(uses_core_tools=["retrieve_context", "write_memory_preview"]),
        license=AgvmModuleLicense(plan_required="pro" if edition == "paid" else None),
        safe_fallback_message=_clean_required_text("safe_fallback_message", payload.get("safe_fallback_message")),
        diagnostics=diagnostics,
        module_state=module_state,
        source_schema_version=LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION,
    )


def _parse_ui_bundle(payload: Mapping[str, Any]) -> AgvmModuleUiBundle:
    kind = _validate_choice("ui.kind", payload.get("kind"), MODULE_UI_KINDS)
    return AgvmModuleUiBundle(
        kind=kind,  # type: ignore[arg-type]
        entry_url=_optional_text(payload.get("entry_url")),
        integrity=_optional_text(payload.get("integrity")),
        mounts=_parse_ui_mounts(payload.get("mounts", [])),
    )


def _parse_ui_mounts(value: Any) -> list[AgvmModuleUiMount]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ModuleManifestValidationError("ui_mounts_must_be_a_list")
    mounts: list[AgvmModuleUiMount] = []
    for index, item in enumerate(value):
        item_payload = _mapping(item, f"ui.mounts[{index}]")
        mounts.append(
            AgvmModuleUiMount(
                route_id=_clean_required_text("route_id", item_payload.get("route_id")),
                label=_clean_required_text("label", item_payload.get("label")),
                path=_clean_required_text("path", item_payload.get("path")),
                nav_group=_clean_required_text("nav_group", item_payload.get("nav_group")),
                required_capability=_clean_required_text("required_capability", item_payload.get("required_capability")),
                description=_text(item_payload.get("description")),
            )
        )
    return mounts


def _parse_capabilities(value: Any) -> dict[str, bool]:
    if value is None:
        return {}
    payload = _mapping(value, "capabilities")
    result: dict[str, bool] = {}
    for key, enabled in payload.items():
        clean_key = _text(key)
        if clean_key:
            result[clean_key] = bool(enabled)
    return result


def _parse_mcp_tools(value: Any) -> AgvmModuleMcpTools:
    if value is None:
        return AgvmModuleMcpTools()
    payload = _mapping(value, "mcp_tools")
    return AgvmModuleMcpTools(
        adds_tools=_parse_text_list(payload.get("adds_tools", []), "mcp_tools.adds_tools"),
        uses_core_tools=_parse_text_list(payload.get("uses_core_tools", []), "mcp_tools.uses_core_tools"),
    )


def _parse_license(value: Any) -> AgvmModuleLicense:
    if value is None:
        return AgvmModuleLicense()
    payload = _mapping(value, "license")
    return AgvmModuleLicense(
        plan_required=_optional_text(payload.get("plan_required")),
        lease_expires_at=_optional_text(payload.get("lease_expires_at")),
    )


def _parse_text_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ModuleManifestValidationError(f"{name}_must_be_a_list")
    return [_clean_required_text(name, item) for item in value]


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModuleManifestValidationError(f"{name}_must_be_an_object")
    return value


def _normalize_api_base_path(value: str) -> str:
    clean = _clean_required_text("api_base_path", value).strip()
    if not clean.startswith("/"):
        clean = f"/{clean}"
    return clean.rstrip("/") or "/"


def _validate_choice(name: str, value: Any, allowed: set[str]) -> str:
    clean = _clean_required_text(name, value)
    _ensure_allowed(name, clean, allowed)
    return clean


def _ensure_allowed(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ModuleManifestValidationError(f"{name}_is_not_supported:{value}")


def _ensure_text_sequence(name: str, values: Sequence[str]) -> None:
    for value in values:
        _require_text(name, value)


def _clean_required_text(name: str, value: Any) -> str:
    clean = _text(value)
    _require_text(name, clean)
    return clean


def _require_text(name: str, value: str) -> None:
    if not _text(value):
        raise ModuleManifestValidationError(f"{name}_is_required")


def _optional_text(value: Any) -> str | None:
    clean = _text(value)
    return clean or None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
