from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO


JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agvm-local-memory-os"
SERVER_VERSION = "0.12.0-pr12p-11"

MUTATION_TOOLS = {
    "create_brain",
    "ensure_brain",
    "grow_source_apply",
    "grow_apply",
    "select_brain",
    "write_memory_commit",
    "sleep_apply",
    "evolve_apply",
    "matrix_calibration_apply",
}
MUTATING_PERMISSION_FAMILIES = {"registry_write", "explicit_apply", "destructive"}
READ_ONLY_PERMISSION_FAMILIES = {"read_only", "read_only_export", "preview_only"}
DEFAULT_ALLOWED_PERMISSION_FAMILIES = ("read_only", "read_only_export", "registry_write", "preview_only", "explicit_apply")
DEFAULT_BLOCKED_PERMISSION_FAMILIES = ("destructive",)
LEGACY_TOOL_ALLOWLIST_FAMILIES = {"explicit_apply", "destructive"}
BRAIN_POLICY_FIXED = "fixed"
BRAIN_POLICY_AI_RESOLVE_EXISTING = "ai_resolve_existing"
BRAIN_POLICY_AI_CREATE_IF_MISSING = "ai_create_if_missing"
BRAIN_POLICIES = {BRAIN_POLICY_FIXED, BRAIN_POLICY_AI_RESOLVE_EXISTING, BRAIN_POLICY_AI_CREATE_IF_MISSING}
MODULE_VISIBILITY_METADATA_ONLY = "metadata_only"
MODULE_VISIBILITY_HIDE_UNLICENSED = "hide_unlicensed"
MODULE_VISIBILITY_BLOCK_UNLICENSED = "block_unlicensed"
DEFAULT_DETWIN_PLATFORM_URL = "https://app.detwin.ai"
DEFAULT_DETWIN_CLOUD_URL = "https://cloud.detwin.ai"
MODULE_VISIBILITY_POLICIES = {
    MODULE_VISIBILITY_METADATA_ONLY,
    MODULE_VISIBILITY_HIDE_UNLICENSED,
    MODULE_VISIBILITY_BLOCK_UNLICENSED,
}
BRAIN_SCOPE_OPTIONAL_TOOLS = {
    "get_agvm_usage_guide",
    "list_brains",
    "active_brain",
    "create_brain",
    "select_brain",
    "ensure_brain",
}


class AgvmMcpError(RuntimeError):
    pass


