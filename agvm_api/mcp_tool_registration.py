from __future__ import annotations

from typing import Any, Mapping


MCP_TOOL_REGISTRATION_SCHEMA_VERSION = "agvm.mcp_tool_registration.v1"
MCP_MODULE_TOOL_REGISTRATION_SCHEMA_VERSION = "agvm.mcp_module_tool_registration.v1"
MCP_TOOL_REGISTRATION_STATE = "core_mcp_module_tool_registration_boundary"
MCP_CORE_TOOL_OWNER_ID = "agvm_core_mcp"

GROW_MODULE_ID = "agvm_grow_studio"
MAINTAIN_MODULE_ID = "agvm_maintain_studio"

MAINTAIN_LIST_TOOL_NAMES = {
    "list_open_questions",
    "list_hypotheses",
    "list_contradictions",
    "list_memory_os_processes",
}


def required_module_id_for_tool_name(tool_name: str) -> str | None:
    clean = str(tool_name or "").strip()
    if clean.startswith("grow_"):
        return GROW_MODULE_ID
    if clean.startswith(("matrix_calibration_", "sleep_", "evolve_")) or clean in MAINTAIN_LIST_TOOL_NAMES:
        return MAINTAIN_MODULE_ID
    return None


def build_mcp_tool_registration(
    *,
    tool_name: str,
    category: str,
    endpoint_path: str,
    http_method: str,
    permission_family: str,
    required_module_id: str | None = None,
) -> dict[str, Any]:
    clean_name = str(tool_name or "").strip()
    module_id = _optional_text(required_module_id) or required_module_id_for_tool_name(clean_name)
    module_required = bool(module_id)
    owner_id = module_id or MCP_CORE_TOOL_OWNER_ID
    return {
        "schema_version": MCP_TOOL_REGISTRATION_SCHEMA_VERSION,
        "state": MCP_TOOL_REGISTRATION_STATE,
        "tool_name": clean_name,
        "tool_owner": "module" if module_required else "core",
        "owner_id": owner_id,
        "required_module_id": module_id,
        "entitlement_required": module_required,
        "public_core_allowed": not module_required,
        "category": str(category or "").strip(),
        "endpoint_path": str(endpoint_path or "").strip(),
        "http_method": str(http_method or "POST").upper(),
        "permission_family": str(permission_family or "").strip(),
        "visibility_policy": "module_entitlement_required" if module_required else "permission_policy_only",
        "registration_source": "module_manifest_or_private_adapter" if module_required else "core_contract_registry",
        "module_manifest_field": "mcp_tools.uses_core_tools" if module_required else None,
        "current_runtime_binding": "core_compatibility_endpoint",
    }


def build_module_requirement_from_registration(registration: Mapping[str, Any]) -> dict[str, Any]:
    module_id = _optional_text(registration.get("required_module_id"))
    return {
        "required": bool(module_id),
        "module_id": module_id,
        "entitlement_required": bool(registration.get("entitlement_required", bool(module_id))),
        "visibility_policy": str(registration.get("visibility_policy") or "permission_policy_only"),
    }


def required_module_id_from_tool_contract(tool: Mapping[str, Any]) -> str | None:
    registration = _mapping(tool.get("tool_registration"))
    module_id = _optional_text(registration.get("required_module_id"))
    if module_id:
        return module_id
    module_requirement = _mapping(tool.get("module_requirement"))
    module_id = _optional_text(module_requirement.get("module_id"))
    if module_id:
        return module_id
    return required_module_id_for_tool_name(str(tool.get("name") or ""))


def build_mcp_module_tool_registration_summary(tools: list[Mapping[str, Any]]) -> dict[str, Any]:
    core_tool_names: list[str] = []
    module_tool_names: list[str] = []
    tools_by_required_module: dict[str, list[str]] = {}
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        module_id = required_module_id_from_tool_contract(tool)
        if module_id:
            module_tool_names.append(name)
            tools_by_required_module.setdefault(module_id, []).append(name)
        else:
            core_tool_names.append(name)
    return {
        "schema_version": MCP_MODULE_TOOL_REGISTRATION_SCHEMA_VERSION,
        "state": MCP_TOOL_REGISTRATION_STATE,
        "core_owner_id": MCP_CORE_TOOL_OWNER_ID,
        "core_tool_names": core_tool_names,
        "module_tool_names": module_tool_names,
        "required_module_ids": sorted(tools_by_required_module),
        "tools_by_required_module": {key: sorted(value) for key, value in sorted(tools_by_required_module.items())},
        "filter_contract": {
            "required_module_field": "tool_registration.required_module_id",
            "entitlement_required_field": "tool_registration.entitlement_required",
            "hosted_gateway_filter": "active module_access entitlement required when required_module_id is set",
            "local_gateway_filter": "permission families still apply; local module lease filtering is a later supervisor slice",
        },
    }


def validate_mcp_tool_registration(tool: Mapping[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    name = str(tool.get("name") or "").strip()
    registration = _mapping(tool.get("tool_registration"))
    if not registration:
        return [{"tool": name, "schema": "tool_registration", "reason": "tool_registration_missing"}]
    if registration.get("schema_version") != MCP_TOOL_REGISTRATION_SCHEMA_VERSION:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "schema_version_not_supported"})
    if registration.get("state") != MCP_TOOL_REGISTRATION_STATE:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "registration_state_not_supported"})
    if str(registration.get("tool_name") or "").strip() != name:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "tool_name_mismatch"})
    owner = str(registration.get("tool_owner") or "").strip()
    if owner not in {"core", "module"}:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "tool_owner_not_supported"})
    module_id = _optional_text(registration.get("required_module_id"))
    if owner == "module" and not module_id:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "module_owner_requires_module_id"})
    if owner == "core" and module_id:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "core_owner_must_not_require_module"})
    expected_module_id = required_module_id_for_tool_name(name)
    if expected_module_id is not None and expected_module_id != module_id:
        errors.append({"tool": name, "schema": "tool_registration", "reason": "required_module_id_mismatch"})
    if bool(registration.get("entitlement_required")) != bool(module_id):
        errors.append({"tool": name, "schema": "tool_registration", "reason": "entitlement_required_mismatch"})
    return errors


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
