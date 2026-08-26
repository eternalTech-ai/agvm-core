# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_REQUIRED_TOOLS = (
    "retrieve_context",
    "retrieve_document",
    "retrieve_path_corridor",
    "inspect_context_package",
    "inspect_route",
    "inspect_path_corridor",
    "grow_source_preview",
    "geometry_calibration_preview",
    "sleep_preview",
)


@dataclass
class JsonRpcExchange:
    method: str
    elapsed_ms: float
    response: dict[str, Any]
    error: str | None = None


@dataclass
class LocalMcpClientProbeResult:
    schema_version: str = "agvm.local_mcp_client_probe.v1"
    transport: str = "stdio_jsonrpc_subprocess"
    server_command: list[str] = field(default_factory=list)
    base_url: str = ""
    brain_id: str | None = None
    all_pass: bool = False
    failures: list[str] = field(default_factory=list)
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    search_id: str | None = None
    call_matrix: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_only_gate: dict[str, Any] = field(default_factory=dict)
    ambiguous_scope_gate: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transport": self.transport,
            "server_command": self.server_command,
            "base_url": self.base_url,
            "brain_id": self.brain_id,
            "all_pass": self.all_pass,
            "failures": self.failures,
            "exchanges": self.exchanges,
            "tool_names": self.tool_names,
            "search_id": self.search_id,
            "call_matrix": self.call_matrix,
            "read_only_gate": self.read_only_gate,
            "ambiguous_scope_gate": self.ambiguous_scope_gate,
            "elapsed_ms": self.elapsed_ms,
        }


