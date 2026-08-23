# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import re
from typing import Any

from mcp_contracts import IMPLEMENTED_MCP_TOOL_NAMES, REQUIRED_MCP_TOOL_NAMES
from mcp_retrieval import build_mcp_retrieval_tool_output


MCP_STABILITY_REPORT_SCHEMA_VERSION = "agvm.mcp_stability_report.v1"
MCP_TOOL_OUTPUT_VALIDATION_SCHEMA_VERSION = "agvm.mcp_tool_output_validation.v1"
MCP_RETRIEVAL_ADAPTER_REGRESSION_SCHEMA_VERSION = "agvm.mcp_retrieval_adapter_regression.v1"

_DEBUG_PROSE_PATTERNS = [
    re.compile(r"\bvec_node_[A-Za-z0-9_:-]+\b"),
    re.compile(r"\bFact\s+\[vec_[A-Za-z0-9_:-]+\]", re.IGNORECASE),
    re.compile(r"\bEvidence Ledger\b", re.IGNORECASE),
    re.compile(r"^##\s*Path Discoveries\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bLanding\s+\d+\s*->\s*Landing\s+\d+\b", re.IGNORECASE),
]


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _tool_contracts_by_name(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(tool.get("name") or ""): dict(tool)
        for tool in _as_list(registry.get("tools"))
        if isinstance(tool, dict) and str(tool.get("name") or "").strip()
    }


