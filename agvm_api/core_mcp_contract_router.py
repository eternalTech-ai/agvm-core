# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from agent_memory_guide import build_agvm_usage_guide
from mcp_contracts import build_local_core_mcp_contract_registry, validate_mcp_contract_registry
from mcp_tool_registration import (
    MAINTAIN_MODULE_ID,
    build_mcp_module_tool_registration_summary,
    build_module_requirement_from_registration,
    mark_local_core_cloud_handoff_registration,
)
from schemas import AgvmUsageGuideResponse, McpContractRegistryResponse


_PUBLIC_LOCAL_CLOUD_HANDOFF_TOOLS = frozenset(
    {
        "brain_profile_preview",
        "brain_profile_apply",
        "brain_profile_rollback",
    }
)


def _build_public_local_core_mcp_contract_registry() -> dict[str, Any]:
    registry = build_local_core_mcp_contract_registry()
    tools = [dict(tool) for tool in list(registry.get("tools") or [])]
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if name not in _PUBLIC_LOCAL_CLOUD_HANDOFF_TOOLS:
            continue
        registration = mark_local_core_cloud_handoff_registration(
            {
                **dict(tool.get("tool_registration") or {}),
                "tool_owner": "module",
                "owner_id": MAINTAIN_MODULE_ID,
                "required_module_id": MAINTAIN_MODULE_ID,
                "entitlement_required": True,
                "public_core_allowed": False,
                "visibility_policy": "module_entitlement_required",
                "registration_source": "public_core_cloud_handoff",
            }
        )
        tool["tool_registration"] = registration
        tool["module_requirement"] = build_module_requirement_from_registration(registration)
        tool["backend_binding"] = {
            **dict(tool.get("backend_binding") or {}),
            "runtime": "hosted_mcp",
            "local_adapter": "cloud_handoff_only",
            "local_execution_available": False,
        }
        tool["safety_contract"] = {
            **dict(tool.get("safety_contract") or {}),
            "local_graph_mutation": "forbidden",
            "cloud_execution_required": True,
            "entitlement_bypass_allowed": False,
        }
    names = {str(tool.get("name") or "").strip() for tool in tools}
    required_tool_names = [
        str(name)
        for name in list(registry.get("required_tool_names") or [])
        if str(name) in names
    ]
    registry.update(
        {
            "required_tool_names": required_tool_names,
            "agent_memory_tool_names": [
                str(name)
                for name in list(registry.get("agent_memory_tool_names") or [])
                if str(name) in names
            ],
            "tools": tools,
            "module_tool_registration": build_mcp_module_tool_registration_summary(tools),
        }
    )
    registry["registry_validation"] = validate_mcp_contract_registry(
        registry,
        required_tool_names=required_tool_names,
    )
    return registry


def create_core_mcp_contract_router() -> APIRouter:
    router = APIRouter()

    @router.get("/memory/mcp/contracts", response_model=McpContractRegistryResponse)
    def memory_mcp_contracts_endpoint() -> McpContractRegistryResponse:
        return McpContractRegistryResponse(**_build_public_local_core_mcp_contract_registry())

    @router.get("/mcp/contracts", response_model=McpContractRegistryResponse)
    def mcp_contracts_endpoint() -> McpContractRegistryResponse:
        return memory_mcp_contracts_endpoint()

    @router.get("/memory/mcp/tools", response_model=McpContractRegistryResponse)
    def memory_mcp_tools_endpoint() -> McpContractRegistryResponse:
        return memory_mcp_contracts_endpoint()

    @router.get("/mcp/usage-guide", response_model=AgvmUsageGuideResponse)
    def mcp_usage_guide_endpoint() -> AgvmUsageGuideResponse:
        return AgvmUsageGuideResponse(**build_agvm_usage_guide())

    return router