class StdioMcpJsonRpcClient:
    def __init__(
        self,
        *,
        command: list[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.command = command or [sys.executable, "-m", "agvm_mcp_server"]
        self.cwd = Path(cwd or Path(__file__).resolve().parents[1])
        self.env = {**os.environ, **dict(env or {})}
        self.timeout_seconds = float(timeout_seconds)
        self._next_id = 1
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_queue: queue.Queue[str | None] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    def __enter__(self) -> "StdioMcpJsonRpcClient":
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._read_lines, args=(self.process.stdout, self._stdout_queue), daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_lines, args=(self.process.stderr, self._stderr_queue), daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_seconds: float | None = None) -> JsonRpcExchange:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("MCP process is not running")
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        started = time.perf_counter()
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        raw_line = self._read_response_line(timeout_seconds or self.timeout_seconds)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        try:
            response = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            return JsonRpcExchange(
                method=method,
                elapsed_ms=elapsed_ms,
                response={},
                error=f"invalid_json_response:{exc}:{raw_line[:300]}",
            )
        if response.get("id") != request_id:
            return JsonRpcExchange(
                method=method,
                elapsed_ms=elapsed_ms,
                response=response if isinstance(response, dict) else {},
                error=f"unexpected_response_id:{response.get('id')}:{request_id}",
            )
        return JsonRpcExchange(
            method=method,
            elapsed_ms=elapsed_ms,
            response=response,
            error=(str((response.get("error") or {}).get("message") or "") or None) if "error" in response else None,
        )

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout_seconds: float | None = None) -> JsonRpcExchange:
        return self.call("tools/call", {"name": name, "arguments": arguments}, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self.process = None

    def stderr_preview(self, *, limit: int = 20) -> list[str]:
        rows: list[str] = []
        while len(rows) < limit:
            try:
                line = self._stderr_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            rows.append(line.rstrip("\n"))
        return rows

    def _read_response_line(self, timeout_seconds: float) -> str:
        deadline = time.perf_counter() + max(0.1, float(timeout_seconds))
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                stderr = self.stderr_preview(limit=8)
                raise TimeoutError(f"timed_out_waiting_for_mcp_response; stderr={stderr}")
            try:
                line = self._stdout_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                process = self.process
                if process is not None and process.poll() is not None:
                    stderr = self.stderr_preview(limit=20)
                    raise RuntimeError(f"mcp_process_exited:{process.returncode}; stderr={stderr}")
                continue
            if line is None:
                raise RuntimeError(f"mcp_stdout_closed; stderr={self.stderr_preview(limit=20)}")
            if line.strip():
                return line

    @staticmethod
    def _read_lines(stream: Any, output_queue: queue.Queue[str | None]) -> None:
        try:
            for line in stream or []:
                output_queue.put(line)
        finally:
            output_queue.put(None)


def _structured(exchange: JsonRpcExchange) -> dict[str, Any]:
    response = dict(exchange.response or {})
    result = dict(response.get("result") or {})
    return dict(result.get("structuredContent") or {})


def _tool_error_reason(exchange: JsonRpcExchange) -> str | None:
    payload = _structured(exchange)
    return str(payload.get("reason") or "") or None


def _record_exchange(result: LocalMcpClientProbeResult, exchange: JsonRpcExchange) -> None:
    status = "jsonrpc_error" if exchange.error else "ok"
    result.exchanges.append(
        {
            "method": exchange.method,
            "status": status,
            "elapsed_ms": exchange.elapsed_ms,
            "error": exchange.error,
        }
    )


def _record_tool_call(result: LocalMcpClientProbeResult, name: str, exchange: JsonRpcExchange) -> dict[str, Any]:
    payload = _structured(exchange)
    matrix = {
        "called": True,
        "jsonrpc_ok": exchange.error is None,
        "is_error": bool(((exchange.response.get("result") or {}) if isinstance(exchange.response, dict) else {}).get("isError")),
        "status": payload.get("status"),
        "search_id": payload.get("search_id") or (dict(payload.get("completeness") or {}).get("search_id") if isinstance(payload.get("completeness"), dict) else None),
        "schema_version": payload.get("schema_version"),
        "brain_id": payload.get("brain_id"),
        "elapsed_ms": exchange.elapsed_ms,
    }
    if isinstance(payload.get("maintenance_latency_profile"), dict):
        matrix["maintenance_latency_profile"] = dict(payload.get("maintenance_latency_profile") or {})
    if isinstance(payload.get("source_latency_profile"), dict):
        matrix["source_latency_profile"] = dict(payload.get("source_latency_profile") or {})
    if payload.get("reason"):
        matrix["blocked_reason"] = str(payload.get("reason"))
    action_contract = dict(dict(payload.get("data") or {}).get("action_contract") or {})
    if action_contract:
        matrix["action_contract"] = action_contract
    result.call_matrix[name] = matrix
    _record_exchange(result, exchange)
    return payload


def _validate_tool_payload(
    result: LocalMcpClientProbeResult,
    tool_name: str,
    payload: dict[str, Any],
    *,
    required_field: str | None = None,
    allow_blocked: bool = True,
) -> None:
    if not payload:
        result.failures.append(f"{tool_name}:empty_structured_content")
        return
    if payload.get("tool_name") != tool_name:
        result.failures.append(f"{tool_name}:wrong_tool_name:{payload.get('tool_name')}")
    if required_field and required_field not in payload:
        result.failures.append(f"{tool_name}:missing_field:{required_field}")
    status = str(payload.get("status") or "")
    allowed = {"ok", "partial", "no_match", "needs_clarification", "preview_ready", "asking_clarification", "applied"}
    if allow_blocked:
        allowed.add("blocked")
    if status not in allowed:
        result.failures.append(f"{tool_name}:unexpected_status:{status}")


def _validate_hosted_mcp_action_contract(
    result: LocalMcpClientProbeResult,
    tool_name: str,
    payload: dict[str, Any],
) -> None:
    reason = str(payload.get("reason") or "")
    data = dict(payload.get("data") or {})
    action = dict(data.get("action_contract") or {})
    if reason != "detwin_cloud_auth_required":
        result.failures.append(f"{tool_name}:unexpected_local_paid_reason:{reason}")
    expected = {
        "schema_version": "agvm.local_mcp_paid_tool_action.v1",
        "action": "use_detwin_cloud_for_advanced_tool",
        "tool_name": tool_name,
        "execution_surface": "hosted_mcp",
        "credential_environment_variable": "AGVM_HOSTED_MCP_API_KEY",
    }
    for field_name, expected_value in expected.items():
        if action.get(field_name) != expected_value:
            result.failures.append(
                f"{tool_name}:invalid_hosted_action_contract:{field_name}:{action.get(field_name)}"
            )
    for field_name in ("requires_account", "requires_credits", "requires_cloud_handoff"):
        if action.get(field_name) is not True:
            result.failures.append(f"{tool_name}:invalid_hosted_action_contract:{field_name}")


def run_local_mcp_client_probe(
    *,
    base_url: str,
    brain_id: str | None,
    command: list[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    env = {
        "AGVM_API_BASE_URL": str(base_url).rstrip("/"),
        "AGVM_MCP_CONFIG": "",
        "AGVM_MCP_TIMEOUT_SECONDS": str(timeout_seconds),
    }
    if brain_id:
        env["AGVM_MCP_BRAIN_ID"] = brain_id
    result = LocalMcpClientProbeResult(
        server_command=command or [sys.executable, "-m", "agvm_mcp_server"],
        base_url=str(base_url).rstrip("/"),
        brain_id=brain_id,
    )
    try:
        with StdioMcpJsonRpcClient(command=command, cwd=cwd, env=env, timeout_seconds=timeout_seconds) as client:
            initialize = client.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "agvm-local-mcp-probe"}})
            _record_exchange(result, initialize)
            if initialize.error:
                result.failures.append(f"initialize:{initialize.error}")
                return _finalize_result(result, started)

            listed = client.call("tools/list")
            _record_exchange(result, listed)
            tool_names = [
                str(item.get("name") or "")
                for item in list(((listed.response.get("result") or {}).get("tools") or []))
                if isinstance(item, dict)
            ]
            result.tool_names = tool_names
            for tool_name in DEFAULT_REQUIRED_TOOLS:
                if tool_name not in tool_names:
                    result.failures.append(f"tools_list_missing:{tool_name}")

            retrieve_context = client.call_tool(
                "retrieve_context",
                {
                    "query_text": "raccontami di te e del lavoro principale",
                    "retrieval_mode": "balanced",
                    "context_package_mode": "broad_dossier",
                    "max_matches": 8,
                    "include_answer_demo": False,
                },
            )
            context_payload = _record_tool_call(result, "retrieve_context", retrieve_context)
            _validate_tool_payload(result, "retrieve_context", context_payload, required_field="context_package")
            result.search_id = str(context_payload.get("search_id") or dict(context_payload.get("completeness") or {}).get("search_id") or "").strip() or None
            if not result.search_id:
                result.failures.append("retrieve_context_missing_search_id")

            retrieve_document = client.call_tool(
                "retrieve_document",
                {
                    "query_text": "trova il documento su BaxEnergy e Yokogawa",
                    "document_hint": "BaxEnergy Yokogawa",
                    "retrieval_mode": "balanced",
                    "context_package_mode": "document_full",
                    "include_raw_text": True,
                    "max_matches": 8,
                },
            )
            document_payload = _record_tool_call(result, "retrieve_document", retrieve_document)
            _validate_tool_payload(result, "retrieve_document", document_payload, required_field="document_workspace")

            retrieve_corridor = client.call_tool(
                "retrieve_path_corridor",
                {
                    "query_text": "collega BaxEnergy, Yokogawa e le aziende fondate",
                    "retrieval_mode": "balanced",
                    "complete_paths": True,
                    "max_matches": 8,
                },
            )
            corridor_payload = _record_tool_call(result, "retrieve_path_corridor", retrieve_corridor)
            _validate_tool_payload(result, "retrieve_path_corridor", corridor_payload, required_field="path_corridors")

            if result.search_id:
                inspect_context = client.call_tool(
                    "inspect_context_package",
                    {"search_id": result.search_id, "include_raw_text": False, "include_answer_demo": False},
                )
                inspect_context_payload = _record_tool_call(result, "inspect_context_package", inspect_context)
                _validate_tool_payload(result, "inspect_context_package", inspect_context_payload, required_field="context_package")

                inspect_route = client.call_tool("inspect_route", {"search_id": result.search_id, "include_debug": False})
                inspect_route_payload = _record_tool_call(result, "inspect_route", inspect_route)
                _validate_tool_payload(result, "inspect_route", inspect_route_payload, required_field="route_trace")

                inspect_corridor = client.call_tool("inspect_path_corridor", {"search_id": result.search_id, "include_raw_text": False})
                inspect_corridor_payload = _record_tool_call(result, "inspect_path_corridor", inspect_corridor)
                _validate_tool_payload(result, "inspect_path_corridor", inspect_corridor_payload, required_field="path_corridors")

            grow_preview = client.call_tool(
                "grow_source_preview",
                {
                    "raw_input": "Simone Massaro founded BaxEnergy in 2010 and is associated with renewable energy management.",
                    "input_kind": "manual_text",
                    "source_label": "local MCP probe source",
                    "run_preview": False,
                    "options": {
                        "max_units": 4,
                        "question_limit": 2,
                        "pause_on_questions": False,
                    },
                },
            )
            grow_payload = _record_tool_call(result, "grow_source_preview", grow_preview)
            _validate_tool_payload(result, "grow_source_preview", grow_payload, required_field="source_investigation", allow_blocked=True)
            grow_profile = dict(grow_payload.get("mcp_latency_profile") or {})
            if grow_profile.get("mode") != "source_unit_only":
                result.failures.append(f"grow_source_preview_fast_profile_missing:{grow_profile}")

            matrix_calibration = client.call_tool("geometry_calibration_preview", {"max_nodes_considered": 4000})
            matrix_payload = _record_tool_call(result, "geometry_calibration_preview", matrix_calibration)
            _validate_hosted_mcp_action_contract(result, "geometry_calibration_preview", matrix_payload)

            sleep_preview = client.call_tool("sleep_preview", {"mode": "sleep", "dry_run": True, "max_nodes_considered": 20})
            sleep_payload = _record_tool_call(result, "sleep_preview", sleep_preview)
            _validate_hosted_mcp_action_contract(result, "sleep_preview", sleep_payload)
    except Exception as exc:  # noqa: BLE001
        result.failures.append(f"stdio_probe_exception:{type(exc).__name__}:{str(exc)[:500]}")

    result.read_only_gate = _probe_read_only_gate(base_url=base_url, brain_id=brain_id, command=command, cwd=cwd, timeout_seconds=min(timeout_seconds, 30.0))
    if not bool(result.read_only_gate.get("passed")):
        result.failures.append(f"read_only_gate_failed:{result.read_only_gate}")
    result.ambiguous_scope_gate = _probe_ambiguous_scope_gate(base_url=base_url, command=command, cwd=cwd, timeout_seconds=min(timeout_seconds, 30.0))
    if not bool(result.ambiguous_scope_gate.get("passed")):
        result.failures.append(f"ambiguous_scope_gate_failed:{result.ambiguous_scope_gate}")
    return _finalize_result(result, started)


