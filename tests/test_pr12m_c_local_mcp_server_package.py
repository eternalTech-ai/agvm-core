# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from agvm_mcp_server.server import (  # noqa: E402
    AgvmHttpClient,
    AgvmMcpConfig,
    AgvmMcpHttpError,
    AgvmMcpServer,
    ToolPermissions,
    load_config,
    run_stdio,
)
from mcp_contracts import AGENT_MEMORY_MCP_TOOL_NAMES, GUIDE_MCP_TOOL_NAMES, REQUIRED_MCP_TOOL_NAMES, build_mcp_contract_registry  # noqa: E402


REPORT = ROOT / "docs" / "AGVM_PROGRESS.md"
SPEC = ROOT / "docs" / "AGVM_SLICES.md"
INDEX = ROOT / "docs" / "AGVM_MASTER.md"
MASTER = ROOT / "docs" / "AGVM_MASTER.md"
ROADMAP = ROOT / "docs" / "AGVM_SLICES.md"
README = ROOT / "README.md"
MANIFEST = ROOT / "agvm_mcp_server" / "manifest.json"
CONFIG_EXAMPLE = ROOT / "agvm_mcp_server" / "config.example.json"
GITIGNORE = ROOT / ".gitignore"


class _FakeHostedMcpClient:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or {
            "content": [{"type": "text", "text": "Hosted operation settled."}],
            "isError": False,
            "structuredContent": {
                "status": "preview_ready",
                "metering": {
                    "mode": "estimate_then_settle",
                    "estimated_credit_units": 180,
                    "settled_credit_units": 193,
                },
            },
        }

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        request_id: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "request_id": request_id,
            }
        )
        return self.result


def _stub_api() -> tuple[ThreadingHTTPServer, type[BaseHTTPRequestHandler], str]:
    registry = build_mcp_contract_registry()

    class Handler(BaseHTTPRequestHandler):
        get_requests: list[dict[str, Any]] = []
        post_requests: list[dict[str, Any]] = []

        def do_GET(self) -> None:  # noqa: N802
            type(self).get_requests.append({"path": self.path, "brain_header": self.headers.get("X-AGVM-Brain-Id")})
            if self.path == "/mcp/contracts":
                self._send_json(registry)
                return
            if self.path == "/mcp/usage-guide":
                self._send_json(
                    {
                        "schema_version": "agvm.mcp_usage_guide.v1",
                        "guide_name": "AGVM MCP Agent Memory Usage Guide",
                        "markdown_guide": "# AGVM MCP Usage Guide\nCall list_brains, ensure_brain, then retrieve_context.",
                        "policy": {"retrieval": {"query_text": "concrete information need"}},
                        "recommended_flow": ["get_agvm_usage_guide", "list_brains", "ensure_brain", "retrieve_context"],
                        "query_recipes": {"normal_recall": {"tool": "retrieve_context"}},
                        "tool_map": {"guide": ["get_agvm_usage_guide"]},
                        "first_call": {"tool": "get_agvm_usage_guide", "requires_brain_id": False},
                    }
                )
                return
            if self.path == "/mcp/brains":
                self._send_json(
                    {
                        "schema_version": "agvm.local_brain_registry.v1",
                        "brain_count": 1,
                        "active_brain_id": "alpha_brain",
                        "default_brain_id": "alpha_brain",
                        "brains": [{"brain_id": "alpha_brain", "display_name": "Alpha Brain"}],
                        "validation": {"passed": True},
                    }
                )
                return
            if self.path == "/mcp/brains/active":
                self._send_json(
                    {
                        "schema_version": "agvm.local_active_brain_summary.v1",
                        "brain_id": "alpha_brain",
                        "display_name": "Alpha Brain",
                        "safe_for_mcp": True,
                    }
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw) if raw else {}
            type(self).post_requests.append(
                {
                    "path": self.path,
                    "brain_header": self.headers.get("X-AGVM-Brain-Id"),
                    "payload": payload,
                }
            )
            self._send_json(
                {
                    "schema_version": "agvm.stub_mcp_output.v1",
                    "tool_name": self.path.removeprefix("/mcp/").replace("-", "_"),
                    "status": "ok",
                    "brain_id": payload.get("brain_id"),
                    "echo": payload,
                    "context_package": {"agent_markdown": "stub context"},
                    "completeness": {"status": "stub"},
                    "budget": {"status": "stub"},
                }
            )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, Handler, f"http://127.0.0.1:{server.server_address[1]}"