def _primary_text_fields(value: Any, *, path: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key) in {"agent_markdown", "primary_markdown", "primary_prose", "context_markdown"} and isinstance(child, str):
                rows.append({"path": child_path, "text": child})
            else:
                rows.extend(_primary_text_fields(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rows.extend(_primary_text_fields(child, path=f"{path}[{index}]"))
    return rows


def _debug_leakage_rows(output: dict[str, Any]) -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    for row in _primary_text_fields(output):
        text = str(row.get("text") or "")
        for pattern in _DEBUG_PROSE_PATTERNS:
            if pattern.search(text):
                leaks.append({"path": str(row.get("path") or ""), "pattern": pattern.pattern})
    return leaks


def validate_mcp_tool_output(tool_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    name = str(tool_contract.get("name") or "")
    output_payload = dict(output or {})
    output_schema = _as_dict(tool_contract.get("output_schema"))
    properties = _as_dict(output_schema.get("properties"))
    required = [str(item) for item in _as_list(output_schema.get("required"))]
    errors: list[dict[str, Any]] = []

    if output_payload.get("tool_name") != name:
        errors.append({"code": "tool_name_mismatch", "expected": name, "actual": output_payload.get("tool_name")})
    for field_name in required:
        if field_name not in output_payload:
            errors.append({"code": "required_field_missing", "field": field_name})
    status_schema = _as_dict(properties.get("status"))
    allowed_statuses = set(str(item) for item in _as_list(status_schema.get("enum")))
    if allowed_statuses and str(output_payload.get("status") or "") not in allowed_statuses:
        errors.append({"code": "status_not_allowed", "status": output_payload.get("status"), "allowed": sorted(allowed_statuses)})
    if bool(tool_contract.get("default_includes_answer_demo")):
        errors.append({"code": "answer_demo_default_enabled"})
    if output_payload.get("answer_demo"):
        errors.append({"code": "answer_demo_present_in_default_fixture"})
    default_package = str(tool_contract.get("default_output_package") or "")
    if default_package and default_package not in output_payload:
        errors.append({"code": "default_output_package_missing", "field": default_package})
    for law in ["stable_json_first_contract", "answer_demo_is_not_default_mcp_output"]:
        if law not in _as_list(tool_contract.get("output_laws")):
            errors.append({"code": "output_law_missing", "law": law})
    leakage = _debug_leakage_rows(output_payload)
    for leak in leakage:
        errors.append({"code": "debug_identifier_leakage_in_primary_prose", **leak})

    return {
        "schema_version": MCP_TOOL_OUTPUT_VALIDATION_SCHEMA_VERSION,
        "tool_name": name,
        "passed": not errors,
        "error_count": len(errors),
        "errors": errors,
        "required_fields": required,
        "default_output_package": default_package,
        "status": output_payload.get("status"),
    }


def build_mcp_surface_stability_report(
    *,
    registry: dict[str, Any],
    representative_outputs: list[dict[str, Any]],
    retrieval_regression_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contracts_by_name = _tool_contracts_by_name(registry)
    output_by_name = {
        str(output.get("tool_name") or ""): dict(output)
        for output in representative_outputs
        if isinstance(output, dict) and str(output.get("tool_name") or "").strip()
    }
    missing_contracts = [name for name in REQUIRED_MCP_TOOL_NAMES if name not in contracts_by_name]
    missing_representative_outputs = [name for name in REQUIRED_MCP_TOOL_NAMES if name not in output_by_name]
    unimplemented_tools = [
        name
        for name in REQUIRED_MCP_TOOL_NAMES
        if name not in IMPLEMENTED_MCP_TOOL_NAMES
        or str(_as_dict(contracts_by_name.get(name)).get("implementation_status") or "") != "implemented"
        or str(_as_dict(_as_dict(contracts_by_name.get(name)).get("backend_binding")).get("binding_state") or "") != "implemented"
    ]
    validations = [
        validate_mcp_tool_output(contracts_by_name[name], output_by_name[name])
        for name in REQUIRED_MCP_TOOL_NAMES
        if name in contracts_by_name and name in output_by_name
    ]
    validation_failures = [item for item in validations if not bool(item.get("passed"))]
    category_coverage: dict[str, int] = {}
    for name in output_by_name:
        category = str(_as_dict(contracts_by_name.get(name)).get("category") or "unknown")
        category_coverage[category] = category_coverage.get(category, 0) + 1
    regression = _as_dict(retrieval_regression_report)
    passed = (
        bool(_as_dict(registry.get("registry_validation")).get("passed"))
        and not missing_contracts
        and not missing_representative_outputs
        and not unimplemented_tools
        and not validation_failures
        and (not regression or bool(regression.get("passed")))
    )
    return {
        "schema_version": MCP_STABILITY_REPORT_SCHEMA_VERSION,
        "source_slice": "PR-12J-E",
        "passed": passed,
        "required_tool_count": len(REQUIRED_MCP_TOOL_NAMES),
        "registered_tool_count": len(contracts_by_name),
        "representative_output_count": len(output_by_name),
        "missing_contracts": missing_contracts,
        "missing_representative_outputs": missing_representative_outputs,
        "unimplemented_tools": unimplemented_tools,
        "category_coverage": category_coverage,
        "tool_output_validations": validations,
        "tool_output_failure_count": len(validation_failures),
        "retrieval_regression_report": regression,
    }


def _first_document(workspace: dict[str, Any]) -> dict[str, Any]:
    documents = [dict(item) for item in _as_list(workspace.get("documents")) if isinstance(item, dict)]
    return documents[0] if documents else {}


def build_mcp_retrieval_adapter_regression_report(retrieve_result: dict[str, Any]) -> dict[str, Any]:
    result = dict(retrieve_result or {})
    context = build_mcp_retrieval_tool_output("retrieve_context", result)
    document_redacted = build_mcp_retrieval_tool_output("retrieve_document", result, include_raw_text=False)
    document_raw = build_mcp_retrieval_tool_output("retrieve_document", result, include_raw_text=True)
    path = build_mcp_retrieval_tool_output("retrieve_path_corridor", result)
    source_trace = build_mcp_retrieval_tool_output("retrieve_source_trace", result)
    redacted_doc = _first_document(_as_dict(document_redacted.get("document_workspace")))
    raw_doc = _first_document(_as_dict(document_raw.get("document_workspace")))
    planner_runtime = _as_dict(result.get("planner_runtime"))
    checks = {
        "pr12a_semantic_contract_present": bool(result.get("semantic_contract")) and bool(_as_dict(result.get("semantic_contract_runtime")).get("contract_passed", True)),
        "pr12b_context_package_v2_present": _as_dict(context.get("context_package")).get("schema_version") == "agvm.mcp_context_package.v2",
        "pr12c_path_corridor_present": bool(_as_list(_as_dict(path.get("path_corridors")).get("corridors"))),
        "pr12d_document_workspace_present": bool(_as_list(_as_dict(document_raw.get("document_workspace")).get("documents"))),
        "pr12d_raw_text_redacted_by_default": redacted_doc.get("full_text") == "" and bool(redacted_doc.get("full_text_available")),
        "pr12d_raw_text_preserved_when_requested": bool(str(raw_doc.get("full_text") or "").strip()),
        "pr12e_answer_demo_downstream_not_default": "answer_demo" not in context and "answer_demo" not in document_redacted,
        "pr12f_retrieve_adapter_is_read_only": "persist_result" not in context and "learning_policy" not in context,
        "pr12g_geometry_signal_present": bool(
            planner_runtime.get("brain_geometry_calibration")
            or planner_runtime.get("geometry_calibration")
            or result.get("brain_geometry_calibration")
            or result.get("geometry_calibration_report")
        ),
        "source_trace_present": bool(source_trace.get("source_trace")),
        "completeness_and_budget_present": bool(context.get("completeness")) and bool(context.get("budget")),
    }
    failed = [name for name, passed in checks.items() if not bool(passed)]
    return {
        "schema_version": MCP_RETRIEVAL_ADAPTER_REGRESSION_SCHEMA_VERSION,
        "source_slice": "PR-12J-E",
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "adapter_outputs": {
            "retrieve_context_status": context.get("status"),
            "retrieve_document_status": document_raw.get("status"),
            "retrieve_path_corridor_status": path.get("status"),
            "retrieve_source_trace_status": source_trace.get("status"),
        },
    }