def _probe_read_only_gate(
    *,
    base_url: str,
    brain_id: str | None,
    command: list[str] | None,
    cwd: str | os.PathLike[str] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    env = {
        "AGVM_API_BASE_URL": str(base_url).rstrip("/"),
        "AGVM_MCP_CONFIG": "",
        "AGVM_MCP_READ_ONLY": "true",
        "AGVM_MCP_TIMEOUT_SECONDS": str(timeout_seconds),
    }
    if brain_id:
        env["AGVM_MCP_BRAIN_ID"] = brain_id
    try:
        with StdioMcpJsonRpcClient(command=command, cwd=cwd, env=env, timeout_seconds=timeout_seconds) as client:
            listed = client.call("tools/list")
            tool_names = [
                str(item.get("name") or "")
                for item in list(((listed.response.get("result") or {}).get("tools") or []))
                if isinstance(item, dict)
            ]
            blocked = client.call_tool("write_memory_commit", {"text": "this must be blocked"})
            reason = _tool_error_reason(blocked)
            passed = "write_memory_commit" in tool_names and reason == "mutation_tool_blocked_by_read_only_local_mcp_config"
            return {
                "passed": passed,
                "write_memory_commit_discoverable": "write_memory_commit" in tool_names,
                "blocked_reason": reason,
                "is_error": bool(((blocked.response.get("result") or {}) if isinstance(blocked.response, dict) else {}).get("isError")),
            }
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"{type(exc).__name__}:{str(exc)[:300]}"}