def _shutdown(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def test_pr12m_c_tools_list_is_mcp_protocol_registry_projection_with_brain_scope() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(api_base_url=base_url, active_brain_id="alpha_brain", default_brain_id="alpha_brain")
        )
        response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        _shutdown(http_server)

    assert response is not None
    result = response["result"]
    tools = result["tools"]
    assert len(tools) == len(GUIDE_MCP_TOOL_NAMES) + len(REQUIRED_MCP_TOOL_NAMES) + len(AGENT_MEMORY_MCP_TOOL_NAMES)
    assert [tool["name"] for tool in tools] == [*GUIDE_MCP_TOOL_NAMES, *REQUIRED_MCP_TOOL_NAMES, *AGENT_MEMORY_MCP_TOOL_NAMES]
    for name in GUIDE_MCP_TOOL_NAMES:
        assert name in {tool["name"] for tool in tools}
    for name in AGENT_MEMORY_MCP_TOOL_NAMES:
        assert name in {tool["name"] for tool in tools}
    assert result["agvm"]["selected_brain_id"] == "alpha_brain"
    assert result["agvm"]["contract_registry_schema_version"] == "agvm.mcp_contract_registry.v1"
    assert handler.get_requests == [{"path": "/mcp/contracts", "brain_header": "alpha_brain"}]
    assert all("brain_id" in tool["inputSchema"]["properties"] for tool in tools if tool["name"] in REQUIRED_MCP_TOOL_NAMES)
    assert "brain_id" not in next(tool for tool in tools if tool["name"] == "list_brains")["inputSchema"]["properties"]
    assert "brain_id" not in next(tool for tool in tools if tool["name"] == "get_agvm_usage_guide")["inputSchema"]["properties"]
    assert all("AGVM returns JSON-first MCP context packages" in tool["description"] for tool in tools)
    retrieve_context = next(tool for tool in tools if tool["name"] == "retrieve_context")
    assert "Query guidance:" in retrieve_context["description"]
    assert "query_text" in retrieve_context["description"]


def test_pr12m_c_tools_call_injects_brain_id_and_mcp_header() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(api_base_url=base_url, active_brain_id="simone_massaro", default_brain_id="simone_massaro")
        )
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "retrieve_context", "arguments": {"query_text": "raccontami del lavoro"}},
            }
        )
    finally:
        _shutdown(http_server)

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["tool_name"] == "retrieve_context"
    assert result["structuredContent"]["brain_id"] == "simone_massaro"
    assert handler.post_requests == [
        {
            "path": "/mcp/retrieve-context",
            "brain_header": "simone_massaro",
            "payload": {"query_text": "raccontami del lavoro", "brain_id": "simone_massaro"},
        }
    ]


def test_pr12m_c_load_config_supports_ai_managed_brain_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "api_base_url": "http://127.0.0.1:8010",
                "active_brain_id": "ui_active_brain",
                "default_brain_id": "ui_default_brain",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGVM_MCP_BRAIN_POLICY", "ai_create_if_missing")
    monkeypatch.setenv("AGVM_MCP_BRAIN_ID_HINT", "codex_project_memory")
    monkeypatch.setenv("AGVM_MCP_BRAIN_DISPLAY_NAME", "Codex Project Memory")
    monkeypatch.setenv("AGVM_MCP_BRAIN_PURPOSE", "Test MCP-scoped memory independent from UI active brain")

    loaded = load_config(config_path)

    assert loaded.brain_policy == "ai_create_if_missing"
    assert loaded.active_brain_id == "ui_active_brain"
    assert loaded.default_brain_id == "ui_default_brain"
    assert loaded.selected_brain_id is None
    assert loaded.brain_id_hint == "codex_project_memory"
    assert loaded.brain_display_name == "Codex Project Memory"
    assert loaded.brain_purpose == "Test MCP-scoped memory independent from UI active brain"