class AgvmMcpHttpError(AgvmMcpError):
    def __init__(self, *, status: int | None, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


@dataclass(frozen=True)
class ToolPermissions:
    read_only: bool = False
    enabled_tools: tuple[str, ...] = ("*",)
    disabled_tools: tuple[str, ...] = ()
    allowed_permission_families: tuple[str, ...] = DEFAULT_ALLOWED_PERMISSION_FAMILIES
    blocked_permission_families: tuple[str, ...] = DEFAULT_BLOCKED_PERMISSION_FAMILIES
    allow_mutation_tools: tuple[str, ...] = tuple(sorted(MUTATION_TOOLS))

    def is_enabled(self, tool_name: str) -> bool:
        if tool_name in self.disabled_tools:
            return False
        return "*" in self.enabled_tools or tool_name in self.enabled_tools

    def is_visible(self, tool_name: str, *, permission_family: str | None = None) -> bool:
        return self.is_enabled(tool_name)

    def can_call(self, tool_name: str, *, permission_family: str | None = None) -> tuple[bool, str | None]:
        if not self.is_enabled(tool_name):
            return False, "tool_disabled_by_local_mcp_config"
        family = self._normalized_permission_family(permission_family)
        if self.read_only and self._is_write_family_or_legacy_mutation(tool_name, family):
            return False, "mutation_tool_blocked_by_read_only_local_mcp_config"
        if family in self.blocked_permission_families:
            return False, "permission_family_blocked_by_local_mcp_config"
        if family and "*" not in self.allowed_permission_families and family not in self.allowed_permission_families:
            return False, "permission_family_not_allowed_by_local_mcp_config"
        if self._legacy_allowlist_applies(tool_name, family) and "*" not in self.allow_mutation_tools and tool_name not in self.allow_mutation_tools:
            return False, "mutation_tool_not_allowlisted_by_local_mcp_config"
        return True, None

    def _normalized_permission_family(self, permission_family: str | None) -> str:
        return str(permission_family or "").strip()

    def _is_write_family_or_legacy_mutation(self, tool_name: str, permission_family: str) -> bool:
        return permission_family in MUTATING_PERMISSION_FAMILIES or tool_name in MUTATION_TOOLS

    def _permission_family_allowed(self, permission_family: str) -> bool:
        if not permission_family:
            return True
        if permission_family in self.blocked_permission_families:
            return False
        return "*" in self.allowed_permission_families or permission_family in self.allowed_permission_families

    def _legacy_allowlist_applies(self, tool_name: str, permission_family: str) -> bool:
        return tool_name in MUTATION_TOOLS and (not permission_family or permission_family in LEGACY_TOOL_ALLOWLIST_FAMILIES)


@dataclass(frozen=True)
class LocalModuleAccessPolicy:
    visibility_policy: str = MODULE_VISIBILITY_BLOCK_UNLICENSED
    license_state_path: str | None = None
    status_source: str = "local_license_supervisor"

    def __post_init__(self) -> None:
        normalized = str(self.visibility_policy or MODULE_VISIBILITY_BLOCK_UNLICENSED).strip().lower()
        if normalized not in MODULE_VISIBILITY_POLICIES:
            raise AgvmMcpError(
                "AGVM MCP module visibility policy must be one of "
                f"{', '.join(sorted(MODULE_VISIBILITY_POLICIES))}; got {normalized!r}"
            )
        object.__setattr__(self, "visibility_policy", normalized)
        clean_path = str(self.license_state_path or "").strip() or None
        object.__setattr__(self, "license_state_path", clean_path)

    @property
    def enabled(self) -> bool:
        return self.visibility_policy != MODULE_VISIBILITY_METADATA_ONLY

    @property
    def hides_unlicensed_tools(self) -> bool:
        return self.visibility_policy == MODULE_VISIBILITY_HIDE_UNLICENSED

    @property
    def blocks_unlicensed_calls(self) -> bool:
        return self.visibility_policy in {MODULE_VISIBILITY_HIDE_UNLICENSED, MODULE_VISIBILITY_BLOCK_UNLICENSED}

    def status_for_module(self, module_id: str) -> dict[str, Any]:
        clean_module_id = str(module_id or "").strip()
        if not clean_module_id:
            return {
                "schema_version": "agvm.local_module_entitlement_status.v1",
                "module_id": None,
                "granted": True,
                "module_state": "not_required",
                "license_state": "not_required",
                "reason": "core_tool",
                "lease_present": False,
                "token_present": False,
            }
        if not self.enabled:
            return {
                "schema_version": "agvm.local_module_entitlement_status.v1",
                "module_id": clean_module_id,
                "granted": True,
                "module_state": "not_enforced",
                "license_state": "not_enforced",
                "reason": "metadata_only_local_mcp_module_visibility",
                "lease_present": False,
                "token_present": False,
            }
        try:
            module_entitlement_status = _load_module_entitlement_status_provider()
            path = Path(self.license_state_path).expanduser() if self.license_state_path else None
            return dict(module_entitlement_status(clean_module_id, path=path))
        except Exception as exc:
            return {
                "schema_version": "agvm.local_module_entitlement_status.v1",
                "module_id": clean_module_id,
                "granted": False,
                "module_state": "unavailable",
                "license_state": "invalid",
                "reason": "local_module_entitlement_status_unavailable",
                "provider_error_type": type(exc).__name__,
                "lease_present": False,
                "token_present": False,
            }


@dataclass(frozen=True)
class AgvmMcpConfig:
    api_base_url: str = "http://127.0.0.1:8010"
    active_brain_id: str | None = None
    default_brain_id: str | None = None
    brain_policy: str = BRAIN_POLICY_FIXED
    brain_id_hint: str | None = None
    brain_display_name: str | None = None
    brain_purpose: str | None = None
    tenant_id: str | None = None
    organization_id: str | None = None
    user_id: str | None = None
    environment_id: str | None = None
    request_timeout_seconds: float = 180.0
    tool_permissions: ToolPermissions = field(default_factory=ToolPermissions)
    module_access: LocalModuleAccessPolicy = field(default_factory=LocalModuleAccessPolicy)

    def __post_init__(self) -> None:
        normalized_policy = str(self.brain_policy or BRAIN_POLICY_FIXED).strip().lower()
        if normalized_policy not in BRAIN_POLICIES:
            raise AgvmMcpError(
                "AGVM MCP brain_policy must be one of "
                f"{', '.join(sorted(BRAIN_POLICIES))}; got {normalized_policy!r}"
            )
        object.__setattr__(self, "brain_policy", normalized_policy)

    @property
    def selected_brain_id(self) -> str | None:
        if self.brain_policy != BRAIN_POLICY_FIXED:
            return None
        return self.active_brain_id or self.default_brain_id

    @property
    def requires_ai_brain_resolution(self) -> bool:
        return self.brain_policy in {BRAIN_POLICY_AI_RESOLVE_EXISTING, BRAIN_POLICY_AI_CREATE_IF_MISSING}

    @property
    def hosted_scope_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.tenant_id:
            headers["X-AGVM-Tenant-Id"] = self.tenant_id
        if self.organization_id:
            headers["X-AGVM-Organization-Id"] = self.organization_id
        if self.user_id:
            headers["X-AGVM-User-Id"] = self.user_id
        if self.environment_id:
            headers["X-AGVM-Environment-Id"] = self.environment_id
        return headers


def _as_tuple(value: Any, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return default


def _as_env_tuple(env_name: str) -> tuple[str, ...] | None:
    raw = os.environ.get(env_name)
    if raw is None:
        return None
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values


def _module_access_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("module_access") or payload.get("module_visibility") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _load_module_entitlement_status_provider() -> Any:
    try:
        from agvm_api.local_entitlements import module_entitlement_status

        return module_entitlement_status
    except ImportError:
        from local_entitlements import module_entitlement_status  # type: ignore[no-redef]

        return module_entitlement_status


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise AgvmMcpError(f"AGVM MCP config must be a JSON object: {path}")
    return payload


def load_config(path: str | os.PathLike[str] | None = None) -> AgvmMcpConfig:
    payload: dict[str, Any] = {}
    config_path_value = path or os.environ.get("AGVM_MCP_CONFIG")
    if config_path_value:
        config_path = Path(config_path_value)
        if not config_path.exists():
            raise AgvmMcpError(f"AGVM MCP config file not found: {config_path}")
        payload = _load_json(config_path)

    permissions_payload = dict(payload.get("tool_permissions") or {})
    read_only_env = os.environ.get("AGVM_MCP_READ_ONLY")
    allowed_families_env = _as_env_tuple("AGVM_MCP_ALLOWED_PERMISSION_FAMILIES")
    blocked_families_env = _as_env_tuple("AGVM_MCP_BLOCKED_PERMISSION_FAMILIES")
    permissions = ToolPermissions(
        read_only=(
            str(read_only_env).strip().lower() in {"1", "true", "yes", "on"}
            if read_only_env is not None
            else bool(permissions_payload.get("read_only", False))
        ),
        enabled_tools=_as_tuple(permissions_payload.get("enabled_tools"), default=("*",)),
        disabled_tools=_as_tuple(permissions_payload.get("disabled_tools"), default=()),
        allowed_permission_families=(
            allowed_families_env
            if allowed_families_env is not None
            else _as_tuple(permissions_payload.get("allowed_permission_families"), default=DEFAULT_ALLOWED_PERMISSION_FAMILIES)
        ),
        blocked_permission_families=(
            blocked_families_env
            if blocked_families_env is not None
            else _as_tuple(permissions_payload.get("blocked_permission_families"), default=DEFAULT_BLOCKED_PERMISSION_FAMILIES)
        ),
        allow_mutation_tools=_as_tuple(permissions_payload.get("allow_mutation_tools"), default=tuple(sorted(MUTATION_TOOLS))),
    )
    module_access_payload = _module_access_payload(payload)
    module_visibility_env = os.environ.get("AGVM_MCP_MODULE_VISIBILITY_POLICY")
    local_license_path_env = os.environ.get("AGVM_MCP_LOCAL_LICENSE_PATH")
    module_access = LocalModuleAccessPolicy(
        visibility_policy=(
            str(module_visibility_env).strip()
            if module_visibility_env is not None
            else str(module_access_payload.get("visibility_policy") or MODULE_VISIBILITY_BLOCK_UNLICENSED)
        ),
        license_state_path=(
            str(local_license_path_env).strip()
            if local_license_path_env is not None
            else (str(module_access_payload.get("license_state_path")).strip() if module_access_payload.get("license_state_path") else None)
        ),
        status_source=str(module_access_payload.get("status_source") or "local_license_supervisor"),
    )

    api_base_url = str(os.environ.get("AGVM_API_BASE_URL") or payload.get("api_base_url") or "http://127.0.0.1:8010").rstrip("/")
    brain_policy = str(os.environ.get("AGVM_MCP_BRAIN_POLICY") or payload.get("brain_policy") or BRAIN_POLICY_FIXED).strip().lower()
    if brain_policy not in BRAIN_POLICIES:
        raise AgvmMcpError(
            "AGVM MCP brain_policy must be one of "
            f"{', '.join(sorted(BRAIN_POLICIES))}; got {brain_policy!r}"
        )
    brain_from_env = str(os.environ.get("AGVM_MCP_BRAIN_ID") or "").strip() or None
    active_brain_id = brain_from_env or (str(payload.get("active_brain_id")).strip() if payload.get("active_brain_id") else None)
    default_brain_id = brain_from_env or (str(payload.get("default_brain_id")).strip() if payload.get("default_brain_id") else None)
    brain_id_hint = str(os.environ.get("AGVM_MCP_BRAIN_ID_HINT") or payload.get("brain_id_hint") or "").strip() or None
    brain_display_name = str(os.environ.get("AGVM_MCP_BRAIN_DISPLAY_NAME") or payload.get("brain_display_name") or "").strip() or None
    brain_purpose = str(os.environ.get("AGVM_MCP_BRAIN_PURPOSE") or payload.get("brain_purpose") or "").strip() or None
    tenant_id = str(os.environ.get("AGVM_MCP_TENANT_ID") or payload.get("tenant_id") or "").strip() or None
    organization_id = str(os.environ.get("AGVM_MCP_ORGANIZATION_ID") or payload.get("organization_id") or "").strip() or None
    user_id = str(os.environ.get("AGVM_MCP_USER_ID") or payload.get("user_id") or "").strip() or None
    environment_id = str(os.environ.get("AGVM_MCP_ENVIRONMENT_ID") or payload.get("environment_id") or "").strip() or None
    timeout = float(os.environ.get("AGVM_MCP_TIMEOUT_SECONDS") or payload.get("request_timeout_seconds") or 180.0)

    return AgvmMcpConfig(
        api_base_url=api_base_url,
        active_brain_id=active_brain_id,
        default_brain_id=default_brain_id,
        brain_policy=brain_policy,
        brain_id_hint=brain_id_hint,
        brain_display_name=brain_display_name,
        brain_purpose=brain_purpose,
        tenant_id=tenant_id,
        organization_id=organization_id,
        user_id=user_id,
        environment_id=environment_id,
        request_timeout_seconds=timeout,
        tool_permissions=permissions,
        module_access=module_access,
    )


class AgvmHttpClient:
    def __init__(self, config: AgvmMcpConfig) -> None:
        self.config = config

    def get_json(self, path: str, *, brain_id: str | None = None, hosted_scope: dict[str, str | None] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, payload=None, brain_id=brain_id, hosted_scope=hosted_scope)

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        brain_id: str | None = None,
        hosted_scope: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        return self._request_json(str(method or "POST").upper(), path, payload=payload, brain_id=brain_id, hosted_scope=hosted_scope)

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        brain_id: str | None = None,
        hosted_scope: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        return self._request_json("POST", path, payload=payload, brain_id=brain_id, hosted_scope=hosted_scope)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        brain_id: str | None,
        hosted_scope: dict[str, str | None] | None,
    ) -> dict[str, Any]:
        if method.upper() == "GET" and payload:
            path = _path_with_query(path, payload)
            payload = None
        url = f"{self.config.api_base_url}{path if path.startswith('/') else '/' + path}"
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if brain_id:
            headers["X-AGVM-Brain-Id"] = brain_id
        headers.update(self.config.hosted_scope_headers)
        for key, header_name in {
            "tenant_id": "X-AGVM-Tenant-Id",
            "organization_id": "X-AGVM-Organization-Id",
            "user_id": "X-AGVM-User-Id",
            "environment_id": "X-AGVM-Environment-Id",
        }.items():
            value = str((hosted_scope or {}).get(key) or "").strip()
            if value:
                headers[header_name] = value
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                decoded = json.loads(raw) if raw else {}
                if not isinstance(decoded, dict):
                    raise AgvmMcpHttpError(status=response.status, message="AGVM API returned non-object JSON", payload=decoded)
                return decoded
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                decoded: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                decoded = {"raw": raw}
            raise AgvmMcpHttpError(status=exc.code, message=f"AGVM API HTTP {exc.code} for {path}", payload=decoded) from exc
        except urllib.error.URLError as exc:
            raise AgvmMcpHttpError(status=None, message=f"AGVM API unreachable at {url}: {exc.reason}", payload=None) from exc
        except json.JSONDecodeError as exc:
            raise AgvmMcpHttpError(status=None, message=f"AGVM API returned invalid JSON for {path}", payload=str(exc)) from exc


def _path_with_query(path: str, payload: dict[str, Any]) -> str:
    query_items: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            query_items[str(key)] = json.dumps(value, ensure_ascii=False)
        else:
            query_items[str(key)] = str(value)
    if not query_items:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.urlencode(query_items)}"