def _probe_ambiguous_scope_gate(
    *,
    base_url: str,
    command: list[str] | None,
    cwd: str | os.PathLike[str] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    env = {
        "AGVM_API_BASE_URL": str(base_url).rstrip("/"),
        "AGVM_MCP_CONFIG": "",
        "AGVM_MCP_BRAIN_ID": "",
        "AGVM_MCP_TENANT_ID": "",
        "AGVM_MCP_ORGANIZATION_ID": "",
        "AGVM_MCP_USER_ID": "",
        "AGVM_MCP_ENVIRONMENT_ID": "",
        "AGVM_MCP_TIMEOUT_SECONDS": str(timeout_seconds),
    }
    try:
        with StdioMcpJsonRpcClient(command=command, cwd=cwd, env=env, timeout_seconds=timeout_seconds) as client:
            blocked = client.call_tool("retrieve_context", {"query_text": "scope must be explicit"})
            reason = _tool_error_reason(blocked)
            passed = reason == "brain_id_required_for_ambiguous_local_mcp_scope"
            return {
                "passed": passed,
                "blocked_reason": reason,
                "is_error": bool(((blocked.response.get("result") or {}) if isinstance(blocked.response, dict) else {}).get("isError")),
            }
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"{type(exc).__name__}:{str(exc)[:300]}"}


def _finalize_result(result: LocalMcpClientProbeResult, started: float) -> dict[str, Any]:
    result.elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    result.all_pass = not result.failures
    return result.to_dict()