def test_pr12m_c_ai_create_policy_prefills_ensure_brain_defaults() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                brain_policy="ai_create_if_missing",
                brain_id_hint="codex_project_memory",
                brain_display_name="Codex Project Memory",
                brain_purpose="MCP-local test memory",
            )
        )
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "ensure-create",
                "method": "tools/call",
                "params": {"name": "ensure_brain", "arguments": {}},
            }
        )
    finally:
        _shutdown(http_server)

    assert response is not None
    assert response["result"]["isError"] is False
    assert handler.post_requests[-1] == {
        "path": "/mcp/brains/ensure",
        "brain_header": None,
        "payload": {
            "activation_policy": "return_only",
            "brain_id": "codex_project_memory",
            "display_name": "Codex Project Memory",
            "purpose": "MCP-local test memory",
            "create_if_missing": True,
        },
    }


def test_pr12m_c_ai_resolve_policy_prefills_existing_brain_guard() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                brain_policy="ai_resolve_existing",
                brain_display_name="Existing Project Memory",
            )
        )
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "ensure-existing",
                "method": "tools/call",
                "params": {"name": "ensure_brain", "arguments": {}},
            }
        )
    finally:
        _shutdown(http_server)

    assert response is not None
    assert response["result"]["isError"] is False
    assert handler.post_requests[-1] == {
        "path": "/mcp/brains/ensure",
        "brain_header": None,
        "payload": {
            "activation_policy": "return_only",
            "display_name": "Existing Project Memory",
            "create_if_missing": False,
        },
    }


def test_pr12m_c_ai_brain_policy_does_not_inject_ui_active_brain() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                active_brain_id="ui_active_brain",
                default_brain_id="ui_default_brain",
                brain_policy="ai_resolve_existing",
                brain_display_name="Existing Project Memory",
            )
        )
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "retrieve-without-brain",
                "method": "tools/call",
                "params": {"name": "retrieve_context", "arguments": {"query_text": "project state"}},
            }
        )
    finally:
        _shutdown(http_server)

    assert response is not None
    assert response["result"]["isError"] is True
    payload = response["result"]["structuredContent"]
    assert payload["reason"] == "brain_id_required_for_ambiguous_local_mcp_scope"
    assert payload["data"]["brain_policy"] == "ai_resolve_existing"
    assert payload["data"]["selected_brain_id"] is None
    assert payload["data"]["configured_active_brain_id"] == "ui_active_brain"
    assert handler.post_requests == []


def test_pr12m_c_tools_call_uses_contract_endpoint_method_and_registry_scope() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(AgvmMcpConfig(api_base_url=base_url))
        listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        listed_names = [tool["name"] for tool in listed["result"]["tools"]]  # type: ignore[index]
        guide_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_agvm_usage_guide", "arguments": {}},
            }
        )
        list_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_brains", "arguments": {}},
            }
        )
        ensure_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ensure_brain",
                    "arguments": {"brain_id": "codex_memory", "display_name": "Codex Memory"},
                },
            }
        )
        create_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "create_brain",
                    "arguments": {
                        "brain_id": "project_memory",
                        "display_name": "Project Memory",
                        "make_active": False,
                        "make_default": False,
                    },
                },
            }
        )
    finally:
        _shutdown(http_server)

    assert "list_brains" in listed_names
    assert "active_brain" in listed_names
    assert guide_response is not None
    assert guide_response["result"]["isError"] is False
    assert guide_response["result"]["structuredContent"]["recommended_flow"][0] == "get_agvm_usage_guide"
    assert list_response is not None
    assert list_response["result"]["isError"] is False
    assert list_response["result"]["structuredContent"]["brain_count"] == 1
    assert ensure_response is not None
    assert ensure_response["result"]["isError"] is False
    assert create_response is not None
    assert create_response["result"]["isError"] is False
    expected_get_requests = [
        {"path": "/mcp/contracts", "brain_header": None},
        {"path": "/mcp/contracts", "brain_header": None},
        {"path": "/mcp/usage-guide", "brain_header": None},
        {"path": "/mcp/contracts", "brain_header": None},
        {"path": "/mcp/brains", "brain_header": None},
        {"path": "/mcp/contracts", "brain_header": None},
        {"path": "/mcp/contracts", "brain_header": None},
    ]
    observed_get_requests = iter(handler.get_requests)
    assert all(any(observed == expected for observed in observed_get_requests) for expected in expected_get_requests)
    assert all(observed in expected_get_requests for observed in handler.get_requests)
    assert handler.post_requests[-2] == {
        "path": "/mcp/brains/ensure",
        "brain_header": None,
        "payload": {"brain_id": "codex_memory", "display_name": "Codex Memory"},
    }
    assert handler.post_requests[-1] == {
        "path": "/mcp/brains/create",
        "brain_header": None,
        "payload": {
            "brain_id": "project_memory",
            "display_name": "Project Memory",
            "make_active": False,
            "make_default": False,
        },
    }


