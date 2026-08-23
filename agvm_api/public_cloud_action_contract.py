# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Public fail-closed response contract for Cloud-backed MCP operations."""

from __future__ import annotations

from typing import Any


PUBLIC_CLOUD_ACTION_STUB = True


def cloud_backed_tool_response(
    tool_name: str,
    *,
    capability: str,
    required_module_id: str,
) -> dict[str, Any]:
    """Return a discoverable action without executing paid code locally."""

    action_contract = {
        "schema_version": "agvm.local_mcp_paid_tool_action.v1",
        "action": "use_detwin_cloud_for_advanced_tool",
        "tool_name": tool_name,
        "capability": capability,
        "required_module_id": required_module_id,
        "execution_surface": "hosted_mcp",
        "requires_account": True,
        "requires_credits": True,
        "dynamic_usage_settlement": True,
        "credential_environment_variable": "AGVM_HOSTED_MCP_API_KEY",
        "cloud_workspace_url": "https://cloud.detwin.ai/",
        "hosted_mcp_key_setup_url": "https://app.detwin.ai/account/mcp",
        "local_execution_available": False,
    }
    return {
        "schema_version": "agvm.public_core.cloud_backed_tool.v1",
        "tool_name": tool_name,
        "status": "blocked",
        "reason": "detwin_cloud_execution_required",
        "recovery": (
            "Connect a Detwin account and Hosted MCP key, then run this tool in "
            "Detwin Cloud. Public Core does not contain its paid implementation."
        ),
        "action_contract": action_contract,
        "data": {"action_contract": action_contract},
    }