def _reconfigure_utf8(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return


class AgvmMcpServer:
    def __init__(self, config: AgvmMcpConfig | None = None, client: AgvmHttpClient | None = None) -> None:
        self.config = config or load_config()
        self.client = client or AgvmHttpClient(self.config)

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}

        if method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return self._success(
                    request_id,
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                )
            if method == "ping":
                return self._success(request_id, {})
            if method == "tools/list":
                return self._success(request_id, self._tools_list())
            if method == "tools/call":
                if not isinstance(params, dict):
                    return self._jsonrpc_error(request_id, -32602, "tools/call params must be an object")
                return self._success(request_id, self._tools_call(params))
            return self._jsonrpc_error(request_id, -32601, f"Unknown MCP method: {method}")
        except AgvmMcpHttpError as exc:
            return self._jsonrpc_error(
                request_id,
                -32002,
                exc.args[0] if exc.args else "AGVM API error",
                {"status": exc.status, "payload": exc.payload},
            )
        except AgvmMcpError as exc:
            return self._jsonrpc_error(request_id, -32001, str(exc))
        except Exception as exc:  # pragma: no cover - defensive boundary for external MCP clients.
            return self._jsonrpc_error(request_id, -32603, f"Internal AGVM MCP server error: {exc}")

    def _tools_list(self) -> dict[str, Any]:
        registry = self.client.get_json("/mcp/contracts", brain_id=self.config.selected_brain_id)
        tools = []
        module_access = self._module_access_summary(registry)
        hidden_module_tool_names: list[str] = []
        for contract in registry.get("tools") or []:
            if not isinstance(contract, dict):
                continue
            name = str(contract.get("name") or "")
            if not name or not self.config.tool_permissions.is_visible(name, permission_family=str(contract.get("permission_family") or "")):
                continue
            if not self._contract_is_callable(contract):
                continue
            module_decision = self._module_access_decision(contract, module_access=module_access)
            if not module_decision["granted"] and self.config.module_access.hides_unlicensed_tools:
                hidden_module_tool_names.append(name)
                continue
            tools.append(self._contract_to_mcp_tool(contract))
        module_access["hidden_tool_names"] = sorted(hidden_module_tool_names)
        return {
            "tools": tools,
            "agvm": {
                "api_base_url": self.config.api_base_url,
                "brain_policy": self.config.brain_policy,
                "selected_brain_id": self.config.selected_brain_id,
                "brain_id_hint": self.config.brain_id_hint,
                "brain_display_name": self.config.brain_display_name,
                "brain_purpose": self.config.brain_purpose,
                "brain_resolution": self._brain_resolution_data(),
                "tenant_id": self.config.tenant_id,
                "user_id": self.config.user_id,
                "environment_id": self.config.environment_id,
                "read_only": self.config.tool_permissions.read_only,
                "contract_registry_schema_version": registry.get("schema_version"),
                "module_tool_registration_state": dict(registry.get("module_tool_registration") or {}).get("state"),
                "module_required_tool_names": list(dict(registry.get("module_tool_registration") or {}).get("module_tool_names") or []),
                "module_access": module_access,
            },
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") or {}
        if not tool_name:
            return self._tool_error("tool_name_required", {"params": params})
        if not isinstance(arguments, dict):
            return self._tool_error("tool_arguments_must_be_object", {"tool_name": tool_name})

        argument_brain_id = str(arguments.get("brain_id") or "").strip() or None
        configured_brain_id = argument_brain_id or (str(self.config.selected_brain_id or "").strip() or None)
        registry_lookup_brain_id = str(self.config.selected_brain_id or "").strip() or None
        hosted_scope = self._hosted_scope_from_arguments(arguments)
        hosted_scope_arg = hosted_scope if self._has_hosted_scope(hosted_scope) else None
        if (
            tool_name not in BRAIN_SCOPE_OPTIONAL_TOOLS
            and not configured_brain_id
            and not (hosted_scope.get("tenant_id") and hosted_scope.get("user_id"))
        ):
            return self._tool_error(
                "brain_id_required_for_ambiguous_local_mcp_scope",
                {
                    "tool_name": tool_name,
                    **self._brain_resolution_data(),
                    "policy": self._brain_missing_policy_text(),
                },
            )
        contract = self._contract_for_tool(tool_name, brain_id=registry_lookup_brain_id, hosted_scope=hosted_scope_arg)
        if not contract:
            return self._tool_error("tool_not_in_agvm_contract_registry", {"tool_name": tool_name})

        module_decision = self._module_access_decision(contract)
        if not module_decision["granted"] and self.config.module_access.blocks_unlicensed_calls:
            action_contract = self._module_access_action_contract(tool_name, module_decision)
            return self._tool_error(
                "module_tool_not_enabled_by_local_mcp_lease",
                {
                    "tool_name": tool_name,
                    "required_module_id": module_decision["required_module_id"],
                    "visibility_policy": self.config.module_access.visibility_policy,
                    "module_status": module_decision["module_status"],
                    "recovery": (
                        "This advanced tool is intentionally visible in the local MCP catalog, "
                        "but execution requires Detwin Cloud, account credits, or an active local Pro module lease. "
                        "Open Detwin Cloud or connect/renew the account, then reconnect the MCP client."
                    ),
                    "action_contract": action_contract,
                },
            )

        requires_brain_id = bool(contract.get("requires_brain_id", True))
        if requires_brain_id and not configured_brain_id and not (hosted_scope.get("tenant_id") and hosted_scope.get("user_id")):
            return self._tool_error(
                "brain_id_required_for_ambiguous_local_mcp_scope",
                {
                    "tool_name": tool_name,
                    **self._brain_resolution_data(),
                    "policy": self._brain_missing_policy_text(),
                },
            )

        can_call, blocked_reason = self.config.tool_permissions.can_call(
            tool_name,
            permission_family=str(contract.get("permission_family") or ""),
        )
        if not can_call:
            return self._tool_error(blocked_reason or "tool_call_blocked", {"tool_name": tool_name})

        arguments = self._arguments_with_brain_policy_defaults(tool_name, arguments)
        validation_error = self._validate_contract_required_arguments(contract, arguments)
        if validation_error:
            return self._tool_error(validation_error["reason"], {"tool_name": tool_name, **validation_error})

        payload = dict(arguments)
        brain_id = configured_brain_id if requires_brain_id else None
        if requires_brain_id and brain_id:
            payload["brain_id"] = brain_id
        endpoint_path = str(contract.get("endpoint_path") or f"/mcp/{tool_name.replace('_', '-')}")
        if brain_id:
            endpoint_path = endpoint_path.replace("{brain_id}", urllib.parse.quote(brain_id, safe=""))
        http_method = str(contract.get("http_method") or "POST").upper()
        result = self.client.request_json(http_method, endpoint_path, payload, brain_id=brain_id, hosted_scope=hosted_scope_arg)
        return self._tool_result(result)

    def _has_hosted_scope(self, hosted_scope: dict[str, str | None] | None) -> bool:
        return any(str(value or "").strip() for value in dict(hosted_scope or {}).values())

    def _hosted_scope_from_arguments(self, arguments: dict[str, Any]) -> dict[str, str | None]:
        return {
            "tenant_id": str(arguments.get("tenant_id") or self.config.tenant_id or "").strip() or None,
            "organization_id": str(arguments.get("organization_id") or self.config.organization_id or "").strip() or None,
            "user_id": str(arguments.get("user_id") or self.config.user_id or "").strip() or None,
            "environment_id": str(arguments.get("environment_id") or self.config.environment_id or "").strip() or None,
        }

    def _contract_registry(self, *, brain_id: str | None, hosted_scope: dict[str, str | None] | None = None) -> dict[str, Any]:
        if self._has_hosted_scope(hosted_scope):
            return self.client.get_json("/mcp/contracts", brain_id=brain_id, hosted_scope=hosted_scope)
        return self.client.get_json("/mcp/contracts", brain_id=brain_id)

    def _contract_for_tool(
        self,
        tool_name: str,
        *,
        brain_id: str | None,
        hosted_scope: dict[str, str | None] | None = None,
    ) -> dict[str, Any] | None:
        registry = self._contract_registry(brain_id=brain_id, hosted_scope=hosted_scope)
        for tool in registry.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            if str(tool.get("name") or "") != tool_name:
                continue
            if not self._contract_is_callable(tool):
                return None
            return dict(tool)
        return None

    def _contract_tool_names(self, *, brain_id: str | None, hosted_scope: dict[str, str | None] | None = None) -> set[str]:
        registry = self._contract_registry(brain_id=brain_id, hosted_scope=hosted_scope)
        return {
            str(tool.get("name") or "")
            for tool in registry.get("tools") or []
            if isinstance(tool, dict) and self._contract_is_callable(tool)
        }

    def _contract_is_callable(self, contract: dict[str, Any]) -> bool:
        backend_binding = dict(contract.get("backend_binding") or {})
        if (
            str(contract.get("implementation_status") or "") == "implemented"
            and backend_binding.get("executable") is True
            and str(contract.get("endpoint_path") or "")
        ):
            return True
        return (
            str(contract.get("implementation_status") or "") == "implemented"
            and str(backend_binding.get("binding_state") or "") == "implemented"
        )

    def _module_access_summary(self, registry: dict[str, Any]) -> dict[str, Any]:
        required_module_ids = sorted(
            {
                module_id
                for tool in registry.get("tools") or []
                if isinstance(tool, dict)
                for module_id in [self._required_module_id(tool)]
                if module_id
            }
        )
        statuses = {
            module_id: self.config.module_access.status_for_module(module_id)
            for module_id in required_module_ids
        }
        if self.config.module_access.enabled:
            granted = sorted(module_id for module_id, status in statuses.items() if bool(status.get("granted")))
            blocked = sorted(module_id for module_id in required_module_ids if module_id not in granted)
            not_enforced: list[str] = []
        else:
            granted = []
            blocked = []
            not_enforced = list(required_module_ids)
        return {
            "schema_version": "agvm.local_mcp_module_access.v1",
            "visibility_policy": self.config.module_access.visibility_policy,
            "status_source": self.config.module_access.status_source,
            "enforced": self.config.module_access.enabled,
            "license_state_path_configured": bool(self.config.module_access.license_state_path),
            "required_module_ids": required_module_ids,
            "granted_module_ids": granted,
            "blocked_module_ids": blocked,
            "not_enforced_module_ids": not_enforced,
            "hidden_tool_names": [],
            "module_statuses": statuses,
        }

    def _module_access_decision(self, contract: dict[str, Any], *, module_access: dict[str, Any] | None = None) -> dict[str, Any]:
        required_module_id = self._required_module_id(contract)
        if not required_module_id:
            return {
                "required_module_id": None,
                "granted": True,
                "module_status": self.config.module_access.status_for_module(""),
            }
        statuses = dict(dict(module_access or {}).get("module_statuses") or {})
        status = dict(statuses.get(required_module_id) or self.config.module_access.status_for_module(required_module_id))
        return {
            "required_module_id": required_module_id,
            "granted": bool(status.get("granted")),
            "module_status": status,
        }

    def _module_access_action_contract(self, tool_name: str, module_decision: dict[str, Any]) -> dict[str, Any]:
        platform_url = str(os.environ.get("AGVM_DETWIN_PLATFORM_URL") or os.environ.get("AGVM_PLATFORM_PUBLIC_BASE_URL") or DEFAULT_DETWIN_PLATFORM_URL).rstrip("/")
        cloud_url = str(os.environ.get("AGVM_DETWIN_CLOUD_URL") or os.environ.get("AGVM_CLOUD_APP_PUBLIC_BASE_URL") or DEFAULT_DETWIN_CLOUD_URL).rstrip("/")
        required_module_id = str(module_decision.get("required_module_id") or "").strip()
        return {
            "schema_version": "agvm.local_mcp_paid_tool_action.v1",
            "action": "use_detwin_cloud_for_advanced_tool",
            "tool_name": tool_name,
            "required_module_id": required_module_id or None,
            "requires_account": True,
            "requires_credits": True,
            "requires_cloud_handoff": True,
            "platform_account_url": f"{platform_url}/account/modules",
            "platform_billing_url": f"{platform_url}/account/billing",
            "cloud_workspace_url": f"{cloud_url}/?runtime=cloud&route=modules",
            "client_message": (
                "Keep the tool visible for planning. Before executing it, open Detwin Cloud "
                "or connect a paid Detwin account/local Pro lease with enough credits."
            ),
        }

    def _required_module_id(self, contract: dict[str, Any]) -> str | None:
        tool_registration = dict(contract.get("tool_registration") or {})
        required_module_id = str(tool_registration.get("required_module_id") or "").strip()
        return required_module_id or None

    def _arguments_with_brain_policy_defaults(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "ensure_brain" or not self.config.requires_ai_brain_resolution:
            return arguments
        payload = dict(arguments)
        payload.setdefault("activation_policy", "return_only")
        if self.config.brain_id_hint and not str(payload.get("brain_id") or "").strip():
            payload["brain_id"] = self.config.brain_id_hint
        if self.config.brain_display_name and not str(payload.get("display_name") or "").strip():
            payload["display_name"] = self.config.brain_display_name
        if self.config.brain_purpose and not str(payload.get("purpose") or "").strip():
            payload["purpose"] = self.config.brain_purpose
        if "create_if_missing" not in payload:
            payload["create_if_missing"] = self.config.brain_policy == BRAIN_POLICY_AI_CREATE_IF_MISSING
        return payload

    def _brain_resolution_data(self) -> dict[str, Any]:
        return {
            "brain_policy": self.config.brain_policy,
            "selected_brain_id": self.config.selected_brain_id,
            "configured_active_brain_id": self.config.active_brain_id,
            "configured_default_brain_id": self.config.default_brain_id,
            "brain_id_hint": self.config.brain_id_hint,
            "brain_display_name": self.config.brain_display_name,
            "brain_purpose": self.config.brain_purpose,
            "requires_explicit_brain_id": self.config.requires_ai_brain_resolution,
            "recommended_sequence": self._brain_resolution_sequence(),
        }

    def _brain_resolution_sequence(self) -> list[str]:
        if self.config.brain_policy == BRAIN_POLICY_AI_CREATE_IF_MISSING:
            return [
                "call get_agvm_usage_guide",
                "call ensure_brain with create_if_missing=true and activation_policy=return_only",
                "copy the returned brain_id into every scoped memory tool call",
            ]
        if self.config.brain_policy == BRAIN_POLICY_AI_RESOLVE_EXISTING:
            return [
                "call get_agvm_usage_guide",
                "call list_brains",
                "call ensure_brain with create_if_missing=false and activation_policy=return_only, or choose an existing brain_id from list_brains",
                "copy the returned or chosen brain_id into every scoped memory tool call",
            ]
        return [
            "call get_agvm_usage_guide",
            "omit brain_id to use the configured fixed brain, or pass brain_id to override intentionally",
        ]

    def _brain_missing_policy_text(self) -> str:
        if self.config.brain_policy == BRAIN_POLICY_AI_CREATE_IF_MISSING:
            return (
                "No implicit local brain is injected. First call ensure_brain with create_if_missing=true "
                "and activation_policy=return_only, then pass the returned brain_id to this tool."
            )
        if self.config.brain_policy == BRAIN_POLICY_AI_RESOLVE_EXISTING:
            return (
                "No implicit local brain is injected. First call list_brains, then call ensure_brain "
                "with create_if_missing=false or choose an existing brain_id, then pass that brain_id to this tool."
            )
        return (
            "Set active_brain_id/default_brain_id or AGVM_MCP_BRAIN_ID in the local MCP config, "
            "pass brain_id explicitly, or configure tenant_id/user_id for hosted default-brain resolution."
        )

    def _brain_id_schema_description(self) -> str:
        if self.config.brain_policy == BRAIN_POLICY_AI_CREATE_IF_MISSING:
            return (
                "Required after onboarding. This MCP config does not inject a local default brain; "
                "first call ensure_brain with create_if_missing=true and activation_policy=return_only, "
                "then pass the returned brain_id here."
            )
        if self.config.brain_policy == BRAIN_POLICY_AI_RESOLVE_EXISTING:
            return (
                "Required after resolution. This MCP config does not inject a local default brain; "
                "first call list_brains or ensure_brain with create_if_missing=false, then pass the chosen brain_id here."
            )
        selected = self.config.selected_brain_id or "<unset>"
        return f"Optional local brain id. If omitted, the fixed MCP config brain is used: {selected}."

    def _brain_policy_tool_text(self, tool_name: str, *, requires_brain_id: bool) -> str:
        if self.config.brain_policy == BRAIN_POLICY_FIXED:
            if requires_brain_id:
                selected = self.config.selected_brain_id or "<unset>"
                return f"Brain policy: fixed; omit brain_id to use {selected}, or pass brain_id only for an intentional override."
            return "Brain policy: fixed local config; this tool is global and does not require brain_id."
        if tool_name == "ensure_brain":
            if self.config.brain_policy == BRAIN_POLICY_AI_CREATE_IF_MISSING:
                return (
                    "Brain policy: AI may create if missing; call this before scoped memory tools. "
                    "If omitted, create_if_missing defaults to true and activation_policy defaults to return_only."
                )
            return (
                "Brain policy: AI must resolve an existing brain; call this with create_if_missing=false "
                "or use list_brains, then reuse the returned brain_id."
            )
        if tool_name == "list_brains":
            return "Brain policy: use this to inspect available brains before choosing a brain_id for scoped tools."
        if requires_brain_id:
            return (
                "Brain policy: no UI active brain is injected. Pass a concrete brain_id returned by list_brains "
                "or ensure_brain."
            )
        return "Brain policy: global tool; use it before resolving a brain."

    def _contract_to_mcp_tool(self, contract: dict[str, Any]) -> dict[str, Any]:
        name = str(contract.get("name") or "")
        schema = self._input_schema_with_brain(
            contract.get("input_schema") or {},
            requires_brain_id=bool(contract.get("requires_brain_id", True)),
        )
        safety_contract = dict(contract.get("safety_contract") or {})
        client_usage = dict(contract.get("client_usage") or {})
        tool_registration = dict(contract.get("tool_registration") or {})
        mutation_policy = str(safety_contract.get("mutation_policy") or client_usage.get("mutation_policy") or "read_only")
        required_module = str(tool_registration.get("required_module_id") or "").strip()
        description_parts = [
            str(contract.get("description") or "").strip(),
            f"Default output package: {contract.get('default_output_package')}.",
            f"Mutation policy: {mutation_policy}.",
            f"Endpoint: {contract.get('http_method', 'POST')} {contract.get('endpoint_path')}.",
            f"Module entitlement required: {required_module}." if required_module else "",
            self._brain_policy_tool_text(name, requires_brain_id=bool(contract.get("requires_brain_id", True))),
            str(client_usage.get("when_to_use") or "").strip(),
            self._client_usage_text(client_usage),
            "AGVM returns JSON-first MCP context packages; answer demos are secondary.",
        ]
        return {
            "name": name,
            "description": " ".join(part for part in description_parts if part),
            "inputSchema": schema,
        }

    def _input_schema_with_brain(self, schema: dict[str, Any], *, requires_brain_id: bool = True) -> dict[str, Any]:
        schema_copy = dict(schema) if isinstance(schema, dict) else {"type": "object", "properties": {}}
        schema_copy.setdefault("type", "object")
        schema_copy.setdefault("properties", {})
        properties = dict(schema_copy.get("properties") or {})
        if requires_brain_id:
            properties.setdefault(
                "brain_id",
                {
                    "type": ["string", "null"],
                    "description": self._brain_id_schema_description(),
                },
            )
        properties.setdefault(
            "tenant_id",
            {
                "type": ["string", "null"],
                "description": "Optional hosted tenant id. Prefer MCP config/env so every call stays under one tenant scope.",
            },
        )
        properties.setdefault(
            "organization_id",
            {
                "type": ["string", "null"],
                "description": "Optional hosted organization id. Prefer MCP config/env so every call stays under one organization scope.",
            },
        )
        properties.setdefault(
            "user_id",
            {
                "type": ["string", "null"],
                "description": "Optional hosted user id. Prefer MCP config/env so every call stays under one user scope.",
            },
        )
        properties.setdefault(
            "environment_id",
            {
                "type": ["string", "null"],
                "description": "Optional hosted environment id such as local_self_hosted_dev, staging or production.",
            },
        )
        schema_copy["properties"] = properties
        return schema_copy

    def _client_usage_text(self, client_usage: dict[str, Any]) -> str:
        text_parts: list[str] = []
        for key, label in (
            ("query_guidance", "Query guidance"),
            ("input_strategy", "Input strategy"),
            ("result_handling", "Result handling"),
        ):
            value = client_usage.get(key)
            if isinstance(value, list):
                joined = " ".join(str(item).strip() for item in value if str(item).strip())
                if joined:
                    text_parts.append(f"{label}: {joined}")
            elif str(value or "").strip():
                text_parts.append(f"{label}: {str(value).strip()}")
        followups = client_usage.get("followups")
        if isinstance(followups, list) and followups:
            text_parts.append("Follow-up tools: " + ", ".join(str(item) for item in followups if str(item).strip()))
        return " ".join(text_parts)

    def _validate_contract_required_arguments(self, contract: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any] | None:
        input_schema = dict(contract.get("input_schema") or {})
        required = [str(item) for item in list(input_schema.get("required") or [])]
        missing = [
            name
            for name in required
            if name not in arguments or arguments.get(name) is None or (isinstance(arguments.get(name), str) and not str(arguments.get(name)).strip())
        ]
        if missing:
            return {"reason": "tool_required_arguments_missing", "missing_arguments": missing}
        return None

    def _tool_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
            "structuredContent": payload,
            "isError": False,
        }

    def _tool_error(self, reason: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"status": "blocked", "reason": reason, "data": data or {}}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
            "structuredContent": payload,
            "isError": True,
        }

    def _success(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}

    def _jsonrpc_error(self, request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _handle_jsonrpc_payload(server: AgvmMcpServer, payload: Any) -> Any:
    if isinstance(payload, list):
        responses = [response for item in payload if isinstance(item, dict) for response in [server.handle_message(item)] if response is not None]
        return responses if responses else None
    if not isinstance(payload, dict):
        return {"jsonrpc": JSONRPC_VERSION, "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC payload"}}
    return server.handle_message(payload)


def run_stdio(server: AgvmMcpServer | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    active_server = server or AgvmMcpServer()
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    _reconfigure_utf8(input_stream)
    _reconfigure_utf8(output_stream)
    for line in input_stream:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": JSONRPC_VERSION, "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
        else:
            response = _handle_jsonrpc_payload(active_server, payload)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    _ = argv or sys.argv[1:]
    try:
        return run_stdio()
    except AgvmMcpError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