def test_pr12m_c_http_client_retries_only_idempotent_transport_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def abort_first_get(request: Any, *, timeout: float) -> Response:
        del timeout
        calls.append(request.get_method())
        if len(calls) == 1:
            raise ConnectionAbortedError(10053, "host software aborted the connection")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", abort_first_get)
    client = AgvmHttpClient(AgvmMcpConfig(api_base_url="http://127.0.0.1:1"))

    assert client.get_json("/mcp/contracts") == {"status": "ok"}
    assert calls == ["GET", "GET"]

    calls.clear()

    def abort_post(request: Any, *, timeout: float) -> Response:
        del timeout
        calls.append(request.get_method())
        raise ConnectionAbortedError(10053, "host software aborted the connection")

    monkeypatch.setattr("urllib.request.urlopen", abort_post)
    with pytest.raises(AgvmMcpHttpError, match="AGVM API transport failed"):
        client.post_json("/mcp/brains/create", {"brain_id": "must_not_retry"})
    assert calls == ["POST"]


def test_pr12m_c_read_only_config_lists_and_blocks_mutation_tools() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                active_brain_id="alpha_brain",
                tool_permissions=ToolPermissions(read_only=True),
            )
        )
        listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "write_memory_commit", "arguments": {"text": "should not persist"}},
            }
        )
    finally:
        _shutdown(http_server)

    listed_names = [tool["name"] for tool in listed["result"]["tools"]]  # type: ignore[index]
    assert "write_memory_commit" in listed_names
    assert "grow_source_apply" in listed_names
    assert "matrix_calibration_apply" in listed_names
    assert "list_brains" in listed_names
    assert "active_brain" in listed_names
    assert "get_agvm_usage_guide" in listed_names
    assert "create_brain" in listed_names
    assert "select_brain" in listed_names
    assert "ensure_brain" in listed_names
    assert called is not None
    assert called["result"]["isError"] is True
    assert called["result"]["structuredContent"]["reason"] == "mutation_tool_blocked_by_read_only_local_mcp_config"
    assert handler.post_requests == []


def test_pr12m_c_default_module_policy_lists_paid_tools_but_blocks_unlicensed_calls() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                active_brain_id="alpha_brain",
            )
        )
        listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "sleep_preview", "arguments": {"max_nodes_considered": 20}},
            }
        )
    finally:
        _shutdown(http_server)

    listed_names = [tool["name"] for tool in listed["result"]["tools"]]  # type: ignore[index]
    assert "sleep_preview" in listed_names
    assert "evolve_preview" in listed_names
    assert "geometry_calibration_preview" in listed_names
    assert "geometry_calibration_apply" in listed_names
    assert "geometry_calibration_rollback" in listed_names
    assert "matrix_calibration_preview" in listed_names
    assert called is not None
    assert called["result"]["isError"] is True
    payload = called["result"]["structuredContent"]
    assert payload["reason"] == "detwin_cloud_auth_required"
    assert payload["data"]["required_module_id"] == "agvm_maintain_studio"
    action_contract = payload["data"]["action_contract"]
    assert action_contract["schema_version"] == "agvm.local_mcp_paid_tool_action.v1"
    assert action_contract["action"] == "use_detwin_cloud_for_advanced_tool"
    assert action_contract["requires_account"] is True
    assert action_contract["requires_credits"] is True
    assert action_contract["execution_surface"] == "hosted_mcp"
    assert action_contract["dynamic_usage_settlement"] is True
    assert action_contract["credential_environment_variable"] == "AGVM_HOSTED_MCP_API_KEY"
    assert action_contract["cloud_workspace_url"].startswith("https://cloud.detwin.ai/")
    assert "Detwin Cloud" in payload["data"]["recovery"]
    assert handler.post_requests == []


