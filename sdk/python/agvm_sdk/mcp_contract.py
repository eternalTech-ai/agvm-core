# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


MCP_CONTRACT_REGISTRY_SCHEMA_VERSION = "agvm.mcp_contract_registry.v1"
MCP_TOOL_CONTRACT_SCHEMA_VERSION = "agvm.mcp_tool_contract.v1"
MCP_TOOL_REGISTRATION_SCHEMA_VERSION = "agvm.mcp_tool_registration.v1"
MCP_TOOL_REGISTRATION_STATE = "core_mcp_module_tool_registration_boundary"

MCP_CONTRACT_HTTP_METHODS = {"GET", "POST"}
MCP_CONTRACT_SCOPE_POLICIES = {
    "global",
    "registry",
    "brain",
    "brain_preview",
    "brain_apply",
    "hosted_registry",
}
MCP_CONTRACT_PERMISSION_FAMILIES = {
    "read_only",
    "read_only_export",
    "registry_write",
    "preview_only",
    "explicit_apply",
    "destructive",
}


class McpContractError(ValueError):
    pass


@dataclass(frozen=True)
class McpToolContractSummary:
    tool_name: str
    scope_policy: str
    permission_family: str
    http_method: str
    endpoint: str
    tool_owner: str = "core"
    required_module_id: str | None = None
    entitlement_required: bool = False
    public_core_allowed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "scope_policy": self.scope_policy,
            "permission_family": self.permission_family,
            "http_method": self.http_method,
            "endpoint": self.endpoint,
            "tool_owner": self.tool_owner,
            "required_module_id": self.required_module_id,
            "entitlement_required": self.entitlement_required,
            "public_core_allowed": self.public_core_allowed,
        }


def normalize_mcp_tool_contract_summary(payload: Mapping[str, Any]) -> McpToolContractSummary:
    tool_name = clean_required_text(payload.get("tool_name"), "tool_name")
    scope_policy = clean_required_text(payload.get("scope_policy"), "scope_policy")
    permission_family = clean_required_text(payload.get("permission_family"), "permission_family")
    http_method = clean_required_text(payload.get("http_method"), "http_method").upper()
    endpoint = clean_required_text(payload.get("endpoint"), "endpoint")
    registration = payload.get("tool_registration")
    registration_payload = registration if isinstance(registration, Mapping) else {}
    tool_owner = clean_optional_text(payload.get("tool_owner") or registration_payload.get("tool_owner")) or "core"
    required_module_id = clean_optional_text(payload.get("required_module_id") or registration_payload.get("required_module_id"))
    entitlement_required = bool(payload.get("entitlement_required", registration_payload.get("entitlement_required", bool(required_module_id))))
    public_core_allowed = bool(payload.get("public_core_allowed", registration_payload.get("public_core_allowed", not required_module_id)))
    if scope_policy not in MCP_CONTRACT_SCOPE_POLICIES:
        raise McpContractError(f"unsupported_scope_policy:{scope_policy}")
    if permission_family not in MCP_CONTRACT_PERMISSION_FAMILIES:
        raise McpContractError(f"unsupported_permission_family:{permission_family}")
    if http_method not in MCP_CONTRACT_HTTP_METHODS:
        raise McpContractError(f"unsupported_http_method:{http_method}")
    if not endpoint.startswith("/"):
        raise McpContractError("endpoint_must_start_with_slash")
    if tool_owner not in {"core", "module"}:
        raise McpContractError(f"unsupported_tool_owner:{tool_owner}")
    if tool_owner == "module" and not required_module_id:
        raise McpContractError("module_tool_requires_required_module_id")
    if tool_owner == "core" and required_module_id:
        raise McpContractError("core_tool_must_not_require_module_id")
    if entitlement_required != bool(required_module_id):
        raise McpContractError("entitlement_required_must_match_required_module_id")
    if public_core_allowed == bool(required_module_id):
        raise McpContractError("public_core_allowed_must_be_false_for_module_tools")
    return McpToolContractSummary(
        tool_name=tool_name,
        scope_policy=scope_policy,
        permission_family=permission_family,
        http_method=http_method,
        endpoint=endpoint,
        tool_owner=tool_owner,
        required_module_id=required_module_id,
        entitlement_required=entitlement_required,
        public_core_allowed=public_core_allowed,
    )


def clean_required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise McpContractError(f"{name}_required")
    if "\n" in text or "\r" in text:
        raise McpContractError(f"{name}_must_be_single_line")
    return text


def clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "\n" in text or "\r" in text:
        raise McpContractError("optional_text_must_be_single_line")
    return text
