# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from fastapi import APIRouter

from agent_memory_guide import build_agvm_usage_guide
from mcp_contracts import build_mcp_contract_registry
from schemas import AgvmUsageGuideResponse, McpContractRegistryResponse


def create_core_mcp_contract_router() -> APIRouter:
    router = APIRouter()

    @router.get("/memory/mcp/contracts", response_model=McpContractRegistryResponse)
    def memory_mcp_contracts_endpoint() -> McpContractRegistryResponse:
        return McpContractRegistryResponse(**build_mcp_contract_registry())

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