def test_pr12m_c_stdio_lists_and_cloud_blocks_canonical_geometry_tools() -> None:
    http_server, handler, base_url = _stub_api()
    input_messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "geometry_calibration_preview",
                "arguments": {"max_nodes_considered": 50, "max_position_updates": 10},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "geometry_calibration_apply",
                "arguments": {
                    "max_nodes_considered": 50,
                    "max_position_updates": 10,
                    "confirm_apply": True,
                    "rollback_consent": True,
                    "preview_signature": "geometry-preview::test",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "geometry_calibration_rollback",
                "arguments": {
                    "plan_signature": "geometry-preview::test",
                    "confirm_rollback": True,
                },
            },
        },
    ]
    stdin = StringIO("".join(json.dumps(message) + "\n" for message in input_messages))
    stdout = StringIO()
    try:
        exit_code = run_stdio(
            AgvmMcpServer(
                AgvmMcpConfig(api_base_url=base_url, active_brain_id="alpha_brain")
            ),
            stdin=stdin,
            stdout=stdout,
        )
    finally:
        _shutdown(http_server)

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    listed_names = [tool["name"] for tool in responses[0]["result"]["tools"]]
    assert exit_code == 0
    assert len(listed_names) == 52
    assert {
        "geometry_calibration_preview",
        "geometry_calibration_apply",
        "geometry_calibration_rollback",
    }.issubset(listed_names)
    for response in responses[1:]:
        result = response["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["reason"] == "detwin_cloud_auth_required"
        action = result["structuredContent"]["data"]["action_contract"]
        assert action["action"] == "use_detwin_cloud_for_advanced_tool"
        assert action["execution_surface"] == "hosted_mcp"
    assert handler.post_requests == []


def test_pr12m_c_paid_tool_uses_hosted_mcp_while_core_grow_stays_local() -> None:
    http_server, handler, base_url = _stub_api()
    hosted = _FakeHostedMcpClient()
    try:
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                active_brain_id="alpha_brain",
                hosted_mcp_api_key="agvm_mcp_test_secret_not_logged",
            ),
            hosted_client=hosted,  # type: ignore[arg-type]
        )
        grow = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "grow-local",
                "method": "tools/call",
                "params": {
                    "name": "grow_source_preview",
                    "arguments": {"raw_input": "Grow remains a local Core operation."},
                },
            }
        )
        sleep = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "sleep-hosted",
                "method": "tools/call",
                "params": {
                    "name": "sleep_preview",
                    "arguments": {"max_nodes_considered": 20},
                },
            }
        )
    finally:
        _shutdown(http_server)

    assert grow is not None and grow["result"]["isError"] is False
    assert sleep is not None and sleep["result"]["isError"] is False
    assert handler.post_requests == [
        {
            "path": "/mcp/grow-source-preview",
            "brain_header": "alpha_brain",
            "payload": {
                "raw_input": "Grow remains a local Core operation.",
                "brain_id": "alpha_brain",
            },
        }
    ]
    assert hosted.calls == [
        {
            "tool_name": "sleep_preview",
            "arguments": {"max_nodes_considered": 20, "brain_id": "alpha_brain"},
            "request_id": "sleep-hosted",
        }
    ]
    metering = sleep["result"]["structuredContent"]["metering"]
    assert metering["estimated_credit_units"] == 180
    assert metering["settled_credit_units"] == 193


def test_pr12m_c_stdio_proxies_canonical_geometry_preview_to_hosted_mcp() -> None:
    http_server, handler, base_url = _stub_api()
    hosted = _FakeHostedMcpClient()
    stdin = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "geometry-hosted-stdio",
                "method": "tools/call",
                "params": {
                    "name": "geometry_calibration_preview",
                    "arguments": {
                        "brain_id": "alpha_brain",
                        "max_nodes_considered": 50,
                        "max_position_updates": 10,
                    },
                },
            }
        )
        + "\n"
    )
    stdout = StringIO()
    try:
        exit_code = run_stdio(
            AgvmMcpServer(
                AgvmMcpConfig(
                    api_base_url=base_url,
                    active_brain_id="alpha_brain",
                    hosted_mcp_api_key="agvm_mcp_test_secret_not_logged",
                ),
                hosted_client=hosted,  # type: ignore[arg-type]
            ),
            stdin=stdin,
            stdout=stdout,
        )
    finally:
        _shutdown(http_server)

    response = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["status"] == "preview_ready"
    assert hosted.calls == [
        {
            "tool_name": "geometry_calibration_preview",
            "arguments": {
                "brain_id": "alpha_brain",
                "max_nodes_considered": 50,
                "max_position_updates": 10,
            },
            "request_id": "geometry-hosted-stdio",
        }
    ]
    assert handler.post_requests == []


def test_pr12m_c_hosted_mcp_secret_is_environment_only_and_redacted_from_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGVM_HOSTED_MCP_API_KEY", "agvm_mcp_never_render_this_secret")

    config = load_config()

    assert config.hosted_mcp_configured is True
    assert "agvm_mcp_never_render_this_secret" not in repr(config)


def test_pr12m_c_permission_families_allow_registry_and_preview_without_apply() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        permissions = ToolPermissions(
            allowed_permission_families=("read_only", "read_only_export", "registry_write", "preview_only"),
            blocked_permission_families=("explicit_apply", "destructive"),
        )
        server = AgvmMcpServer(
            AgvmMcpConfig(
                api_base_url=base_url,
                active_brain_id="alpha_brain",
                tool_permissions=permissions,
            )
        )
        listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        ensure_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ensure_brain", "arguments": {"brain_id": "agent_memory", "display_name": "Agent Memory"}},
            }
        )
        preview_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "grow_source_preview", "arguments": {"raw_input": "preview this source"}},
            }
        )
        apply_response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "write_memory_commit", "arguments": {"text": "do not apply"}},
            }
        )
    finally:
        _shutdown(http_server)

    listed_names = [tool["name"] for tool in listed["result"]["tools"]]  # type: ignore[index]
    assert "ensure_brain" in listed_names
    assert "create_brain" in listed_names
    assert "grow_source_preview" in listed_names
    assert "write_memory_commit" in listed_names
    assert "grow_apply" in listed_names
    assert ensure_response is not None
    assert ensure_response["result"]["isError"] is False
    assert preview_response is not None
    assert preview_response["result"]["isError"] is False
    assert apply_response is not None
    assert apply_response["result"]["isError"] is True
    assert apply_response["result"]["structuredContent"]["reason"] == "permission_family_blocked_by_local_mcp_config"
    assert handler.post_requests[-2]["path"] == "/mcp/brains/ensure"
    assert handler.post_requests[-1]["path"] == "/mcp/grow-source-preview"


def test_pr12m_c_destructive_permission_family_is_blocked_by_default() -> None:
    permissions = ToolPermissions()

    assert permissions.is_visible("delete_brain", permission_family="destructive") is True
    can_call, reason = permissions.can_call("delete_brain", permission_family="destructive")
    assert can_call is False
    assert reason == "permission_family_blocked_by_local_mcp_config"


def test_pr12m_c_tools_call_rejects_unknown_tools_before_http_post() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(AgvmMcpConfig(api_base_url=base_url, active_brain_id="alpha_brain"))
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "delete_everything", "arguments": {}},
            }
        )
    finally:
        _shutdown(http_server)

    assert response is not None
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["reason"] == "tool_not_in_agvm_contract_registry"
    assert handler.post_requests == []


def test_pr12m_c_tools_call_validates_required_contract_arguments_before_http_post() -> None:
    http_server, handler, base_url = _stub_api()
    try:
        server = AgvmMcpServer(AgvmMcpConfig(api_base_url=base_url))
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "create_brain", "arguments": {"display_name": "Unsafe Default Brain"}},
            }
        )
    finally:
        _shutdown(http_server)

    assert response is not None
    assert response["result"]["isError"] is True
    payload = response["result"]["structuredContent"]
    assert payload["reason"] == "tool_required_arguments_missing"
    assert payload["data"]["missing_arguments"] == ["make_active", "make_default"]
    assert handler.post_requests == []


def test_pr12m_c_stdio_protocol_initialize_and_notification_handling() -> None:
    server = AgvmMcpServer(AgvmMcpConfig(api_base_url="http://127.0.0.1:8010"))
    stdin = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": "init-1", "method": "initialize", "params": {}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        + "\n"
    )
    stdout = StringIO()

    assert run_stdio(server, stdin=stdin, stdout=stdout) == 0
    output_lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(output_lines) == 1
    assert output_lines[0]["id"] == "init-1"
    assert output_lines[0]["result"]["serverInfo"]["name"] == "agvm-local-memory-os"
    assert output_lines[0]["result"]["capabilities"]["tools"]["listChanged"] is False


def test_pr12m_c_config_manifest_and_docs_close_slice() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    config_example = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    readme = README.read_text(encoding="utf-8")
    gitignore = GITIGNORE.read_text(encoding="utf-8")
    internal_docs_available = all(path.exists() for path in (REPORT, SPEC, INDEX, MASTER, ROADMAP))

    loaded = load_config(CONFIG_EXAMPLE)
    assert loaded.api_base_url == "http://127.0.0.1:8010"
    assert loaded.selected_brain_id == "example_local_brain"
    assert "registry_write" in loaded.tool_permissions.allowed_permission_families
    assert "preview_only" in loaded.tool_permissions.allowed_permission_families
    assert "destructive" in loaded.tool_permissions.blocked_permission_families
    assert "write_memory_commit" in loaded.tool_permissions.allow_mutation_tools
    assert loaded.module_access.visibility_policy == "block_unlicensed"
    assert manifest["schema_version"] == "agvm.local_mcp_server_manifest.v1"
    assert manifest["transport"] == "stdio"
    assert manifest["contract_registry_endpoint"] == "/mcp/contracts"
    assert manifest["tool_call_endpoint_policy"] == "contract_metadata_endpoint_path_and_http_method"
    assert manifest["module_access"]["required_module_field"] == "tool_registration.required_module_id"
    assert manifest["module_access"]["visibility_policy_default_for_generated_local_configs"] == "block_unlicensed"
    assert "registry_write" in manifest["permission_families"]
    assert config_example["tool_permissions"]["enabled_tools"] == ["*"]
    assert config_example["module_access"]["visibility_policy"] == "block_unlicensed"
    assert "preview_only_learning" in config_example["permission_profiles"]
    assert "explicit_apply" in config_example["permission_profiles"]["preview_only_learning"]["blocked_permission_families"]
    assert "agvm_mcp_server/config.local.json" in gitignore
    assert "python -m agvm_mcp_server" in readme
    if internal_docs_available:
        report = REPORT.read_text(encoding="utf-8")
        spec = SPEC.read_text(encoding="utf-8")
        index = INDEX.read_text(encoding="utf-8")
        master = MASTER.read_text(encoding="utf-8")
        roadmap = ROADMAP.read_text(encoding="utf-8")
        assert "local stdio MCP smoke" in report
        assert "python -m agvm_mcp_server" in report
        assert "Local MCP Proof" in spec
        assert "brain_health" in index
        assert "MCP" in master
        assert "Phase 8 - Local MCP Proof" in roadmap
