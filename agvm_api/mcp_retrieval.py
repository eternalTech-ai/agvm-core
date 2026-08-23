# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from document_need_contract import build_target_document_need_contract
from retrieval import build_mission_learning_rollup, normalize_retrieve_response_payload
from run_projection import build_run_projection_truth


MCP_RETRIEVAL_OUTPUT_SCHEMA_VERSION = "agvm.mcp_retrieval_tool_output.v1"
MCP_ROUTE_TRACE_SCHEMA_VERSION = "agvm.mcp_route_trace.v1"
MCP_MEMORY_OBJECT_SCHEMA_VERSION = "agvm.mcp_memory_object_inspection.v1"
MCP_COMPLETION_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14n.mcp_completion_contract.v1"
MCP_STAGE_TIMING_SCHEMA_VERSION = "agvm.pr12p14n.stage_timing.v1"
MCP_PAYLOAD_TRUTH_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14o.payload_truth_contract.v1"
MCP_RUNTIME_STATE_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14u_a.runtime_state_contract.v1"
MCP_TOOL_BOUNDARY_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14u_b.tool_boundary_contract.v1"
MCP_AI_MATERIALIZATION_RESILIENCE_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14u_c.ai_materialization_resilience_contract.v1"
MCP_FIRST_PACKAGE_BACKGROUND_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14u_d.first_package_background_contract.v1"
MCP_RUN_PROJECTION_EVENT_STREAM_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14u_e.run_projection_event_stream_contract.v1"
MCP_DELIVERY_CONTRACT_SCHEMA_VERSION = "agvm.pr12p14u_h.mcp_delivery_contract.v1"
MCP_AI_CRITICAL_PATH_CONTRACT_SCHEMA_VERSION = "agvm.pr12p8b_b.ai_critical_path_contract.v1"
MCP_ROUTE_ARBITRATION_CONTRACT_SCHEMA_VERSION = "agvm.pr12p8b_c.route_arbitration_contract.v1"

PR12J_B_RETRIEVAL_TOOL_NAMES = {
    "retrieve_context",
    "retrieve_document",
    "retrieve_document_workspace",
    "retrieve_project_workspace",
    "retrieve_path_corridor",
    "retrieve_source_trace",
    "inspect_context_package",
    "inspect_route",
    "inspect_path_corridor",
    "inspect_memory_object",
}

DOCUMENT_WORKSPACE_MCP_TOOL_NAMES = {"retrieve_document_workspace", "retrieve_project_workspace"}
DOCUMENT_PAYLOAD_MCP_TOOL_NAMES = {"retrieve_document", *DOCUMENT_WORKSPACE_MCP_TOOL_NAMES}

_RAW_TEXT_KEYS = {
    "full_text",
    "raw_text",
    "raw_document_text",
    "raw_source_text",
    "document_raw_text",
    "source_raw_text",
    "deferred_raw_text",
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _ai_hard_gate_satisfied(ai_gate: dict[str, Any]) -> bool:
    gate = _as_dict(ai_gate)
    if bool(gate.get("satisfied")):
        return True
    if bool(gate.get("blocked")):
        return False
    state = str(gate.get("validation_state") or "").strip()
    return state in {
        "ai_materialization_validated",
        "exact_document_lookup_exception",
        "materialized",
        "mcp_context_package_ready",
        "satisfied",
    }


def _dedupe_text_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        output.append(item)
    return output


def _document_workspace_has_documents(workspace: dict[str, Any]) -> bool:
    return bool(_as_list(_as_dict(workspace).get("documents")))


def _result_document_workspace(result: dict[str, Any]) -> dict[str, Any]:
    top_level = _as_dict(result.get("document_workspace"))
    embedded = _as_dict(_as_dict(result.get("context_package")).get("document_workspace"))
    if _document_workspace_has_documents(top_level):
        return top_level
    if _document_workspace_has_documents(embedded):
        return embedded
    return top_level or embedded


_AI_CERTIFYING_STATES = {"fresh_llm", "cached_llm", "route_materialized"}
_PROVIDER_DEGRADED_STATES = {"provider_degraded", "degraded", "timeout", "timed_out", "error"}


def _semantic_cache_validity(semantic_runtime: dict[str, Any]) -> dict[str, Any]:
    runtime = _as_dict(semantic_runtime)
    cache = _as_dict(runtime.get("cache"))
    cache_hit = bool(runtime.get("cache_hit") or runtime.get("cached_ai_contract") or cache.get("hit"))
    semantic_material = bool(runtime.get("material"))
    source = str(runtime.get("source") or "").strip()
    cached_source = str(runtime.get("cached_source") or "").strip()
    provider_state = str(runtime.get("provider_state") or "").strip()
    cache_source_valid = bool(
        source == "semantic_contract_cache"
        or cached_source == "llm"
        or provider_state == "cached_ai_contract"
    )
    brain_revision = str(runtime.get("brain_revision") or cache.get("brain_revision") or "").strip()
    key_fingerprint = str(runtime.get("cache_key_fingerprint") or cache.get("key_fingerprint") or "").strip()
    cache_scope = str(runtime.get("cache_scope") or cache.get("cache_scope") or "").strip()
    model_profile = _as_dict(runtime.get("model_profile"))
    model = str(runtime.get("model") or model_profile.get("compiler_model") or "").strip()
    missing: list[str] = []
    if not cache_hit:
        missing.append("cache_hit")
    if not semantic_material:
        missing.append("semantic_material")
    if not cache_source_valid:
        missing.append("cached_llm_source")
    if not brain_revision:
        missing.append("brain_revision")
    if not key_fingerprint:
        missing.append("cache_key_fingerprint")
    if not cache_scope:
        missing.append("cache_scope")
    if not model:
        missing.append("model_profile")
    return {
        "valid_for_ai": not missing,
        "missing": missing,
        "hit": cache_hit,
        "source_valid": cache_source_valid,
        "brain_revision": brain_revision or None,
        "key_fingerprint": key_fingerprint or None,
        "cache_scope": cache_scope or None,
        "model": model or None,
    }


def _semantic_provider_state(semantic_runtime: dict[str, Any], runtime_provider_state: Any = None) -> dict[str, Any]:
    runtime = _as_dict(semantic_runtime)
    retry_policy = _as_dict(runtime.get("provider_retry_policy"))
    raw_state = str(
        runtime.get("provider_state")
        or runtime_provider_state
        or runtime.get("status")
        or runtime.get("cache_status")
        or ""
    ).strip()
    error_text = " ".join(
        str(runtime.get(key) or retry_policy.get(key) or "")
        for key in ("degraded_reason", "primary_error", "retry_error", "error")
    ).lower()
    degraded = bool(
        runtime.get("provider_degraded")
        or runtime.get("degraded")
        or retry_policy.get("provider_degraded")
        or raw_state in _PROVIDER_DEGRADED_STATES
        or any(marker in error_text for marker in ("timeout", "provider_error", "rate_limit", "overloaded", "api_error", "connection"))
    )
    timed_out = bool("timeout" in raw_state.lower() or "timed out" in error_text or "timeout" in error_text)
    unavailable = bool(raw_state in {"unavailable", "disabled", "missing_api_key", "not_configured", "llm_unavailable"})
    deferred = bool(raw_state in {"deferred", "pending", "waiting"})
    if timed_out:
        state = "timeout"
    elif degraded:
        state = "provider_degraded"
    elif unavailable:
        state = "provider_unavailable"
    elif deferred:
        state = "ai_pending"
    elif raw_state:
        state = "ok"
    else:
        state = "unknown"
    return {
        "state": state,
        "raw_state": raw_state or None,
        "degraded": degraded,
        "timed_out": timed_out,
        "unavailable": unavailable,
        "deferred": deferred,
    }


def _ai_materialization_state(
    *,
    ai_required: bool,
    semantic_material: bool,
    route_material: bool,
    hard_gate_satisfied: bool,
    gate_blocked: bool,
    semantic_runtime: dict[str, Any],
    provider_state: dict[str, Any],
) -> dict[str, Any]:
    runtime = _as_dict(semantic_runtime)
    source = str(runtime.get("source") or "").strip()
    cache_validity = _semantic_cache_validity(runtime)
    semantic_status = str(runtime.get("status") or "").strip()
    if not ai_required:
        state = "not_required"
    elif semantic_material and cache_validity["valid_for_ai"]:
        state = "cached_llm"
    elif semantic_material and source == "llm":
        state = "fresh_llm"
    elif route_material or hard_gate_satisfied:
        state = "route_materialized"
    elif provider_state.get("timed_out"):
        state = "timeout"
    elif provider_state.get("degraded") or provider_state.get("state") == "provider_degraded":
        state = "provider_degraded"
    elif semantic_status == "deferred" or provider_state.get("deferred"):
        state = "ai_pending"
    elif gate_blocked:
        state = "not_materialized"
    else:
        state = "not_materialized"
    certifiable = bool(state in _AI_CERTIFYING_STATES and not gate_blocked)
    return {
        "state": state,
        "certifiable": certifiable,
        "cache_validity": cache_validity,
        "first_ai_contract_ms": _as_ms(runtime.get("compiler_ms")),
    }


def _ai_spatial_pending_or_retryable(
    ai_spatial_landing_contract: dict[str, Any],
    *,
    observed: bool,
    ai_required: bool,
    materialized: bool,
) -> bool:
    contract = _as_dict(ai_spatial_landing_contract)
    if not observed or not ai_required or materialized:
        return False
    source = str(contract.get("source") or "").strip().lower()
    status = str(contract.get("status") or contract.get("materialization_state") or "").strip().lower()
    retry_policy = _as_dict(contract.get("provider_retry_policy"))
    missing_text = " ".join(
        [
            source,
            status,
            *[str(item or "") for item in _as_list(contract.get("missing_reasons"))],
            str(contract.get("error") or ""),
            str(retry_policy.get("primary_error") or ""),
            str(retry_policy.get("retry_error") or ""),
            str(retry_policy.get("retry_status") or ""),
            str(retry_policy.get("retry_skipped_reason") or ""),
        ]
    ).lower()
    hard_missing_sources = {
        "blocked_missing_metamemory",
        "blocked_missing_answer_strands",
        "ai_unavailable",
    }
    if source in hard_missing_sources or "llm_disabled" in missing_text or "metamemory_spatial_brief_missing" in missing_text:
        return False
    if source == "deferred" or status in {"deferred", "pending", "waiting"}:
        return True
    if "ai_spatial_contract_deferred" in missing_text:
        return True
    if source == "llm_error":
        return True
    retryable_markers = (
        "timeout",
        "timed out",
        "transport",
        "connection",
        "rate_limit",
        "overloaded",
        "provider_error",
        "api_error",
        "read operation",
        "runtime_timeout_no_sync_retry",
        "flash_timeout_no_sync_retry",
    )
    return any(marker in missing_text for marker in retryable_markers)


def _document_ref_raw_available(ref: dict[str, Any]) -> bool:
    availability = _as_dict(ref.get("raw_availability"))
    return bool(
        ref.get("raw_available")
        or ref.get("raw_text_available")
        or ref.get("complete_text_available")
        or ref.get("raw_text_char_count")
        or ref.get("available_raw_text_char_count")
        or str(availability.get("state") or "") == "raw_available"
    )


def _document_ref_identity_keys(ref: dict[str, Any]) -> list[str]:
    payload = _as_dict(ref)
    keys: list[str] = []
    call = _as_dict(
        payload.get("retrieve_document_call")
        or payload.get("document_evidence_retrieve_document_call")
        or payload.get("exact_follow_up_recipe")
        or payload.get("follow_up_call")
    )
    arguments = _as_dict(call.get("arguments") or call.get("payload"))
    document_id = str(payload.get("document_id") or payload.get("anchor_node_id") or payload.get("node_id") or "").strip()
    source_label = str(payload.get("source_label") or payload.get("source") or "").strip().lower()
    title = str(payload.get("title") or payload.get("document_title") or payload.get("document_hint") or "").strip().lower()
    recipe_document_id = str(arguments.get("document_id") or "").strip()
    if document_id.startswith("vec_") and not source_label and not title and not recipe_document_id:
        return []
    if document_id:
        keys.append(f"id:{document_id}")
    if source_label:
        keys.append(f"source:{source_label}")
    if not keys:
        return []
    if title:
        keys.append(f"title:{title}")
    return keys


def _merge_document_ref(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in dict(incoming or {}).items():
        if value in (None, "", [], {}):
            continue
        current = merged.get(key)
        if current in (None, "", [], {}):
            merged[key] = value
            continue
        if key == "raw_availability" and isinstance(current, dict) and isinstance(value, dict):
            merged[key] = {**value, **current}
        elif key in {"why_included", "follow_up_tools", "document_rank_reasons"} and isinstance(current, list) and isinstance(value, list):
            merged[key] = _dedupe_contract_items([*current, *value], limit=24)
    return merged


def _visible_document_refs(*sources: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    key_to_index: dict[str, int] = {}
    for source in sources:
        for item in _as_list(source):
            if not isinstance(item, dict):
                continue
            ref = dict(item)
            keys = _document_ref_identity_keys(ref)
            if not keys:
                continue
            existing_index = next((key_to_index[key] for key in keys if key in key_to_index), None)
            if existing_index is not None:
                refs[existing_index] = _merge_document_ref(refs[existing_index], ref)
                for merged_key in _document_ref_identity_keys(refs[existing_index]):
                    key_to_index.setdefault(merged_key, existing_index)
                continue
            index = len(refs)
            refs.append(ref)
            for key in keys:
                key_to_index[key] = index
    return refs


def _document_refs_from_workspace_documents(workspace: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for document in _as_list(_as_dict(workspace).get("documents")):
        if not isinstance(document, dict):
            continue
        document_id = str(
            document.get("document_id")
            or document.get("anchor_node_id")
            or document.get("node_id")
            or document.get("id")
            or ""
        ).strip()
        title = str(document.get("title") or document.get("source_label") or document_id or "Document").strip()
        raw_chars = int(
            document.get("raw_text_char_count")
            or document.get("available_raw_text_char_count")
            or len(str(document.get("full_text") or document.get("raw_text") or ""))
            or 0
        )
        if not document_id and not title:
            continue
        refs.append(
            {
                "document_id": document_id,
                "title": title,
                "source_label": str(document.get("source_label") or "").strip(),
                "source_type": str(document.get("source_type") or "").strip(),
                "raw_available": bool(raw_chars or document.get("raw_text_available")),
                "raw_text_available": bool(raw_chars or document.get("raw_text_available")),
                "raw_text_char_count": raw_chars,
                "follow_up_tool": "retrieve_document",
                "retrieve_document_call": {
                    "tool_name": "retrieve_document",
                    "arguments": {
                        "document_id": document_id,
                        "document_hint": title,
                        "query_text": title,
                        "include_raw_text": True,
                        "context_package_mode": "document_full",
                        "document_text_policy": "all_raw",
                    },
                },
                "reason": "derived_from_document_workspace_documents",
            }
        )
        if len(refs) >= limit:
            break
    return refs


def _repair_document_ref_contract_from_visible_refs(
    contract: dict[str, Any],
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(contract or {})
    current_count = max(
        int(payload.get("document_ref_count") or 0),
        int(payload.get("actionable_document_ref_count") or 0),
        int(payload.get("raw_available_document_ref_count") or 0),
    )
    if not refs or current_count >= len(refs):
        return payload
    actionable_count = sum(1 for ref in refs if str(ref.get("document_id") or "").strip())
    raw_available_count = sum(1 for ref in refs if _document_ref_raw_available(ref))
    return {
        **payload,
        "schema_version": payload.get("schema_version") or "agvm.document_ref_contract.v1",
        "state": "refs_ready",
        "document_ref_count": max(len(refs), int(payload.get("document_ref_count") or 0)),
        "actionable_document_ref_count": max(actionable_count, int(payload.get("actionable_document_ref_count") or 0)),
        "raw_available_document_ref_count": max(raw_available_count, int(payload.get("raw_available_document_ref_count") or 0)),
        "all_refs_actionable": bool(actionable_count == len(refs)),
        "raw_document_policy_options": payload.get("raw_document_policy_options") or ["refs_only", "top_raw", "all_raw"],
        "default_context_document_text_policy": payload.get("default_context_document_text_policy") or "refs_only",
        "exact_follow_up_recipe_required": True,
        "retrieve_document_requires_include_raw_text_for_raw": True,
        "repaired_from_visible_document_refs": True,
    }


def _repair_document_delivery_contract_from_visible_refs(
    contract: dict[str, Any],
    *,
    refs: list[dict[str, Any]],
    document_ref_contract: dict[str, Any],
    document_text_policy: str,
    document_bundle_payload: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(contract or {})
    if not refs:
        return payload
    current_count = max(
        int(payload.get("document_ref_count") or 0),
        int(payload.get("actionable_document_ref_count") or 0),
        int(payload.get("raw_available_document_ref_count") or 0),
    )
    if current_count >= len(refs):
        return payload
    raw_included_count = int(document_bundle_payload.get("document_count") or len(_as_list(document_bundle_payload.get("documents"))) or 0)
    raw_available_count = int(document_ref_contract.get("raw_available_document_ref_count") or 0)
    first_ref = refs[0]
    first_document_id = str(first_ref.get("document_id") or "<document_id_from_document_refs>")
    first_document_hint = str(first_ref.get("title") or first_ref.get("document_title") or "<document title or task>")
    return {
        **payload,
        "schema_version": payload.get("schema_version") or "agvm.document_delivery_contract.v1",
        "state": "raw_included" if raw_included_count else "refs_actionable",
        "document_text_policy": document_text_policy or payload.get("document_text_policy") or "refs_only",
        "primary_payload_field": payload.get("primary_payload_field") or "context_package.agent_markdown",
        "mcp_client_receives_first": (
            "context_package_plus_raw_document_bundle"
            if raw_included_count
            else "context_package_plus_actionable_document_refs"
        ),
        "raw_text_already_in_primary_payload": bool(raw_included_count),
        "raw_text_follow_up_required": bool(raw_available_count > raw_included_count),
        "document_ref_count": int(document_ref_contract.get("document_ref_count") or len(refs)),
        "actionable_document_ref_count": int(document_ref_contract.get("actionable_document_ref_count") or 0),
        "raw_available_document_ref_count": raw_available_count,
        "raw_included_document_count": raw_included_count,
        "raw_available_not_included_count": max(0, raw_available_count - raw_included_count),
        "document_bundle_state": document_bundle_payload.get("state") or payload.get("document_bundle_state") or "raw_unavailable",
        "document_bundle_document_count": raw_included_count,
        "document_bundle_raw_text_char_count": int(document_bundle_payload.get("raw_text_char_count") or 0),
        "all_refs_actionable": bool(document_ref_contract.get("all_refs_actionable", True)),
        "exact_follow_up_recipe": {
            "tool": "retrieve_document",
            "arguments": {
                "document_id": first_document_id,
                "document_hint": first_document_hint,
                "query_text": first_document_hint,
                "include_raw_text": True,
                "context_package_mode": "document_full",
                "document_text_policy": "all_raw",
            },
        },
        "repaired_from_visible_document_refs": True,
    }


def _dedupe_contract_items(values: list[Any], *, limit: int = 16) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _projection_truth_stale_for_ai_materialization(result: dict[str, Any], projection: dict[str, Any]) -> bool:
    if not projection:
        return True
    ai_materialization = _as_dict(result.get("ai_landing_materialization"))
    landing = _as_dict(ai_materialization.get("landing"))
    path = _as_dict(ai_materialization.get("path"))
    semantic_contract = _as_dict(ai_materialization.get("semantic_contract"))
    ai_route_materialized = bool(
        ai_materialization.get("route_level_materialized")
        or ai_materialization.get("materialized")
        or landing.get("materialized")
        or path.get("corridor_materialized")
        or semantic_contract.get("materialized")
    )
    if not ai_route_materialized:
        return False
    summary = _as_dict(projection.get("summary"))
    try:
        ai_landings = int(summary.get("ai_landings") or 0)
    except (TypeError, ValueError):
        ai_landings = 0
    if ai_landings > 0:
        return False
    return bool(landing.get("created_by_ai") or path.get("intent_created_by_ai") or semantic_contract.get("materialized"))


def _as_ms(value: Any) -> float | None:
    try:
        if value is None:
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    return round(numeric, 2)


def _first_ms(*values: Any) -> float | None:
    for value in values:
        numeric = _as_ms(value)
        if numeric is not None:
            return numeric
    return None


def _strip_raw_text(value: Any) -> Any:
    if isinstance(value, dict):
        stripped: dict[str, Any] = {}
        for key, child in value.items():
            if str(key) in _RAW_TEXT_KEYS and isinstance(child, str):
                stripped[key] = ""
                stripped[f"{key}_available"] = bool(child)
                stripped[f"{key}_char_count"] = len(child)
            else:
                stripped[key] = _strip_raw_text(child)
        return stripped
    if isinstance(value, list):
        return [_strip_raw_text(item) for item in value]
    return value


def _cap_raw_text(value: Any, *, limit: int = 12000) -> Any:
    bounded_limit = max(1000, int(limit or 12000))
    if isinstance(value, dict):
        capped: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text in _RAW_TEXT_KEYS and isinstance(child, str):
                full_text = child
                capped[key] = full_text[:bounded_limit]
                capped[f"{key_text}_available"] = bool(full_text)
                capped[f"{key_text}_char_count"] = len(full_text)
                capped[f"{key_text}_included_char_count"] = min(len(full_text), bounded_limit)
                capped[f"{key_text}_truncated"] = len(full_text) > bounded_limit
            else:
                capped[key] = _cap_raw_text(child, limit=bounded_limit)
        return capped
    if isinstance(value, list):
        return [_cap_raw_text(item, limit=bounded_limit) for item in value]
    return value


def _cap_source_trace_text(value: Any, *, limit: int = 12000, include_agent_markdown: bool = False) -> Any:
    bounded_limit = max(1000, int(limit or 12000))
    if isinstance(value, dict):
        capped: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text in {"text", "text_preview", "evidence_snippet"} or (
                include_agent_markdown and key_text == "agent_markdown"
            ):
                if not isinstance(child, str):
                    capped[key] = _cap_source_trace_text(
                        child,
                        limit=bounded_limit,
                        include_agent_markdown=include_agent_markdown,
                    )
                    continue
                capped[key] = child[:bounded_limit]
                capped[f"{key_text}_char_count"] = len(child)
                capped[f"{key_text}_included_char_count"] = min(len(child), bounded_limit)
                capped[f"{key_text}_truncated"] = len(child) > bounded_limit
            else:
                capped[key] = _cap_source_trace_text(
                    child,
                    limit=bounded_limit,
                    include_agent_markdown=include_agent_markdown,
                )
        return capped
    if isinstance(value, list):
        return [
            _cap_source_trace_text(
                item,
                limit=bounded_limit,
                include_agent_markdown=include_agent_markdown,
            )
            for item in value
        ]
    return value


def _context_package_exact_no_match(package: dict[str, Any]) -> bool:
    payload = _as_dict(package)
    metrics = _as_dict(payload.get("metrics"))
    contract = _as_dict(payload.get("contract"))
    exact_requirement_count = int(metrics.get("exact_field_requirement_count") or 0)
    exact_missing_count = int(metrics.get("exact_field_missing_count") or 0)
    if exact_requirement_count <= 0 or exact_missing_count < exact_requirement_count:
        return False
    missing_keys = [str(item or "") for item in _as_list(contract.get("semantic_missing_slot_keys"))]
    missing_text = " ".join(missing_keys + [str(item or "") for item in _as_list(contract.get("semantic_missing_descriptions"))]).lower()
    return bool("private_identifier" in missing_text or "exact" in missing_text or exact_missing_count > 0)


def _exact_no_match_mission_ledger(query_text: str, package: dict[str, Any]) -> dict[str, Any]:
    contract = _as_dict(package.get("contract"))
    missing_keys = [
        str(item).strip()
        for item in _as_list(contract.get("semantic_missing_slot_keys") or contract.get("missing_semantic_slots"))
        if str(item).strip()
    ]
    return {
        "schema_version": "agvm.mission_evidence_ledger.v1",
        "row_schema_version": "agvm.mission_ledger_row.v1",
        "status": "exact_no_match",
        "query_text": query_text,
        "mission_count": 0,
        "row_count": 0,
        "resolved_count": 0,
        "partial_count": 0,
        "missed_count": 0,
        "hot_evidence_count": 0,
        "cold_evidence_count": 0,
        "document_ref_count": 0,
        "route_event_count": 0,
        "exact_no_match_boundary": True,
        "missing_semantic_slot_keys": missing_keys,
        "renderer_contract": {
            "package_builder_should_use_ledger_only": True,
            "package_builder_must_not_mine_raw_text": True,
            "exact_no_match_suppresses_adjacent_evidence": True,
        },
        "rows": [],
    }


def _exact_no_match_master_judgement(query_text: str, package: dict[str, Any]) -> dict[str, Any]:
    contract = _as_dict(package.get("contract"))
    metrics = _as_dict(package.get("metrics"))
    missing_keys = [
        str(item).strip()
        for item in _as_list(contract.get("semantic_missing_slot_keys") or contract.get("missing_semantic_slots"))
        if str(item).strip()
    ]
    missing_descriptions = [
        str(item).strip()
        for item in _as_list(contract.get("semantic_missing_descriptions"))
        if str(item).strip()
    ]
    cache_seed = "|".join([query_text, ",".join(missing_keys), ",".join(missing_descriptions)])
    cache_key = _sha256_text(cache_seed) or "exact_no_match"
    return {
        "schema_version": "agvm.master_judgement.v1",
        "master_judgement_id": f"master_exact_no_match_{cache_key[:8]}",
        "master_state": "no_match",
        "goal_coverage": [],
        "covered_goals": [],
        "partial_goals": [],
        "missing_goals": missing_keys or missing_descriptions or ["exact_requested_field"],
        "unresolved_goals": missing_keys or missing_descriptions or ["exact_requested_field"],
        "no_match_claim": True,
        "provider_state": "available",
        "context_state": "no_match",
        "document_state": "suppressed_for_exact_field_no_match",
        "path_state": "not_required",
        "answer_voice": "first_person",
        "agent_payload_state": "no_match",
        "final_seal_allowed": True,
        "terminal_for_client": True,
        "continuation_recommendation": {
            "state": "none",
            "tool_action": None,
            "reason": "exact_no_match_terminal_boundary",
        },
        "next_recommended_call": None,
        "expected_evidence_policy": {
            "schema_version": "agvm.master_expected_evidence_policy.v1",
            "query_scope": "exact_no_match",
            "expected_branch_count": 0,
            "minimum_resolved_branch_count": 0,
            "branch_completion_policy": "exact_private_or_missing_field_absence_is_terminal",
            "document_hydration_required": False,
            "path_truth_required": False,
            "evidence_budget_policy": "suppress_adjacent_evidence_for_exact_no_match",
            "static_required_sections_are_not_terminality_source": True,
            "ai_sufficiency_required": False,
            "safety_invariants": [
                "ai_participation",
                "privacy_and_off_contract_boundary",
                "document_hydration_contract",
                "budget",
            ],
        },
        "sufficiency_judge": {
            "schema_version": "agvm.master_sufficiency_judge.v1",
            "tier": "deterministic_precheck",
            "deterministic_precheck_state": "clear_terminal",
            "ai_sufficiency_required": False,
            "ai_sufficiency_state": "not_required",
            "ledger_hash": cache_key[:16],
            "does_not_scan_raw_documents": True,
            "does_not_use_hidden_package_rescue": True,
        },
        "branch_state_counts": {},
        "reason_codes": ["exact_no_match_boundary", "adjacent_evidence_suppressed"],
        "ledger_row_count": 0,
        "cache_key": cache_key[:16],
        "cache_hit": False,
        "master_judge_timing_ms": 0.0,
        "source": "exact_no_match_context_contract",
        "exact_field_requirement_count": int(metrics.get("exact_field_requirement_count") or 0),
        "exact_field_missing_count": int(metrics.get("exact_field_missing_count") or 0),
    }


def _exact_no_match_agent_markdown(package: dict[str, Any]) -> str:
    payload = _as_dict(package)
    contract = _as_dict(payload.get("contract"))
    metrics = _as_dict(payload.get("metrics"))
    missing_descriptions = [
        str(item).strip()
        for item in _as_list(contract.get("semantic_missing_descriptions"))
        if str(item).strip()
    ]
    missing_keys = [
        str(item).strip()
        for item in _as_list(contract.get("semantic_missing_slot_keys"))
        if str(item).strip()
    ]
    requested_fields: list[str] = []
    for description in missing_descriptions:
        match = re.search(r"Missing exact requested field:\s*(.+)$", description, flags=re.IGNORECASE)
        if match and match.group(1).strip():
            requested_fields.append(match.group(1).strip())
    if not requested_fields:
        requested_fields = [key.split(":", 1)[-1].replace("_", " ") for key in missing_keys if key]
    requested_fields = _dedupe_text_keep_order([field for field in requested_fields if field])[:6]
    query_text = str(payload.get("query_text") or "").strip()
    if not query_text:
        markdown = str(payload.get("agent_markdown") or "")
        match = re.search(r"## Task / User Intent\s+(.+?)(?:\n## |\Z)", markdown, flags=re.DOTALL)
        if match:
            query_text = " ".join(match.group(1).split())
    field_text = ", ".join(requested_fields) if requested_fields else "requested exact/private field"
    exact_required = int(metrics.get("exact_field_requirement_count") or 0)
    exact_missing = int(metrics.get("exact_field_missing_count") or 0)
    lines = [
        "# AGVM Context Package",
        "",
        "## Task / User Intent",
        query_text or "Exact private-field availability check.",
        "",
        "## Terminal No-Match Boundary",
        (
            "- The requested exact/private data is not present in the promoted memory package. "
            "AGVM must report the field as unavailable instead of inferring or fabricating it."
        ),
        f"- Requested field surface: {field_text}.",
        (
            f"- Exact-field contract: {exact_missing}/{exact_required or exact_missing or 1} "
            "required private/exact field(s) are missing from trusted memory."
        ),
        "",
        "## MCP Client Instruction",
        (
            "- Return a no-match / unavailable answer for the requested private data. "
            "Do not substitute adjacent biography, company facts, family facts, source snippets or document metadata."
        ),
        (
            "- If the user wants this value stored later, ask for an explicit user-provided source or a new Grow input; "
            "normal retrieval must not expose unrelated context for this exact-field request."
        ),
        "",
        "## Evidence Boundary",
        (
            "- Public context, hot memory, cold memory and document references are intentionally suppressed in this "
            "primary payload because they do not satisfy the requested exact/private field."
        ),
        (
            "- Spatial route traversal is not required for this terminal absence result once the semantic contract "
            "has identified the exact field and the package declares the no-match state."
        ),
    ]
    if missing_keys:
        lines.extend(
            [
                "",
                "## Missing Semantic Slots",
                *[f"- {key}" for key in missing_keys[:8]],
            ]
        )
    return "\n".join(lines)


def _redact_exact_no_match_context_package(package: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(_as_dict(package))
    metrics = dict(payload.get("metrics") or {})
    contract = dict(payload.get("contract") or {})
    exact_missing = int(metrics.get("exact_field_missing_count") or 0)
    exact_required = int(metrics.get("exact_field_requirement_count") or 0)
    payload["status"] = "no_match"
    payload["agent_markdown"] = _exact_no_match_agent_markdown(payload)
    payload["hot_sections"] = []
    payload["cold_sections"] = []
    payload["excluded_sections"] = []
    payload["document_refs"] = []
    payload["document_workspace"] = {}
    payload["document_bundle"] = {}
    payload["path_discoveries"] = []
    payload["source_trace"] = []
    metrics.update(
        {
            "hot_item_count": 0,
            "cold_item_count": 0,
            "excluded_item_count": 0,
            "document_count": 0,
            "document_workspace_document_count": 0,
            "document_workspace_full_text_document_count": 0,
            "document_workspace_raw_text_char_count": 0,
            "document_ref_count": 0,
            "actionable_document_ref_count": 0,
            "raw_available_document_ref_count": 0,
            "raw_included_document_count": 0,
            "raw_available_not_included_document_count": 0,
            "document_bundle_document_count": 0,
            "document_bundle_raw_text_char_count": 0,
            "exact_field_requirement_count": exact_required,
            "exact_field_missing_count": exact_missing,
            "package_breadth_state": "exact_field_no_match",
            "agent_body_char_count": len(str(payload.get("agent_markdown") or "")),
            "agent_markdown_chars": len(str(payload.get("agent_markdown") or "")),
            "agent_markdown_char_count": len(str(payload.get("agent_markdown") or "")),
        }
    )
    contract.update(
        {
            "passed": True,
            "no_match": True,
            "exact_field_no_match": True,
            "hot_context_policy": "suppressed_for_missing_exact_field",
        }
    )
    payload["metrics"] = metrics
    payload["contract"] = contract
    return payload


def _package_for_tool(tool_name: str, result: dict[str, Any]) -> tuple[str, Any]:
    if tool_name == "retrieve_context" or tool_name == "inspect_context_package":
        return "context_package", _as_dict(result.get("context_package"))
    if tool_name in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES:
        return "document_workspace", _result_document_workspace(result)
    if tool_name == "retrieve_path_corridor" or tool_name == "inspect_path_corridor":
        return "path_corridors", _as_dict(result.get("path_corridors"))
    if tool_name == "retrieve_source_trace":
        return "source_trace", _as_list(result.get("source_trace"))
    return "context_package", _as_dict(result.get("context_package"))


def _originating_mcp_tool_name(result: dict[str, Any]) -> str:
    payload = _as_dict(result)
    planner_runtime = _as_dict(payload.get("planner_runtime"))
    candidates = [
        payload.get("mcp_tool_name"),
        payload.get("tool_name"),
        planner_runtime.get("mcp_tool_name"),
        planner_runtime.get("mcp_entrypoint_tool"),
    ]
    for candidate in candidates:
        tool = str(candidate or "").strip()
        if tool:
            return tool
    return ""


def _effective_delivery_tool_name(tool_name: str, result: dict[str, Any]) -> str:
    normalized_tool = str(tool_name or "").strip()
    originating_tool = _originating_mcp_tool_name(result)
    if normalized_tool == "inspect_context_package" and originating_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES:
        return originating_tool
    return normalized_tool


def _document_tool_readiness(result: dict[str, Any]) -> dict[str, Any]:
    document_lookup = _as_dict(result.get("document_lookup"))
    document_workspace = _result_document_workspace(result)
    metrics = _as_dict(document_workspace.get("metrics"))
    documents = _as_list(document_workspace.get("documents"))
    document_count = int(metrics.get("document_count") or len(documents))
    full_text_document_count = int(
        metrics.get("full_text_document_count")
        or sum(1 for document in documents if str(_as_dict(document).get("full_text") or "").strip())
        or 0
    )
    primary_document_count = int(metrics.get("primary_document_count") or 0)
    primary_full_text_document_count = int(metrics.get("primary_full_text_document_count") or 0)
    raw_text_char_count = int(metrics.get("raw_text_char_count") or 0)
    primary_raw_text_char_count = int(metrics.get("primary_raw_text_char_count") or 0)
    workspace_status = str(document_workspace.get("status") or "").strip()
    workspace_kind = str(document_workspace.get("workspace_kind") or "").strip()
    lookup_kind = str(
        result.get("document_lookup_kind")
        or document_workspace.get("document_lookup_kind")
        or document_lookup.get("kind")
        or ""
    ).strip()
    workspace_ready = bool(workspace_status == "workspace_ready" and document_count > 0)
    exact_document_ready = bool(
        workspace_ready
        and workspace_kind == "exact_document"
        and lookup_kind == "exact_document_lookup"
        and (primary_full_text_document_count > 0 or full_text_document_count > 0)
    )
    document_tool_ready = bool(workspace_ready and (full_text_document_count > 0 or document_count > 0))
    return {
        "schema_version": "agvm.mcp_document_tool_readiness.v1",
        "state": "exact_document_ready" if exact_document_ready else "workspace_ready" if workspace_ready else workspace_status or "not_ready",
        "document_tool_ready": document_tool_ready,
        "exact_document_ready": exact_document_ready,
        "workspace_ready": workspace_ready,
        "workspace_status": workspace_status,
        "workspace_kind": workspace_kind,
        "document_lookup_kind": lookup_kind,
        "document_count": document_count,
        "full_text_document_count": full_text_document_count,
        "primary_document_count": primary_document_count,
        "primary_full_text_document_count": primary_full_text_document_count,
        "raw_text_char_count": raw_text_char_count,
        "primary_raw_text_char_count": primary_raw_text_char_count,
        "context_package_partial_does_not_block_document_tool": exact_document_ready,
    }


def _document_ref_has_hydration_recipe(ref: dict[str, Any]) -> bool:
    payload = _as_dict(ref)
    call = _as_dict(
        payload.get("retrieve_document_call")
        or payload.get("document_evidence_retrieve_document_call")
        or payload.get("exact_follow_up_recipe")
        or payload.get("follow_up_call")
    )
    arguments = _as_dict(call.get("arguments") or call.get("payload"))
    tool_name = str(call.get("tool_name") or call.get("tool") or payload.get("follow_up_tool") or "").strip()
    return bool(
        (tool_name == "retrieve_document" or not tool_name)
        and (
            str(arguments.get("document_id") or payload.get("document_id") or payload.get("anchor_node_id") or "").strip()
            or str(arguments.get("document_hint") or payload.get("title") or payload.get("document_title") or "").strip()
        )
    )


def _mission_ledger_certifies_document_evidence(ledger: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(ledger)
    rows = [
        _as_dict(row)
        for row in _as_list(payload.get("rows") or payload.get("entries"))
        if isinstance(row, dict)
    ]
    certifiable_states = {
        "accepted",
        "covered",
        "direct",
        "materialized",
        "promoted",
        "ready",
        "resolved",
        "satisfied",
        "sufficient",
    }
    document_rows = [row for row in rows if bool(row.get("document_evidence_row"))]
    certifiable_rows = [
        row
        for row in document_rows
        if (
            str(row.get("coverage_state") or row.get("status") or "").strip().lower()
            in certifiable_states
            or str(_as_dict(row.get("branch_judgement")).get("state") or "").strip().lower()
            in certifiable_states
        )
        and not bool(row.get("heuristic_support_only") or row.get("provisional_until_ai_spatial"))
    ]
    try:
        resolved_count = int(payload.get("resolved_count") or payload.get("covered_count") or payload.get("accepted_count") or 0)
    except (TypeError, ValueError):
        resolved_count = 0
    return {
        "schema_version": "agvm.document_evidence_mission_certification.v1",
        "certifiable": bool(certifiable_rows or (resolved_count > 0 and not document_rows)),
        "document_evidence_row_count": len(document_rows),
        "certifiable_document_evidence_row_count": len(certifiable_rows),
        "resolved_count": resolved_count,
    }


def _document_workspace_refs_terminality_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    target_document_need_contract: dict[str, Any],
    master_judgement: dict[str, Any],
    document_ref_contract: dict[str, Any],
    document_delivery_contract: dict[str, Any],
) -> dict[str, Any]:
    normalized_tool = str(tool_name or "").strip()
    context_package = _as_dict(result.get("context_package"))
    document_workspace = _result_document_workspace(result)
    workspace_metrics = _as_dict(document_workspace.get("metrics"))
    workspace_lane = _as_dict(document_workspace.get("document_evidence_lane"))
    refs = _visible_document_refs(
        result.get("document_refs"),
        context_package.get("document_refs"),
        document_workspace.get("document_refs"),
        document_workspace.get("primary_document_refs"),
        document_workspace.get("candidate_document_refs"),
        _as_list(workspace_lane.get("primary_document_refs")),
        _as_list(workspace_lane.get("candidate_document_refs")),
        _document_refs_from_workspace_documents(document_workspace),
    )

    def metric_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    classification = str(target_document_need_contract.get("classification") or "").strip()
    pure_document_evidence = bool(
        target_document_need_contract.get("pure_document_evidence")
        or classification == "pure_document_evidence"
        or (
            target_document_need_contract.get("document_evidence")
            and not target_document_need_contract.get("normal_context_required")
        )
    )
    workspace_surface = normalized_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES
    context_surface = normalized_tool in {"retrieve_context", "inspect_context_package"}
    eligible_surface = bool(workspace_surface or (context_surface and pure_document_evidence))

    document_ref_count = max(
        metric_int(document_ref_contract.get("document_ref_count")),
        metric_int(document_delivery_contract.get("document_ref_count")),
        len(refs),
    )
    actionable_count = max(
        metric_int(document_ref_contract.get("actionable_document_ref_count")),
        metric_int(document_delivery_contract.get("actionable_document_ref_count")),
        sum(1 for ref in refs if str(ref.get("document_id") or "").strip()),
    )
    raw_available_count = max(
        metric_int(document_ref_contract.get("raw_available_document_ref_count")),
        metric_int(document_delivery_contract.get("raw_available_document_ref_count")),
        sum(1 for ref in refs if _document_ref_raw_available(ref)),
    )
    primary_ref_count = max(
        metric_int(document_ref_contract.get("primary_document_ref_count")),
        metric_int(document_delivery_contract.get("primary_document_ref_count")),
        metric_int(workspace_metrics.get("primary_document_ref_count")),
        metric_int(workspace_metrics.get("primary_document_count")),
        len(_as_list(document_workspace.get("primary_document_refs"))),
        len(_as_list(workspace_lane.get("primary_document_refs"))),
        sum(1 for ref in refs if str(ref.get("relationship_to_query") or "").strip() == "primary"),
    )
    primary_ref_inferred_from_ranked_refs = False
    exact_recipe_count = sum(1 for ref in refs if _document_ref_has_hydration_recipe(ref))
    exact_recipe = _as_dict(document_delivery_contract.get("exact_follow_up_recipe"))
    exact_recipe_args = _as_dict(exact_recipe.get("arguments"))
    if (
        str(exact_recipe.get("tool") or exact_recipe.get("tool_name") or "").strip() == "retrieve_document"
        and (
            str(exact_recipe_args.get("document_id") or "").strip()
            or str(exact_recipe_args.get("document_hint") or "").strip()
        )
    ):
        exact_recipe_count = max(1, exact_recipe_count)

    all_refs_actionable = bool(
        document_ref_count > 0
        and actionable_count >= min(document_ref_count, max(1, len(refs)))
        and bool(document_ref_contract.get("all_refs_actionable", True))
    )
    refs_actionable = bool(document_ref_count > 0 and actionable_count > 0 and all_refs_actionable)
    materialization = _as_dict(result.get("context_package_materialization"))
    result_status = str(result.get("status") or "").strip().lower()
    result_materialization_state = str(
        result.get("result_materialization_state")
        or materialization.get("state")
        or ""
    ).strip()
    background_running_states = {
        "first_package_ready_background_running",
        "first_partial_package_background_running",
        "first_document_workspace_ready_background_ai_running",
        "first_document_payload_ready_background_running",
        "snapshot_ready",
    }
    background_cap = _as_dict(
        result.get("mcp_background_cap")
        or _as_dict(result.get("mcp_delivery_contract")).get("background_cap")
    )
    background_cap_requested = bool(background_cap.get("requested"))
    materialization_pending_flag = bool(
        result.get("final_materialization_pending")
        or materialization.get("final_materialization_pending")
    )
    background_running_state = result_materialization_state in background_running_states
    package_materialization_state = str(materialization.get("state") or "").strip()
    mature_ranked_ref_surface = bool(
        document_ref_count >= 3
        or package_materialization_state in {"context_ready", "context_partial", "document_refs_ready", "finalized"}
        or (
            not package_materialization_state.startswith("first_")
            and result_materialization_state in {"context_ready", "context_partial", "document_refs_ready", "finalized"}
        )
        or bool(materialization.get("terminal") or materialization.get("terminal_for_mcp_client"))
    )
    if (
        primary_ref_count <= 0
        and workspace_surface
        and document_ref_count > 0
        and actionable_count > 0
        and raw_available_count > 0
        and mature_ranked_ref_surface
    ):
        primary_ref_count = min(document_ref_count, actionable_count, raw_available_count, 1)
        primary_ref_inferred_from_ranked_refs = True
    raw_availability_known = bool(raw_available_count > 0 and raw_available_count >= min(actionable_count, max(1, primary_ref_count)))
    exact_hydration_ready = bool(exact_recipe_count > 0 and exact_recipe_count >= min(primary_ref_count or 1, actionable_count or 1))
    primary_ready = bool(primary_ref_count > 0)
    partial_document_ref_target = 10
    terminal_ref_surface_mature = bool(
        result_status == "ok"
        or document_ref_count >= partial_document_ref_target
        or package_materialization_state in {"context_ready", "document_refs_ready", "finalized"}
        or bool(materialization.get("contract_passed"))
        or bool(materialization.get("terminal") or materialization.get("terminal_for_mcp_client"))
    )

    planner_runtime = _as_dict(result.get("planner_runtime"))
    ledger = _as_dict(result.get("mission_evidence_ledger") or planner_runtime.get("mission_evidence_ledger"))
    ledger_certification = _mission_ledger_certifies_document_evidence(ledger)
    master_state = str(master_judgement.get("master_state") or "").strip()
    master_document_state = str(master_judgement.get("document_state") or "").strip()
    master_certifies = bool(
        master_state in {"terminal", "no_match"}
        or master_document_state in {
            "document_evidence_sufficient",
            "raw_refs_ready",
            "document_refs_ready",
        }
        or bool(master_judgement.get("document_evidence_sufficient"))
        or bool(ledger_certification.get("certifiable"))
        or str(result.get("stop_reason") or "").strip() == "document_evidence_sufficient"
    )

    refs_ready = bool(
        eligible_surface
        and document_ref_count > 0
        and refs_actionable
        and raw_availability_known
        and exact_hydration_ready
        and primary_ready
    )
    certified_refs_ready = bool(refs_ready and master_certifies and terminal_ref_surface_mature)
    document_workspace_background_pending = bool(
        (result_status == "partial" and not certified_refs_ready)
        or (background_running_state and not certified_refs_ready)
        or (background_cap_requested and materialization_pending_flag)
        or (
            materialization_pending_flag
            and not certified_refs_ready
            and result_status not in {"ok", "no_match"}
        )
    )
    terminal_for_client = bool(certified_refs_ready and not document_workspace_background_pending)
    missing: list[str] = []
    if not eligible_surface:
        missing.append("document_workspace_surface_not_requested")
    if workspace_surface and not pure_document_evidence:
        pure_document_evidence = True
    if document_ref_count <= 0:
        missing.append("document_refs_missing")
    if not refs_actionable:
        missing.append("actionable_document_refs_missing")
    if not primary_ready:
        missing.append("primary_document_ref_missing")
    if not raw_availability_known:
        missing.append("raw_availability_not_known_for_primary_refs")
    if not exact_hydration_ready:
        missing.append("retrieve_document_hydration_recipe_missing")
    if not master_certifies:
        missing.append("document_evidence_not_master_or_mission_certified")
    if refs_ready and master_certifies and not terminal_ref_surface_mature:
        missing.append("document_ref_surface_not_mature")
    if document_workspace_background_pending:
        missing.append("document_workspace_background_completion_pending")

    return {
        "schema_version": "agvm.document_workspace_refs_terminality.v1",
        "eligible_surface": eligible_surface,
        "workspace_surface": workspace_surface,
        "context_surface": context_surface,
        "pure_document_evidence": pure_document_evidence,
        "refs_ready": refs_ready,
        "terminal_for_client": terminal_for_client,
        "state": "document_refs_ready" if terminal_for_client else "refs_ready_waiting_certification" if refs_ready else "refs_not_ready",
        "document_ref_count": document_ref_count,
        "actionable_document_ref_count": actionable_count,
        "raw_available_document_ref_count": raw_available_count,
        "primary_document_ref_count": primary_ref_count,
        "primary_ref_inferred_from_ranked_refs": primary_ref_inferred_from_ranked_refs,
        "exact_hydration_recipe_count": exact_recipe_count,
        "raw_follow_up_is_next_call_not_blocker": terminal_for_client,
        "master_certifies_document_evidence": master_certifies,
        "terminal_ref_surface_mature": terminal_ref_surface_mature,
        "partial_document_ref_target": partial_document_ref_target,
        "background_completion_pending": document_workspace_background_pending,
        "background_cap_requested": background_cap_requested,
        "materialization_pending_flag": materialization_pending_flag,
        "background_running_state": background_running_state,
        "master_state": master_state or None,
        "master_document_state": master_document_state or None,
        "mission_certification": ledger_certification,
        "missing_reasons": _dedupe_contract_items(missing, limit=12),
    }


def _path_tool_has_visible_route_truth(result: dict[str, Any]) -> bool:
    path_corridors = _as_dict(result.get("path_corridors"))
    if not path_corridors:
        return False
    path_metrics = _as_dict(path_corridors.get("metrics"))
    path_storage_contract = _as_dict(result.get("path_tool_storage_contract"))
    if bool(path_storage_contract.get("mission_surface_missing") or path_metrics.get("mission_surface_missing")):
        return False
    paths = [
        path
        for path in _as_list(path_corridors.get("paths") or path_corridors.get("corridors"))
        if isinstance(path, dict)
    ]
    embedded_route_event_count = sum(len(_as_list(path.get("route_events"))) for path in paths)
    route_trace = _as_dict(result.get("route_trace"))
    route_trace_events = _as_list(route_trace.get("branch_route_events") or route_trace.get("events"))
    visible_route_event_count = max(embedded_route_event_count, len(route_trace_events))
    try:
        metric_route_event_count = int(path_metrics.get("route_event_count") or 0)
    except (TypeError, ValueError):
        metric_route_event_count = 0
    if (
        str(path_metrics.get("route_event_count_source") or "") == "route_trace"
        and visible_route_event_count <= 0
    ):
        route_event_count = 0
    else:
        try:
            route_event_count = int(
                metric_route_event_count
                or embedded_route_event_count
                or path_metrics.get("traversed_count")
                or path_metrics.get("route_step_count")
                or len(route_trace_events)
                or 0
            )
        except (TypeError, ValueError):
            route_event_count = 0
    try:
        path_count = int(
            path_metrics.get("path_count")
            or path_metrics.get("planned_path_count")
            or path_metrics.get("planned_corridor_count")
            or len(paths)
            or 0
        )
    except (TypeError, ValueError):
        path_count = 0
    return bool(path_count > 0 and route_event_count > 0)


def _path_tool_certifiable_mission_surface(
    mission_evidence_ledger: dict[str, Any],
    path_mission_contract: dict[str, Any],
) -> dict[str, Any]:
    def metric_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    mission_status = str(path_mission_contract.get("status") or "").strip().lower()
    mission_count = metric_int(path_mission_contract.get("mission_count"))
    contract_materialized = bool(
        path_mission_contract.get("materialized")
        and mission_count > 0
        and mission_status not in {"blocked", "failed", "missing"}
    )
    resolved_count = max(
        metric_int(mission_evidence_ledger.get("resolved_count")),
        metric_int(mission_evidence_ledger.get("covered_count")),
        metric_int(mission_evidence_ledger.get("accepted_count")),
    )
    rows = [
        row
        for row in _as_list(mission_evidence_ledger.get("rows") or mission_evidence_ledger.get("entries"))
        if isinstance(row, dict)
    ]
    certifiable_states = {
        "accepted",
        "covered",
        "direct",
        "materialized",
        "promoted",
        "ready",
        "resolved",
        "satisfied",
        "sufficient",
    }
    forbidden_states = {
        "blocked",
        "excluded",
        "excluded_only",
        "failed",
        "forbidden",
        "missing",
        "rejected",
        "unresolved",
    }
    certifiable_rows = 0
    forbidden_rows = 0
    provisional_rows = 0
    for row in rows:
        coverage_state = str(row.get("coverage_state") or row.get("status") or "").strip().lower()
        judgement = _as_dict(row.get("branch_judgement"))
        judgement_state = str(judgement.get("state") or "").strip().lower()
        accepted = bool(
            row.get("accepted_by_ai_or_master")
            or row.get("accepted")
            or judgement.get("accepted")
            or coverage_state in certifiable_states
            or judgement_state in certifiable_states
        )
        forbidden = bool(
            coverage_state in forbidden_states
            or judgement_state in forbidden_states
            or str(row.get("missing_reason") or "").strip()
        )
        heuristic_only = bool(row.get("heuristic_support_only") or row.get("provisional_until_ai_spatial"))
        if accepted and not forbidden and not heuristic_only:
            certifiable_rows += 1
        elif forbidden:
            forbidden_rows += 1
        elif heuristic_only:
            provisional_rows += 1
    legacy_counter_only_certifiable = bool(resolved_count > 0 and not rows)
    certifiable = bool(contract_materialized or certifiable_rows > 0 or legacy_counter_only_certifiable)
    missing_reason = None
    if not certifiable:
        if mission_status in {"blocked", "failed", "missing"}:
            missing_reason = f"path_mission_contract_{mission_status}"
        elif resolved_count > 0 and rows and certifiable_rows <= 0:
            missing_reason = "mission_ledger_resolved_rows_not_ai_or_master_accepted"
        elif forbidden_rows > 0 and certifiable_rows <= 0 and resolved_count <= 0:
            missing_reason = "mission_ledger_only_forbidden_or_excluded_rows"
        elif provisional_rows > 0 and certifiable_rows <= 0 and resolved_count <= 0:
            missing_reason = "mission_ledger_only_provisional_rows"
        else:
            missing_reason = "mission_ledger_has_no_certifiable_rows"
    return {
        "schema_version": "agvm.path_route_first_mission_surface_certification.v1",
        "certifiable": certifiable,
        "missing_reason": missing_reason,
        "path_mission_contract_status": mission_status or None,
        "path_mission_contract_materialized": contract_materialized,
        "path_mission_count": mission_count,
        "mission_resolved_count": resolved_count,
        "legacy_counter_only_certifiable": legacy_counter_only_certifiable,
        "mission_row_count": max(metric_int(mission_evidence_ledger.get("row_count")), len(rows)),
        "certifiable_row_count": certifiable_rows,
        "forbidden_row_count": forbidden_rows,
        "provisional_row_count": provisional_rows,
    }


def _path_tool_route_first_sufficiency_contract(result: dict[str, Any]) -> dict[str, Any]:
    path_corridors = _as_dict(result.get("path_corridors"))
    path_metrics = _as_dict(path_corridors.get("metrics"))
    path_storage_contract = _as_dict(result.get("path_tool_storage_contract"))
    planner_runtime = _as_dict(result.get("planner_runtime"))
    mission_evidence_ledger = _as_dict(
        result.get("mission_evidence_ledger") or planner_runtime.get("mission_evidence_ledger")
    )
    path_mission_contract = _as_dict(
        result.get("path_mission_contract") or planner_runtime.get("path_mission_contract")
    )

    def metric_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    paths = [
        path
        for path in _as_list(path_corridors.get("paths") or path_corridors.get("corridors"))
        if isinstance(path, dict)
    ]
    embedded_route_event_count = sum(len(_as_list(path.get("route_events"))) for path in paths)
    route_trace = _as_dict(result.get("route_trace"))
    route_trace_events = _as_list(route_trace.get("branch_route_events") or route_trace.get("events"))
    visible_route_event_count = max(embedded_route_event_count, len(route_trace_events))
    if str(path_metrics.get("route_event_count_source") or "") == "route_trace" and visible_route_event_count <= 0:
        route_event_count = 0
    else:
        route_event_count = max(
            metric_int(path_metrics.get("route_event_count")),
            embedded_route_event_count,
            metric_int(path_metrics.get("traversed_count")),
            metric_int(path_metrics.get("route_step_count")),
            len(route_trace_events),
        )
    path_count = max(
        metric_int(path_metrics.get("path_count")),
        metric_int(path_metrics.get("planned_path_count")),
        metric_int(path_metrics.get("planned_corridor_count")),
        len(paths),
    )
    terminal_path_count = max(
        metric_int(path_metrics.get("terminal_path_count")),
        metric_int(path_metrics.get("completed_path_count")),
        metric_int(path_metrics.get("completed_corridor_count")),
    )
    pending_path_count = metric_int(path_metrics.get("pending_path_count"))
    mission_ledger_row_count = max(
        metric_int(path_storage_contract.get("mission_ledger_row_count")),
        metric_int(mission_evidence_ledger.get("row_count")),
    )
    mission_surface_certification = _path_tool_certifiable_mission_surface(
        mission_evidence_ledger,
        path_mission_contract,
    )
    mission_ledger_present = bool(path_storage_contract.get("mission_ledger_ready") or mission_ledger_row_count > 0)
    mission_ledger_ready = bool(mission_ledger_present and mission_surface_certification.get("certifiable"))
    mission_surface_missing = bool(
        path_storage_contract.get("mission_surface_missing") or path_metrics.get("mission_surface_missing")
    )
    route_truth_ready = bool(
        path_storage_contract.get("route_truth_ready")
        or path_storage_contract.get("route_truth_ready_from_path")
        or (
            path_count > 0
            and route_event_count > 0
            and visible_route_event_count > 0
            and (
                terminal_path_count > 0
                or pending_path_count == 0
                or bool(mission_surface_certification.get("certifiable"))
            )
        )
    )
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    ai_landing_materialization = _as_dict(
        result.get("ai_landing_materialization") or planner_runtime.get("ai_landing_materialization")
    )
    semantic_ai_materialized = bool(
        semantic_runtime.get("material")
        or _as_dict(ai_landing_materialization.get("semantic_contract")).get("materialized")
    )
    branch_materialization = _as_dict(ai_landing_materialization.get("branch"))
    path_materialization = _as_dict(ai_landing_materialization.get("path"))
    semantic_ai_route_materialized = bool(
        ai_landing_materialization.get("route_level_materialized")
        or ai_landing_materialization.get("materialized")
        or ai_landing_materialization.get("semantic_route_candidate_materialized")
        or branch_materialization.get("materialized")
        or path_materialization.get("ai_compiled_path_materialized")
        or path_materialization.get("corridor_materialized")
    )
    visible_route_truth = _path_tool_has_visible_route_truth(result)
    present = bool(
        visible_route_truth
        and route_truth_ready
        and mission_ledger_ready
        and not mission_surface_missing
        and semantic_ai_materialized
        and semantic_ai_route_materialized
    )
    return {
        "schema_version": "agvm.path_route_first_sufficiency_contract.v1",
        "present": present,
        "reason": "visible_route_truth_with_ai_semantic_route_and_mission_ledger" if present else None,
        "visible_route_truth": visible_route_truth,
        "route_truth_ready": route_truth_ready,
        "mission_surface_missing": mission_surface_missing,
        "mission_surface_certifiable": bool(mission_surface_certification.get("certifiable")),
        "mission_surface_certification": mission_surface_certification,
        "mission_ledger_ready": mission_ledger_ready,
        "mission_ledger_present": mission_ledger_present,
        "mission_ledger_row_count": mission_ledger_row_count,
        "path_count": path_count,
        "route_event_count": route_event_count,
        "visible_route_event_count": visible_route_event_count,
        "terminal_path_count": terminal_path_count,
        "pending_path_count": pending_path_count,
        "semantic_ai_materialized": semantic_ai_materialized,
        "semantic_ai_route_materialized": semantic_ai_route_materialized,
        "spatial_contract_can_be_deferred": present,
        "metric_only_route_truth_rejected": bool(
            str(path_metrics.get("route_event_count_source") or "") == "route_trace"
            and visible_route_event_count <= 0
        ),
    }


def _status_for_tool(tool_name: str, result: dict[str, Any], package: Any) -> str:
    document_lookup = _as_dict(result.get("document_lookup"))
    document_workspace = _result_document_workspace(result)
    document_readiness = _document_tool_readiness(result)
    planner_runtime = _as_dict(result.get("planner_runtime"))
    runtime_boundary = _as_dict(result.get("mcp_runtime_boundary") or planner_runtime.get("mcp_runtime_boundary"))
    context_package_materialization = _as_dict(result.get("context_package_materialization"))
    nonblocking_pending = bool(
        result.get("final_materialization_pending")
        or context_package_materialization.get("final_materialization_pending")
        or (
            runtime_boundary.get("nonblocking_first_package_returned")
            and str(result.get("result_materialization_state") or runtime_boundary.get("result_materialization_state") or "")
            in {"first_package_ready_background_running", "snapshot_ready", ""}
            and not bool(result.get("result_ready_terminal"))
        )
    )
    ai_hard_gate = _as_dict(result.get("ai_materialization_hard_gate") or planner_runtime.get("ai_materialization_hard_gate"))
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    ai_landing_materialization = _as_dict(result.get("ai_landing_materialization") or planner_runtime.get("ai_landing_materialization"))
    semantic_provider = _semantic_provider_state(semantic_runtime)
    semantic_provider_degraded = bool(semantic_provider.get("degraded"))
    semantic_ai_required = bool(
        semantic_runtime.get("ai_required")
        or ai_hard_gate.get("required")
        or ai_landing_materialization.get("required")
    )
    semantic_material_raw = bool(semantic_runtime.get("material"))
    route_material = bool(
        ai_landing_materialization.get("materialized")
        or ai_landing_materialization.get("route_level_materialized")
    )
    semantic_ai_state = _ai_materialization_state(
        ai_required=semantic_ai_required,
        semantic_material=semantic_material_raw,
        route_material=route_material,
        hard_gate_satisfied=_ai_hard_gate_satisfied(ai_hard_gate),
        gate_blocked=bool(ai_hard_gate.get("blocked")),
        semantic_runtime=semantic_runtime,
        provider_state=semantic_provider,
    )
    semantic_ai_material = bool(semantic_ai_state.get("certifiable"))
    answer_demo_materialization = _as_dict(result.get("answer_demo_materialization") or planner_runtime.get("answer_demo_materialization"))
    answer_demo_requested = bool(answer_demo_materialization.get("requested"))
    answer_demo_state = str(answer_demo_materialization.get("state") or "").strip()
    answer_surface_requested = str(result.get("response_mode") or planner_runtime.get("response_mode") or "").strip() in {"answer", "both"}
    context_package = _as_dict(result.get("context_package"))
    context_contract = _as_dict(context_package.get("contract"))
    context_metrics = _as_dict(context_package.get("metrics"))
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    exact_no_match_ai_exception = bool(
        semantic_runtime.get("exact_no_match_ai_exception")
        and semantic_runtime.get("material")
    )
    exact_field_no_match_status = bool(
        tool_name in {"retrieve_context", "inspect_context_package"}
        and (semantic_ai_material or exact_no_match_ai_exception)
        and int(context_metrics.get("exact_field_requirement_count") or 0) > 0
        and int(context_metrics.get("exact_field_missing_count") or 0)
        >= int(context_metrics.get("exact_field_requirement_count") or 0)
    )
    if exact_field_no_match_status:
        return "no_match"
    answer_context_alignment = _as_dict(context_contract.get("answer_context_alignment"))
    stop_reason = str(result.get("stop_reason") or "").strip()
    ai_spatial_landing_contract = _as_dict(
        result.get("ai_spatial_landing_contract") or planner_runtime.get("ai_spatial_landing_contract")
    )
    ai_spatial_contract_observed = bool(
        ai_spatial_landing_contract
        and ("ai_spatial_landing_contract" in result or "ai_spatial_landing_contract" in planner_runtime)
    )
    ai_spatial_materialized = bool(
        ai_spatial_landing_contract.get("certifiable")
        or ai_spatial_landing_contract.get("materialized")
    )
    ai_spatial_missing_reasons = {
        str(item or "").strip()
        for item in _as_list(ai_spatial_landing_contract.get("missing_reasons"))
        if str(item or "").strip()
    }
    ai_spatial_deferred = bool(
        _ai_spatial_pending_or_retryable(
            ai_spatial_landing_contract,
            observed=ai_spatial_contract_observed,
            ai_required=semantic_ai_required,
            materialized=ai_spatial_materialized,
        )
    )
    if tool_name in {"retrieve_path_corridor", "inspect_path_corridor"} and bool(
        _path_tool_route_first_sufficiency_contract(result).get("present")
    ):
        return "ok"
    if tool_name in {"retrieve_context", "inspect_context_package"} and semantic_ai_material and ai_spatial_deferred:
        return "partial"
    if (
        tool_name in {"retrieve_context", "inspect_context_package"}
        and semantic_ai_required
        and ai_spatial_contract_observed
        and not ai_spatial_materialized
        and not ai_spatial_deferred
    ):
        return "blocked" if package else "partial"
    if bool(ai_hard_gate.get("blocked")) or stop_reason in {
        "blocked_ai_material_missing",
        "blocked_ai_final_judge_missing",
        "blocked_context_package_unapproved",
        "blocked_answer_demo_not_approved",
    }:
        if tool_name in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES and bool(document_readiness.get("document_tool_ready")):
            return "ok"
        return "blocked"
    if semantic_provider_degraded and semantic_ai_required and not semantic_ai_material:
        if tool_name in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES and bool(document_readiness.get("document_tool_ready")):
            return "ok"
        return "blocked"
    if tool_name in {"retrieve_context", "inspect_context_package"} and semantic_ai_required and not semantic_ai_material:
        if nonblocking_pending and package and bool(context_contract.get("passed", False)):
            return "partial"
        if package and str(semantic_ai_state.get("state") or "") == "ai_pending":
            return "partial"
        return "blocked" if package else "partial"
    if semantic_provider_degraded and semantic_ai_required:
        return "partial"
    if (
        answer_demo_requested
        and bool(answer_context_alignment.get("checked"))
        and not bool(answer_context_alignment.get("passed", True))
    ):
        return "blocked"
    if tool_name in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES:
        if (
            str(result.get("document_lookup_kind") or "") == "no_document_found"
            or str(document_lookup.get("kind") or "") == "no_document_found"
            or str(document_workspace.get("workspace_kind") or "") == "no_document_found"
            or str(document_workspace.get("status") or "") == "no_document_found"
        ):
            return "no_match"
        if bool(document_readiness.get("document_tool_ready")):
            return "ok"
    if tool_name in {"retrieve_path_corridor", "inspect_path_corridor"}:
        if _path_tool_has_visible_route_truth(result):
            if (
                semantic_ai_required
                and ai_spatial_contract_observed
                and not ai_spatial_materialized
            ):
                return "partial" if ai_spatial_deferred else "blocked"
            if semantic_ai_required and not semantic_ai_material:
                return "partial" if package else "blocked"
            return "ok"
    if tool_name in {"retrieve_context", "inspect_context_package"}:
        exact_requirement_count = int(context_metrics.get("exact_field_requirement_count") or 0)
        exact_missing_count = int(context_metrics.get("exact_field_missing_count") or 0)
        if exact_requirement_count > 0 and exact_missing_count >= exact_requirement_count:
            return "no_match"
        selected_materialization_ready = bool(
            context_package_materialization.get("contract_passed")
            and not context_package_materialization.get("final_materialization_pending")
            and str(context_package.get("agent_markdown") or "").strip()
            and not _as_list(context_contract.get("unresolved_sections"))
            and not _as_list(context_contract.get("semantic_missing_slot_keys") or context_contract.get("missing_semantic_slots"))
        )
        if context_contract and not bool(context_contract.get("passed", False)) and selected_materialization_ready:
            return "ok"
        if context_contract and not bool(context_contract.get("passed", False)):
            if _as_list(context_contract.get("unresolved_sections")) and _selected_context_package_soft_unresolved_sections_allowed(
                result,
                context_package=context_package,
            ):
                return "ok"
            return "partial"
        if context_contract and bool(context_contract.get("passed", False)):
            if nonblocking_pending and not bool(runtime_boundary.get("nonblocking_first_package_returned")):
                return "partial"
            if answer_surface_requested and answer_demo_requested and answer_demo_state and answer_demo_state not in {"ready", "not_requested"}:
                return "partial"
            return "ok"
    if nonblocking_pending:
        return "partial"
    if tool_name == "retrieve_source_trace" and not package:
        return "no_match"
    if isinstance(package, dict) and not package:
        return "partial" if _as_list(result.get("matches")) else "no_match"
    if isinstance(package, list) and not package:
        return "partial" if _as_list(result.get("matches")) else "no_match"
    if answer_surface_requested and answer_demo_requested and answer_demo_state and answer_demo_state not in {"ready", "not_requested"}:
        return "partial"
    if answer_demo_requested and not bool(result.get("final_closure_ready")):
        return "partial"
    if str(result.get("answerability_state") or "") in {"insufficient", "partial"}:
        return "partial"
    return "ok"


def _collect_node_ids(value: Any) -> set[str]:
    node_ids: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {"node_id", "source_node_id", "memory_node_id", "anchor_node_id"} and str(child or "").strip():
                node_ids.add(str(child).strip())
            node_ids.update(_collect_node_ids(child))
    elif isinstance(value, list):
        for child in value:
            node_ids.update(_collect_node_ids(child))
    return node_ids


def _payload_integrity(result: dict[str, Any], *, tool_name: str, package_field: str, package: Any) -> dict[str, Any]:
    answer = _as_dict(result.get("answer"))
    context_package = _as_dict(result.get("context_package"))
    context_contract = _as_dict(context_package.get("contract"))
    answer_context_alignment = _as_dict(context_contract.get("answer_context_alignment"))
    evidence_ids = [
        str(item).strip()
        for item in list(answer.get("evidence_node_ids") or [])
        if str(item).strip()
    ]
    package_node_ids = _collect_node_ids(package)
    missing_ids = [
        node_id
        for node_id in evidence_ids
        if node_id not in package_node_ids
    ]
    alignment_missing_ids = [
        str(item).strip()
        for item in list(answer_context_alignment.get("missing_evidence_node_ids") or [])
        if str(item).strip()
    ]
    passed = not missing_ids and not alignment_missing_ids and bool(answer_context_alignment.get("passed", True))
    return {
        "schema_version": "agvm.mcp_payload_integrity.v1",
        "tool_name": tool_name,
        "package_field": package_field,
        "context_package_is_product": True,
        "answer_demo_secondary": True,
        "answer_support_node_count": len(evidence_ids),
        "package_node_id_count": len(package_node_ids),
        "answer_support_node_ids_missing_from_package": missing_ids[:16],
        "contract_missing_evidence_node_ids": alignment_missing_ids[:16],
        "answer_context_alignment_checked": bool(answer_context_alignment.get("checked")),
        "answer_context_alignment_passed": bool(answer_context_alignment.get("passed", True)),
        "passed": bool(passed),
    }


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value or "")


def _mission_ledger_effective_branch_counts(ledger: dict[str, Any]) -> dict[str, int]:
    rows = [row for row in _as_list(_as_dict(ledger).get("rows")) if isinstance(row, dict)]
    counts: dict[str, int] = {}
    for row in rows:
        branch_judgement = _as_dict(row.get("branch_judgement"))
        state = str(branch_judgement.get("state") or row.get("coverage_state") or "unknown").strip().lower() or "unknown"
        counts[state] = counts.get(state, 0) + 1
    if counts:
        return counts
    raw_counts = _as_dict(ledger.get("branch_judge_state_counts") or ledger.get("branch_state_counts"))
    output: dict[str, int] = {}
    for key, value in raw_counts.items():
        state = str(key or "").strip().lower()
        if not state:
            continue
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            output[state] = count
    return output


def _master_judgement_matches_ledger(master_judgement: dict[str, Any], ledger: dict[str, Any]) -> bool:
    if not _as_dict(ledger):
        return bool(master_judgement)
    rows = [row for row in _as_list(ledger.get("rows")) if isinstance(row, dict)]
    expected_row_count = len(rows) if rows else int(ledger.get("row_count") or 0)
    if expected_row_count <= 0:
        return bool(master_judgement)
    if not master_judgement:
        return False
    try:
        master_row_count = int(master_judgement.get("ledger_row_count") or 0)
    except (TypeError, ValueError):
        master_row_count = 0
    if master_row_count != expected_row_count:
        return False
    expected_counts = _mission_ledger_effective_branch_counts(ledger)
    if not expected_counts:
        return True
    raw_master_counts = _as_dict(master_judgement.get("branch_state_counts"))
    master_counts: dict[str, int] = {}
    for key, value in raw_master_counts.items():
        state = str(key or "").strip().lower()
        if not state:
            continue
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            master_counts[state] = count
    return master_counts == expected_counts


def _package_render_contract_for_master(package_payload: Any, context_package: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(package_payload)
    payload_metrics = _as_dict(payload.get("metrics"))
    context_metrics = _as_dict(context_package.get("metrics"))
    return _as_dict(
        payload.get("package_render_contract")
        or payload_metrics.get("package_render_contract")
        or context_package.get("package_render_contract")
        or context_metrics.get("package_render_contract")
    )


def _path_truth_contract_for_master(package_payload: Any, context_package: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(package_payload)
    return _as_dict(
        _as_dict(payload.get("contract")).get("path_truth")
        or payload.get("path_truth_contract")
        or _as_dict(context_package.get("contract")).get("path_truth")
        or context_package.get("path_truth_contract")
    )


def _query_text_for_master(result: dict[str, Any], context_package: dict[str, Any]) -> str:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    query_text = str(
        result.get("query_text")
        or planner_runtime.get("query_text")
        or context_package.get("query_text")
        or ""
    ).strip()
    if query_text:
        return query_text
    agent_markdown = str(context_package.get("agent_markdown") or "").strip()
    match = re.search(r"(?im)^##\s*Task\s*/\s*User\s*Intent\s*\n(?P<query>.+?)(?:\n##|\Z)", agent_markdown)
    if match:
        return " ".join(match.group("query").strip().split())
    return ""


def _repair_master_judgement_from_delivery_ledger(
    normalized: dict[str, Any],
    *,
    package_payload: Any,
    visible_document_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    context_package = _as_dict(normalized.get("context_package"))
    mission_evidence_ledger = _as_dict(
        normalized.get("mission_evidence_ledger")
        or planner_runtime.get("mission_evidence_ledger")
    )
    existing_master = _as_dict(
        normalized.get("master_judgement")
        or _as_dict(package_payload).get("master_judgement")
        or context_package.get("master_judgement")
        or planner_runtime.get("master_judgement")
    )
    visible_doc_state_stale = bool(
        visible_document_refs
        and str(existing_master.get("document_state") or "").strip() in {"missing", "not_available"}
        and str(existing_master.get("master_state") or "").strip() != "no_match"
    )
    path_truth = _path_truth_contract_for_master(package_payload, context_package)
    path_state_stale = bool(
        path_truth
        and bool(path_truth.get("ready"))
        and str(existing_master.get("master_state") or "").strip() != "no_match"
        and str(existing_master.get("path_state") or "").strip()
        not in {"route_truth_ready", "completed", "complete", str(path_truth.get("state") or "").strip()}
    )
    if (
        _master_judgement_matches_ledger(existing_master, mission_evidence_ledger)
        and not visible_doc_state_stale
        and not path_state_stale
    ):
        master_judgement = existing_master
    elif mission_evidence_ledger:
        from answering import build_mcp_master_judgement

        master_judgement = build_mcp_master_judgement(
            query_text=_query_text_for_master(normalized, context_package),
            mission_evidence_ledger=mission_evidence_ledger,
            package_render_contract=_package_render_contract_for_master(package_payload, context_package),
            path_truth_contract=path_truth,
            document_refs=visible_document_refs,
        )
        master_judgement = dict(master_judgement)
        master_judgement["repaired_from_delivery_ledger"] = True
    else:
        master_judgement = existing_master
    if not master_judgement:
        return {}
    normalized["master_judgement"] = master_judgement
    planner_runtime["master_judgement"] = master_judgement
    normalized["planner_runtime"] = planner_runtime
    context_package["master_judgement"] = master_judgement
    normalized["context_package"] = context_package
    if isinstance(package_payload, dict):
        package_payload["master_judgement"] = master_judgement
    return master_judgement


def _mission_ledger_is_legacy_empty_route_snapshot(
    ledger: dict[str, Any],
    *,
    allow_actionable_placeholder: bool = False,
) -> bool:
    rows = [row for row in _as_list(_as_dict(ledger).get("rows")) if isinstance(row, dict)]
    if not rows:
        return False
    legacy_rows = 0
    allowed_states = {"stop", "missed"}
    if allow_actionable_placeholder:
        allowed_states.update({"needs_radius_widen", "wrong_region"})
    for row in rows:
        branch_judgement = _as_dict(row.get("branch_judgement"))
        counts = _as_dict(branch_judgement.get("evidence_counts"))
        visible_counts = 0
        for key in ("hot", "cold", "document_refs", "route_events", "visited_nodes"):
            try:
                visible_counts += int(counts.get(key) or 0)
            except (TypeError, ValueError):
                visible_counts += 0
        row_has_visible_evidence = bool(
            visible_counts
            or _as_list(row.get("hot_evidence"))
            or _as_list(row.get("cold_evidence"))
            or _as_list(row.get("document_refs"))
            or _as_list(row.get("route_events"))
        )
        if row_has_visible_evidence:
            return False
        state = str(branch_judgement.get("state") or row.get("coverage_state") or "").strip().lower()
        reason_text = " ".join(
            str(value or "")
            for value in (
                row.get("coverage_reason"),
                row.get("missing_reason"),
                " ".join(str(item or "") for item in _as_list(branch_judgement.get("reason_codes"))),
            )
        ).lower()
        mission_id = str(row.get("mission_id") or "").strip().lower()
        goal = str(row.get("goal") or "").strip().lower()
        if (
            state in allowed_states
            and (
                mission_id.startswith("branch_")
                or goal == "legacy_branch_goal"
                or "mission_not_executed_or_no_route_material" in reason_text
                or "path_mission_not_executed_by_runtime" in reason_text
                or "no_visible_branch_evidence" in reason_text
            )
        ):
            legacy_rows += 1
            continue
        return False
    return legacy_rows == len(rows)


def _mission_ledger_is_selected_payload_supersedable_route_only_placeholder(ledger: dict[str, Any]) -> bool:
    rows = [row for row in _as_list(_as_dict(ledger).get("rows")) if isinstance(row, dict)]
    if not rows:
        return False
    supersedable_rows = 0
    for row in rows:
        branch_judgement = _as_dict(row.get("branch_judgement"))
        counts = _as_dict(branch_judgement.get("evidence_counts"))

        def _count(key: str) -> int:
            try:
                return int(counts.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        hot_count = _count("hot") + len(_as_list(row.get("hot_evidence")))
        cold_count = _count("cold") + len(_as_list(row.get("cold_evidence")))
        document_count = _count("document_refs") + len(_as_list(row.get("document_refs")))
        if hot_count > 0 or cold_count > 0 or document_count > 0:
            return False
        route_count = _count("route_events") + len(_as_list(row.get("route_events")))
        visited_count = _count("visited_nodes") + len(_as_list(row.get("visited_node_ids")))
        if route_count <= 0 and visited_count <= 0:
            return False
        state = str(branch_judgement.get("state") or row.get("coverage_state") or "").strip().lower()
        reason_text = " ".join(
            str(value or "")
            for value in (
                row.get("coverage_reason"),
                row.get("missing_reason"),
                " ".join(str(item or "") for item in _as_list(branch_judgement.get("reason_codes"))),
            )
        ).lower()
        if state not in {"near_miss", "needs_radius_widen", "wrong_region"}:
            return False
        if not any(
            marker in reason_text
            for marker in (
                "route_executed_without_sufficient_mission_evidence",
                "route_executed_without_enough_evidence",
                "no_visible_branch_evidence",
            )
        ):
            return False
        supersedable_rows += 1
    return supersedable_rows == len(rows)


def _selected_context_package_soft_unresolved_sections_allowed(
    normalized: dict[str, Any],
    *,
    context_package: dict[str, Any],
) -> bool:
    contract = _as_dict(context_package.get("contract"))
    unresolved = [
        str(item or "").strip()
        for item in _as_list(contract.get("unresolved_sections"))
        if str(item or "").strip()
    ]
    if not unresolved:
        return True
    if any(item != "explicit_query_entity_coverage" for item in unresolved):
        return False
    if _as_list(contract.get("semantic_missing_slot_keys") or contract.get("missing_semantic_slots")):
        return False
    answerability_ledger = _as_dict(
        contract.get("answerability_slot_ledger") or context_package.get("answerability_ledger")
    )
    if answerability_ledger and not bool(answerability_ledger.get("passed", True)):
        return False
    link_aware_context = _as_dict(contract.get("link_aware_context") or context_package.get("link_aware_context"))
    if link_aware_context and not bool(link_aware_context.get("passed", True)):
        return False
    ref_contract = _as_dict(
        normalized.get("document_ref_contract")
        or contract.get("document_ref_contract")
        or context_package.get("document_ref_contract")
    )
    delivery_contract = _as_dict(
        normalized.get("document_delivery_contract")
        or contract.get("document_delivery_contract")
        or context_package.get("document_delivery_contract")
    )
    actionable_refs = int(ref_contract.get("actionable_document_ref_count") or 0)
    raw_refs = int(ref_contract.get("raw_available_document_ref_count") or 0)
    delivery_state = str(delivery_contract.get("state") or "").strip()
    return bool(
        actionable_refs > 0
        and raw_refs > 0
        and delivery_state in {"refs_ready", "refs_actionable", "raw_included"}
        and bool(ref_contract.get("all_refs_actionable", True))
    )


def _selected_context_package_can_reconcile_no_match_snapshot(
    normalized: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    allow_actionable_placeholder: bool = False,
) -> bool:
    if tool_name not in {"retrieve_context", "inspect_context_package"}:
        return False
    if status in {"no_match", "blocked", "failed"}:
        return False
    context_package = _as_dict(normalized.get("context_package"))
    if not context_package or _context_package_exact_no_match(context_package):
        return False
    contract = _as_dict(context_package.get("contract"))
    metrics = _as_dict(context_package.get("metrics"))
    materialization = _as_dict(normalized.get("context_package_materialization"))
    package_render_contract = _as_dict(
        metrics.get("package_render_contract") or context_package.get("package_render_contract")
    )
    answerability_ledger = _as_dict(
        contract.get("answerability_slot_ledger") or context_package.get("answerability_ledger")
    )
    link_aware_context = _as_dict(contract.get("link_aware_context") or context_package.get("link_aware_context"))
    selected_contract_passed = bool(
        contract.get("passed")
        or materialization.get("contract_passed")
        or package_render_contract.get("final_contract_passed")
        or metrics.get("contract_passed")
        or str(context_package.get("status") or "").strip() == "contract_satisfied"
        or str(materialization.get("status") or "").strip() == "contract_satisfied"
    )
    unresolved_sections = [
        str(item or "").strip()
        for item in _as_list(contract.get("unresolved_sections"))
        if str(item or "").strip()
    ]
    unresolved_sections_allowed = _selected_context_package_soft_unresolved_sections_allowed(
        normalized,
        context_package=context_package,
    )
    soft_unresolved_sections_certify = bool(unresolved_sections and unresolved_sections_allowed)
    if not selected_contract_passed and not soft_unresolved_sections_certify:
        return False
    if not unresolved_sections_allowed:
        return False
    if _as_list(contract.get("semantic_missing_slot_keys") or contract.get("missing_semantic_slots")):
        return False
    if answerability_ledger and not bool(answerability_ledger.get("passed", True)):
        return False
    if link_aware_context and not bool(link_aware_context.get("passed", True)):
        return False
    safety_unresolved = _as_list(package_render_contract.get("safety_unresolved_sections"))
    if safety_unresolved:
        return False
    unsafe_blocker_text = " ".join(
        str(item or "")
        for item in _as_list(package_render_contract.get("blocked_reasons"))
    ).lower()
    unsafe_markers = (
        "privacy",
        "private",
        "off_contract",
        "provenance",
        "source",
        "document_ref",
        "path_truth",
        "answer_alignment",
        "ai_material",
    )
    if any(marker in unsafe_blocker_text for marker in unsafe_markers):
        return False
    if not str(context_package.get("agent_markdown") or "").strip():
        return False
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    mission_ledger = _as_dict(normalized.get("mission_evidence_ledger") or planner_runtime.get("mission_evidence_ledger"))
    legacy_or_supersedable = _mission_ledger_is_legacy_empty_route_snapshot(
        mission_ledger,
        allow_actionable_placeholder=allow_actionable_placeholder,
    ) or (
        allow_actionable_placeholder
        and _mission_ledger_is_selected_payload_supersedable_route_only_placeholder(mission_ledger)
    )
    if not legacy_or_supersedable:
        return False
    if allow_actionable_placeholder:
        if bool(package_render_contract.get("source_is_ledger_only")):
            return False
        actionable_contract_passed = bool(
            package_render_contract.get("final_contract_passed")
            or metrics.get("contract_passed")
            or contract.get("passed")
            or materialization.get("contract_passed")
            or str(context_package.get("status") or "").strip() == "contract_satisfied"
            or str(materialization.get("status") or "").strip() == "contract_satisfied"
            or soft_unresolved_sections_certify
        )
        if not actionable_contract_passed:
            return False
        path_truth = _as_dict(contract.get("path_truth") or context_package.get("path_truth_contract"))
        path_truth_required = bool(path_truth.get("required") or metrics.get("path_truth_required"))
        path_truth_ready = bool(path_truth.get("ready") or metrics.get("path_truth_ready"))
        if path_truth_required and not path_truth_ready:
            return False
    semantic_runtime = _as_dict(normalized.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    ai_resilience = _as_dict(normalized.get("ai_materialization_resilience_contract"))
    if allow_actionable_placeholder:
        ai_spatial = _as_dict(
            normalized.get("ai_spatial_landing_contract")
            or normalized.get("ai_spatial_landing_contract_runtime")
            or planner_runtime.get("ai_spatial_landing_contract")
            or planner_runtime.get("ai_spatial_landing_contract_runtime")
        )
        ai_spatial_status = str(ai_spatial.get("status") or "").strip()
        ai_spatial_pending = bool(
            ai_spatial
            and not bool(ai_spatial.get("materialized") or ai_spatial.get("certifiable"))
            and (
                ai_spatial_status in {"blocked", "pending", "deferred", "provider_degraded", "timeout"}
                or _as_list(ai_spatial.get("missing_reasons"))
            )
        )
        if ai_spatial_pending:
            return False
    return bool(
        metrics.get("ai_slot_fast_package")
        or materialization.get("source") == "ai_slot_fast_package"
        or bool(materialization.get("contract_passed"))
        or soft_unresolved_sections_certify
        or bool(ai_resilience.get("readiness_certifiable"))
        or bool(semantic_runtime.get("material"))
        or str(semantic_runtime.get("status") or "").strip() in {"completed", "materialized"}
    )


def _selected_context_package_can_reconcile_sufficient_partial_master(
    normalized: dict[str, Any],
    *,
    tool_name: str,
    status: str,
) -> bool:
    if tool_name not in {"retrieve_context", "inspect_context_package"}:
        return False
    if status in {"no_match", "blocked", "failed"}:
        return False
    context_package = _as_dict(normalized.get("context_package"))
    if not context_package or _context_package_exact_no_match(context_package):
        return False
    if not str(context_package.get("agent_markdown") or "").strip():
        return False

    contract = _as_dict(context_package.get("contract"))
    metrics = _as_dict(context_package.get("metrics"))
    materialization = _as_dict(normalized.get("context_package_materialization"))
    package_render_contract = _as_dict(
        metrics.get("package_render_contract") or context_package.get("package_render_contract")
    )
    selected_contract_passed = bool(
        contract.get("passed")
        or materialization.get("contract_passed")
        or package_render_contract.get("final_contract_passed")
        or metrics.get("contract_passed")
        or str(context_package.get("status") or "").strip() == "contract_satisfied"
        or str(materialization.get("status") or "").strip() == "contract_satisfied"
    )
    if not selected_contract_passed:
        return False

    unresolved_sections_allowed = _selected_context_package_soft_unresolved_sections_allowed(
        normalized,
        context_package=context_package,
    )
    unresolved_sections = [
        str(item or "").strip()
        for item in _as_list(contract.get("unresolved_sections"))
        if str(item or "").strip()
    ]
    if unresolved_sections and not unresolved_sections_allowed:
        return False
    if _as_list(contract.get("semantic_missing_slot_keys") or contract.get("missing_semantic_slots")):
        return False

    answerability_ledger = _as_dict(
        contract.get("answerability_slot_ledger") or context_package.get("answerability_ledger")
    )
    if answerability_ledger and not bool(answerability_ledger.get("passed", True)):
        return False
    link_aware_context = _as_dict(contract.get("link_aware_context") or context_package.get("link_aware_context"))
    if link_aware_context and not bool(link_aware_context.get("passed", True)):
        return False
    if _as_list(package_render_contract.get("safety_unresolved_sections")):
        return False

    unsafe_blocker_text = " ".join(
        str(item or "")
        for item in _as_list(package_render_contract.get("blocked_reasons"))
    ).lower()
    unsafe_markers = (
        "privacy",
        "private",
        "off_contract",
        "provenance",
        "document_ref",
        "path_truth",
        "answer_alignment",
        "ai_material",
    )
    if any(marker in unsafe_blocker_text for marker in unsafe_markers):
        return False

    path_truth = _as_dict(contract.get("path_truth") or context_package.get("path_truth_contract"))
    path_truth_required = bool(path_truth.get("required") or metrics.get("path_truth_required"))
    path_truth_ready = bool(path_truth.get("ready") or metrics.get("path_truth_ready"))
    if path_truth_required and not path_truth_ready:
        return False

    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    semantic_runtime = _as_dict(
        normalized.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime")
    )
    ai_resilience = _as_dict(normalized.get("ai_materialization_resilience_contract"))
    semantic_ai_material = bool(
        metrics.get("ai_slot_fast_package")
        or materialization.get("source") == "ai_slot_fast_package"
        or materialization.get("contract_passed")
        or ai_resilience.get("readiness_certifiable")
        or semantic_runtime.get("material")
        or str(semantic_runtime.get("status") or "").strip() in {"completed", "materialized"}
    )
    if not semantic_ai_material:
        return False

    ai_spatial = _as_dict(
        normalized.get("ai_spatial_landing_contract")
        or normalized.get("ai_spatial_landing_contract_runtime")
        or planner_runtime.get("ai_spatial_landing_contract")
        or planner_runtime.get("ai_spatial_landing_contract_runtime")
    )
    if ai_spatial and not bool(ai_spatial.get("materialized") or ai_spatial.get("certifiable")):
        return False

    return True


def _selected_context_package_master_judgement(
    normalized: dict[str, Any],
    *,
    previous_master: dict[str, Any],
    reconciliation_reason: str = "legacy_empty_route_snapshot_with_ai_context_contract_passed",
) -> dict[str, Any]:
    context_package = _as_dict(normalized.get("context_package"))
    contract = _as_dict(context_package.get("contract"))
    metrics = _as_dict(context_package.get("metrics"))
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    mission_ledger = _as_dict(normalized.get("mission_evidence_ledger") or planner_runtime.get("mission_evidence_ledger"))
    ledger_branch_counts = _mission_ledger_effective_branch_counts(mission_ledger)
    goals = [
        str(item or "").strip()
        for item in _as_list(contract.get("semantic_satisfied_slot_keys"))
        or _as_list(contract.get("contract_core_sections"))
        or _as_list(contract.get("required_sections"))
        if str(item or "").strip()
    ]
    if not goals:
        goals = ["selected_context_package"]
    partial_master_reconciliation = "partial_master" in reconciliation_reason
    cache_seed = "|".join(
        [
            str(normalized.get("search_id") or ""),
            _query_text_for_master(normalized, context_package),
            str(_sha256_text(str(context_package.get("agent_markdown") or "")) or ""),
        ]
    )
    cache_key = _sha256_text(cache_seed) or "selected_context_package"
    goal_coverage = []
    for index, goal in enumerate(goals, start=1):
        goal_coverage.append(
            {
                "mission_id": f"selected_payload_goal_{index}",
                "path_id": None,
                "goal": goal,
                "coverage_state": "resolved_by_selected_payload_contract",
                "normalized_goal_state": "covered",
                "coverage_reason": reconciliation_reason,
                "branch_judgement_state": "superseded_nonexecuted_snapshot",
                "branch_judgement_reason_codes": [
                    "selected_payload_contract_passed",
                    *(
                        ["partial_master_satisfied_by_selected_payload_contract"]
                        if partial_master_reconciliation
                        else []
                    ),
                ],
                "branch_next_recommended_action": None,
                "effective_branch_state": "selected_payload_contract",
                "master_goal_state": "covered",
                "hot_evidence_count": int(metrics.get("hot_item_count") or len(_as_list(context_package.get("hot_sections"))) or 0),
                "cold_evidence_count": int(metrics.get("cold_item_count") or len(_as_list(context_package.get("cold_sections"))) or 0),
                "document_ref_count": int(metrics.get("document_ref_count") or len(_as_list(context_package.get("document_refs"))) or 0),
                "route_event_count": 0,
                "correction_signal": {
                    "schema_version": "agvm.mission_correction_signal.v1",
                    "state": (
                        "selected_payload_satisfied_partial_master_contract"
                        if partial_master_reconciliation
                        else "superseded_nonexecuted_route_snapshot"
                    ),
                    "learning_use": (
                        "feed_master_partiality_review_without_overriding_selected_payload"
                        if partial_master_reconciliation
                        else "feed_latency_and_route_execution_review_without_overriding_selected_payload"
                    ),
                    "needs_review": bool(not partial_master_reconciliation),
                },
            }
        )
    return {
        "schema_version": "agvm.master_judgement.v1",
        "master_judgement_id": f"master_selected_payload_terminal_{len(goals)}_{cache_key[:8]}",
        "master_state": "terminal",
        "goal_coverage": goal_coverage,
        "covered_goals": goals,
        "partial_goals": [],
        "missing_goals": [],
        "unresolved_goals": [],
        "no_match_claim": False,
        "provider_state": "available",
        "context_state": "complete",
        "document_state": "raw_refs_ready" if _as_list(context_package.get("document_refs")) else "not_requested",
        "path_state": "not_required",
        "answer_voice": str(previous_master.get("answer_voice") or "first_person"),
        "agent_payload_state": "usable_context",
        "final_seal_allowed": True,
        "terminal_for_client": True,
        "continuation_recommendation": {
            "state": "none",
            "tool_action": None,
            "reason": "selected_mcp_payload_contract_passed",
        },
        "next_recommended_call": None,
        "expected_evidence_policy": {
            "schema_version": "agvm.master_expected_evidence_policy.v1",
            "query_scope": "selected_payload_contract",
            "expected_branch_count": int(mission_ledger.get("row_count") or len(_as_list(mission_ledger.get("rows"))) or 0),
            "minimum_resolved_branch_count": 0,
            "branch_completion_policy": (
                "selected_ai_payload_contract_can_terminalize_sufficient_partial_master"
                if partial_master_reconciliation
                else "nonexecuted_route_snapshot_cannot_override_selected_ai_payload"
            ),
            "document_hydration_required": False,
            "path_truth_required": False,
            "evidence_budget_policy": "selected_mcp_payload_contract_is_primary",
            "static_required_sections_are_not_terminality_source": True,
            "ai_sufficiency_required": False,
            "safety_invariants": [
                "ai_participation",
                "visible_provenance",
                "privacy_and_off_contract_boundary",
                "document_hydration_contract",
                "path_truth_for_path_tools",
                "budget",
            ],
        },
        "sufficiency_judge": {
            "schema_version": "agvm.master_sufficiency_judge.v1",
            "tier": "selected_payload_contract_reconciliation",
            "deterministic_precheck_state": "selected_payload_terminal",
            "ai_sufficiency_required": False,
            "ai_sufficiency_state": "covered_by_selected_ai_materialized_mcp_payload",
            "ledger_hash": cache_key[:16],
            "does_not_scan_raw_documents": True,
            "does_not_use_hidden_package_rescue": True,
            "uses_selected_mcp_payload_contract": True,
        },
        "branch_state_counts": ledger_branch_counts,
        "selected_payload_state_counts": {"covered_by_context_contract": len(goals)},
        "reason_codes": [
            "selected_context_package_contract_passed",
            (
                "partial_master_satisfied_by_selected_payload_contract"
                if partial_master_reconciliation
                else "nonexecuted_route_snapshot_demoted"
            ),
        ],
        "ledger_row_count": int(mission_ledger.get("row_count") or len(_as_list(mission_ledger.get("rows"))) or 0),
        "cache_key": cache_key[:16],
        "cache_hit": False,
        "master_judge_timing_ms": 0.0,
        "source": "selected_context_package_contract",
        "selected_payload_reconciliation": True,
        "supersedes_master_judgement_id": previous_master.get("master_judgement_id"),
        "superseded_master_state": previous_master.get("master_state"),
        "superseded_ledger_reason": reconciliation_reason,
    }


def _reconcile_master_judgement_with_selected_context_package(
    normalized: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    package_payload: Any,
) -> dict[str, Any]:
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    context_package = _as_dict(normalized.get("context_package"))
    existing_master = _as_dict(
        normalized.get("master_judgement")
        or _as_dict(package_payload).get("master_judgement")
        or context_package.get("master_judgement")
        or planner_runtime.get("master_judgement")
    )
    existing_master_state = str(existing_master.get("master_state") or "").strip()
    if existing_master_state not in {"no_match", "needs_more_search", "needs_hydration", "usable_partial"}:
        return existing_master
    allow_actionable_placeholder = existing_master_state in {"needs_more_search", "needs_hydration"}
    if _selected_context_package_can_reconcile_no_match_snapshot(
        normalized,
        tool_name=tool_name,
        status=status,
        allow_actionable_placeholder=allow_actionable_placeholder,
    ):
        reconciliation_reason = (
            "nonexecuted_route_snapshot_with_selected_ai_payload_contract_passed"
            if allow_actionable_placeholder
            else "legacy_empty_route_snapshot_with_ai_context_contract_passed"
        )
        if existing_master_state == "needs_hydration":
            reconciliation_reason = "nonexecuted_hydration_snapshot_with_selected_ai_payload_contract_passed"
    elif (
        existing_master_state == "usable_partial"
        and _selected_context_package_can_reconcile_sufficient_partial_master(
            normalized,
            tool_name=tool_name,
            status=status,
        )
    ):
        reconciliation_reason = "partial_master_satisfied_by_selected_ai_payload_contract"
    else:
        return existing_master
    reconciled = _selected_context_package_master_judgement(
        normalized,
        previous_master=existing_master,
        reconciliation_reason=reconciliation_reason,
    )
    normalized["master_judgement"] = reconciled
    planner_runtime["master_judgement"] = reconciled
    normalized["planner_runtime"] = planner_runtime
    context_package["master_judgement"] = reconciled
    context_package["selected_payload_master_reconciliation"] = {
        "schema_version": "agvm.selected_payload_master_reconciliation.v1",
        "applied": True,
        "reason": reconciliation_reason,
        "previous_master_state": existing_master.get("master_state"),
        "selected_payload_contract_passed": bool(_as_dict(context_package.get("contract")).get("passed")),
    }
    normalized["context_package"] = context_package
    if isinstance(package_payload, dict):
        package_payload["master_judgement"] = reconciled
        package_payload["selected_payload_master_reconciliation"] = context_package["selected_payload_master_reconciliation"]
    return reconciled


def _refresh_mission_learning_rollup_for_output(
    normalized: dict[str, Any],
    *,
    package_payload: Any,
) -> dict[str, Any]:
    context_package = _as_dict(normalized.get("context_package"))
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    package_payload_dict = _as_dict(package_payload)
    master_judgement = _as_dict(
        normalized.get("master_judgement")
        or package_payload_dict.get("master_judgement")
        or context_package.get("master_judgement")
        or planner_runtime.get("master_judgement")
    )
    existing = _as_dict(
        normalized.get("mission_learning_rollup")
        or package_payload_dict.get("mission_learning_rollup")
        or context_package.get("mission_learning_rollup")
        or planner_runtime.get("mission_learning_rollup")
    )
    selected_reconciliation = _as_dict(
        context_package.get("selected_payload_master_reconciliation")
        or package_payload_dict.get("selected_payload_master_reconciliation")
    )
    should_refresh = bool(
        not existing
        or selected_reconciliation.get("applied")
        or master_judgement.get("selected_payload_reconciliation")
    )
    if not should_refresh:
        return existing

    mission_ledger = _as_dict(
        normalized.get("mission_evidence_ledger")
        or planner_runtime.get("mission_evidence_ledger")
        or context_package.get("mission_evidence_ledger")
    )
    package_render_contract = _as_dict(
        context_package.get("package_render_contract")
        or package_payload_dict.get("package_render_contract")
        or _as_dict(context_package.get("metrics")).get("package_render_contract")
        or _as_dict(package_payload_dict.get("metrics")).get("package_render_contract")
    )
    context_contract = _as_dict(context_package.get("contract") or package_payload_dict.get("contract"))
    path_truth_contract = _as_dict(
        context_contract.get("path_truth")
        or context_package.get("path_truth_contract")
        or package_payload_dict.get("path_truth_contract")
    )
    rollup = build_mission_learning_rollup(
        query_text=_query_text_for_master(normalized, context_package or package_payload_dict),
        mission_evidence_ledger=mission_ledger,
        master_judgement=master_judgement,
        package_render_contract=package_render_contract,
        path_truth_contract=path_truth_contract,
    )
    normalized["mission_learning_rollup"] = rollup
    planner_runtime["mission_learning_rollup"] = rollup
    normalized["planner_runtime"] = planner_runtime
    if context_package:
        context_package["mission_learning_rollup"] = rollup
        normalized["context_package"] = context_package
    if isinstance(package_payload, dict):
        package_payload["mission_learning_rollup"] = rollup
    return rollup


def _sha256_text(value: str) -> str | None:
    text = str(value or "")
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _primary_payload_pointer_and_text(tool_name: str, package_field: str, package_payload: Any) -> tuple[str, str, bool]:
    if isinstance(package_payload, dict):
        if tool_name in {"retrieve_context", "inspect_context_package"}:
            agent_markdown = str(package_payload.get("agent_markdown") or "").strip()
            if agent_markdown:
                return f"{package_field}.agent_markdown", agent_markdown, True
        if tool_name in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES:
            documents = _as_list(package_payload.get("documents"))
            document_texts: list[str] = []
            for document in documents:
                if not isinstance(document, dict):
                    continue
                text = str(
                    document.get("raw_text")
                    or document.get("full_text")
                    or document.get("text")
                    or document.get("summary")
                    or ""
                ).strip()
                if text:
                    document_texts.append(text)
            if document_texts:
                return f"{package_field}.documents[*].raw_text", "\n\n".join(document_texts), True
            agent_markdown = str(package_payload.get("agent_markdown") or "").strip()
            if agent_markdown:
                return f"{package_field}.agent_markdown", agent_markdown, True
        return package_field, _stable_json(package_payload), False
    if isinstance(package_payload, list):
        return package_field, _stable_json(package_payload), False
    return package_field, str(package_payload or ""), False


def _payload_truth_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    package_field: str,
    package_payload: Any,
    status: str,
    completion_contract: dict[str, Any],
    payload_integrity: dict[str, Any],
    document_ref_contract: dict[str, Any],
    document_delivery_contract: dict[str, Any],
    document_bundle_payload: dict[str, Any],
) -> dict[str, Any]:
    context_package = _as_dict(result.get("context_package"))
    context_metrics = _as_dict(context_package.get("metrics"))
    hot_working_memory = _as_dict(result.get("hot_working_memory"))
    hot_working_memory_contract = _as_dict(result.get("hot_working_memory_contract"))
    evidence_reservoir = _as_dict(result.get("evidence_reservoir"))
    reservoir_summary = _as_dict(evidence_reservoir.get("reservoir_summary") or result.get("evidence_reservoir_summary"))
    document_workspace = _result_document_workspace(result)
    document_workspace_metrics = _as_dict(document_workspace.get("metrics"))
    pointer, primary_text, exact_primary = _primary_payload_pointer_and_text(tool_name, package_field, package_payload)
    primary_chars = len(primary_text)
    hot_item_count = len(_as_list(hot_working_memory.get("items")))
    demoted_count = int(hot_working_memory_contract.get("demoted_item_count") or len(_as_list(hot_working_memory.get("demoted_items"))) or 0)
    cold_entry_count = int(
        reservoir_summary.get("entry_count")
        or reservoir_summary.get("reservoir_entry_count")
        or context_metrics.get("cold_reservoir_entry_count")
        or 0
    )
    return {
        "schema_version": MCP_PAYLOAD_TRUTH_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "status": status,
        "completion_state": completion_contract.get("state"),
        "completion_reason": completion_contract.get("visible_reason"),
        "primary_mcp_payload": {
            "field": pointer,
            "package_field": package_field,
            "exact_backend_field": exact_primary,
            "present": bool(primary_chars),
            "char_count": primary_chars,
            "sha256": _sha256_text(primary_text),
            "ui_surface_name": "MCP package sent to client",
        },
        "surface_separation": {
            "mcp_package_sent_to_client": package_field,
            "hot_working_memory": "persistent runtime state, only sent when promoted into the MCP package",
            "cold_reservoir": "diagnostic/non-promoted evidence",
            "document_workspace": "document refs/raw bodies for document tools or follow-up calls",
            "raw_document_bundle": "optional raw document material controlled by document_text_policy",
            "answer_demo": "secondary demo surface, not the MCP memory product",
            "route_truth": "diagnostic path/map truth",
        },
        "hot_working_memory": {
            "separate_from_current_package": bool(hot_working_memory_contract.get("separate_from_context_package", True)),
            "item_count": hot_item_count,
            "demoted_item_count": demoted_count,
            "reuse_state": hot_working_memory_contract.get("reuse_state") or hot_working_memory_contract.get("state"),
        },
        "cold_reservoir": {
            "entry_count": cold_entry_count,
            "not_promoted_by_default": True,
            "promotion_reason_required": True,
        },
        "documents": {
            "document_ref_count": int(document_ref_contract.get("document_ref_count") or context_metrics.get("document_ref_count") or 0),
            "actionable_document_ref_count": int(document_ref_contract.get("actionable_document_ref_count") or context_metrics.get("actionable_document_ref_count") or 0),
            "raw_available_document_ref_count": int(document_ref_contract.get("raw_available_document_ref_count") or context_metrics.get("raw_available_document_ref_count") or 0),
            "document_delivery_state": document_delivery_contract.get("state"),
            "mcp_client_receives_first": document_delivery_contract.get("mcp_client_receives_first"),
            "raw_text_already_in_primary_payload": bool(document_delivery_contract.get("raw_text_already_in_primary_payload")),
            "raw_text_follow_up_required": bool(document_delivery_contract.get("raw_text_follow_up_required")),
            "raw_included_document_count": int(document_delivery_contract.get("raw_included_document_count") or 0),
            "raw_available_not_included_count": int(document_delivery_contract.get("raw_available_not_included_count") or 0),
            "document_bundle_state": document_bundle_payload.get("state") or context_metrics.get("document_bundle_state"),
            "document_bundle_document_count": int(document_bundle_payload.get("document_count") or context_metrics.get("document_bundle_document_count") or 0),
            "document_workspace_document_count": int(document_workspace_metrics.get("document_count") or len(_as_list(document_workspace.get("documents"))) or 0),
            "raw_text_policy": result.get("document_text_policy") or context_package.get("document_text_policy") or document_bundle_payload.get("document_text_policy") or "refs_only",
            "raw_text_follow_up_tool": "retrieve_document",
            "raw_text_follow_up_required_when_refs_only": True,
        },
        "answer_demo": {
            "secondary": True,
            "primary_product": False,
            "support_aligned": bool(payload_integrity.get("passed", True)),
            "support_node_count": int(payload_integrity.get("answer_support_node_count") or 0),
        },
        "operator_questions": {
            "did_ai_participate": "semantic_contract_runtime.material or ai_landing_materialization.route_level_materialized",
            "fresh_or_cached_ai": "semantic_contract_runtime.provider_state and cache_tier",
            "what_did_mcp_receive": pointer,
            "is_final": "completion_contract.state",
            "which_documents": "document_ref_contract and document_workspace",
            "raw_text_already_present": "document_delivery_contract.raw_text_already_in_primary_payload",
            "how_to_open_raw_document": "document_delivery_contract.exact_follow_up_recipe",
            "hot_vs_package": "hot_working_memory.separate_from_current_package",
            "why_not_promoted": "cold_reservoir plus promotion_policy/context_package contract",
        },
    }


def _inspection_payload_integrity(*, tool_name: str, package_field: str, package: Any) -> dict[str, Any]:
    return {
        "schema_version": "agvm.mcp_payload_integrity.v1",
        "tool_name": tool_name,
        "package_field": package_field,
        "context_package_is_product": package_field == "context_package",
        "answer_demo_secondary": True,
        "answer_support_node_count": 0,
        "package_node_id_count": len(_collect_node_ids(package)),
        "answer_support_node_ids_missing_from_package": [],
        "contract_missing_evidence_node_ids": [],
        "answer_context_alignment_checked": False,
        "answer_context_alignment_passed": True,
        "passed": True,
    }


def _completeness(result: dict[str, Any], *, tool_name: str, package_field: str, package: Any) -> dict[str, Any]:
    context_package_materialization = _as_dict(result.get("context_package_materialization"))
    answer_demo_materialization = _as_dict(result.get("answer_demo_materialization"))
    ai_landing_materialization = _as_dict(result.get("ai_landing_materialization"))
    ai_materialization_hard_gate = _as_dict(result.get("ai_materialization_hard_gate") or _as_dict(result.get("planner_runtime")).get("ai_materialization_hard_gate"))
    path_corridors = _as_dict(result.get("path_corridors"))
    path_metrics = _as_dict(path_corridors.get("metrics"))
    path_lifecycle = _as_dict(path_corridors.get("lifecycle"))
    context_package = _as_dict(result.get("context_package"))
    context_contract = _as_dict(context_package.get("contract"))
    context_metrics = _as_dict(context_package.get("metrics"))
    document_workspace = _result_document_workspace(result)
    document_ref_contract = _as_dict(result.get("document_ref_contract") or context_package.get("document_ref_contract") or document_workspace.get("document_ref_contract"))
    document_delivery_contract = _as_dict(result.get("document_delivery_contract") or context_package.get("document_delivery_contract") or document_workspace.get("document_delivery_contract"))
    document_bundle = _as_dict(result.get("document_bundle") or context_package.get("document_bundle"))
    search_map_2d_truth = _as_dict(result.get("search_map_2d_truth") or _as_dict(result.get("planner_runtime")).get("search_map_2d_truth"))
    run_projection_truth = _as_dict(result.get("run_projection_truth"))
    hot_working_memory = _as_dict(result.get("hot_working_memory"))
    hot_working_memory_contract = _as_dict(result.get("hot_working_memory_contract"))
    document_readiness = _document_tool_readiness(result)
    return {
        "query_text": str(result.get("query_text") or ""),
        "search_id": result.get("search_id"),
        "tool_name": tool_name,
        "package_field": package_field,
        "package_present": bool(package),
        "answerability_state": result.get("answerability_state"),
        "stop_reason": result.get("stop_reason"),
        "closure_state": result.get("closure_state"),
        "final_closure_ready": bool(result.get("final_closure_ready")),
        "unresolved_destination_count": int(result.get("unresolved_destination_count") or 0),
        "document_lookup_kind": result.get("document_lookup_kind"),
        "document_tool_readiness": document_readiness,
        "document_tool_ready": bool(document_readiness.get("document_tool_ready")),
        "exact_document_ready": bool(document_readiness.get("exact_document_ready")),
        "document_workspace_status": document_readiness.get("workspace_status"),
        "document_workspace_kind": document_readiness.get("workspace_kind"),
        "document_workspace_document_count": int(document_readiness.get("document_count") or 0),
        "document_workspace_full_text_document_count": int(document_readiness.get("full_text_document_count") or 0),
        "document_workspace_primary_document_count": int(document_readiness.get("primary_document_count") or 0),
        "document_workspace_primary_full_text_document_count": int(document_readiness.get("primary_full_text_document_count") or 0),
        "document_workspace_raw_text_char_count": int(document_readiness.get("raw_text_char_count") or 0),
        "document_workspace_primary_raw_text_char_count": int(document_readiness.get("primary_raw_text_char_count") or 0),
        "document_text_policy": result.get("document_text_policy") or context_package.get("document_text_policy") or "refs_only",
        "document_ref_count": int(document_ref_contract.get("document_ref_count") or context_metrics.get("document_ref_count") or 0),
        "actionable_document_ref_count": int(document_ref_contract.get("actionable_document_ref_count") or context_metrics.get("actionable_document_ref_count") or 0),
        "raw_available_document_ref_count": int(document_ref_contract.get("raw_available_document_ref_count") or context_metrics.get("raw_available_document_ref_count") or 0),
        "document_delivery_state": document_delivery_contract.get("state"),
        "mcp_client_receives_first": document_delivery_contract.get("mcp_client_receives_first"),
        "raw_text_already_in_primary_payload": bool(document_delivery_contract.get("raw_text_already_in_primary_payload")),
        "raw_text_follow_up_required": bool(document_delivery_contract.get("raw_text_follow_up_required")),
        "raw_included_document_count": int(document_delivery_contract.get("raw_included_document_count") or 0),
        "raw_available_not_included_count": int(document_delivery_contract.get("raw_available_not_included_count") or 0),
        "document_bundle_state": document_bundle.get("state") or context_metrics.get("document_bundle_state"),
        "document_bundle_document_count": int(document_bundle.get("document_count") or context_metrics.get("document_bundle_document_count") or 0),
        "search_map_2d_truth_present": bool(search_map_2d_truth),
        "run_projection_truth_present": bool(run_projection_truth),
        "run_projection_schema": run_projection_truth.get("schema_version"),
        "source_trace_count": len(_as_list(result.get("source_trace"))),
        "context_wave_count": len(_as_list(result.get("context_waves"))),
        "supporting_document_count": len(_as_list(result.get("supporting_documents"))),
        "context_package_materialization_state": context_package_materialization.get("state"),
        "context_package_contract_passed": bool(context_contract.get("passed")),
        "context_package_unresolved_sections": _as_list(context_contract.get("unresolved_sections")),
        "hot_item_count": int(context_metrics.get("hot_item_count") or len(_as_list(context_package.get("hot_sections"))) or 0),
        "cold_item_count": int(context_metrics.get("cold_item_count") or len(_as_list(context_package.get("cold_sections"))) or 0),
        "hot_working_memory_available": bool(hot_working_memory_contract.get("available") or hot_working_memory.get("items")),
        "hot_working_memory_reused_for_query": bool(hot_working_memory_contract.get("reused_for_query")),
        "hot_working_memory_reuse_state": hot_working_memory_contract.get("reuse_state") or hot_working_memory_contract.get("state"),
        "hot_working_memory_item_count": len(_as_list(hot_working_memory.get("items"))),
        "hot_working_memory_demoted_item_count": int(hot_working_memory_contract.get("demoted_item_count") or len(_as_list(hot_working_memory.get("demoted_items")))),
        "hot_working_memory_separate_from_context_package": bool(hot_working_memory_contract.get("separate_from_context_package", True)),
        "semantic_missing_slot_keys": _as_list(context_contract.get("semantic_missing_slot_keys")),
        "semantic_missing_descriptions": _as_list(context_contract.get("semantic_missing_descriptions")),
        "exact_field_requirement_count": int(context_metrics.get("exact_field_requirement_count") or 0),
        "exact_field_missing_count": int(context_metrics.get("exact_field_missing_count") or 0),
        "answer_demo_materialization_state": answer_demo_materialization.get("state"),
        "answer_demo_requested": bool(answer_demo_materialization.get("requested")),
        "ai_landing_materialization_state": ai_landing_materialization.get("validation_state"),
        "ai_landing_materialized": bool(ai_landing_materialization.get("route_level_materialized")),
        "ai_landing_materialization_blockers": _as_list(ai_landing_materialization.get("blockers")),
        "ai_materialization_gate_state": ai_materialization_hard_gate.get("validation_state"),
        "ai_materialization_gate_blocked": bool(ai_materialization_hard_gate.get("blocked")),
        "ai_materialization_gate_blockers": _as_list(ai_materialization_hard_gate.get("blockers")),
        "path_lifecycle_state_counts": {
            "completed": int(path_metrics.get("completed_path_count") or 0),
            "stopped": int(path_metrics.get("stopped_path_count") or 0),
            "started": int(path_metrics.get("started_path_count") or 0),
            "pending": int(path_metrics.get("pending_path_count") or 0),
        },
        "path_all_planned_accounted_for": bool(path_lifecycle.get("all_planned_paths_accounted_for")) if path_lifecycle else True,
        "path_changed_context_package_count": int(path_metrics.get("changed_context_package_path_count") or 0),
        "no_match": _status_for_tool(tool_name, result, package) == "no_match",
    }


def _budget(result: dict[str, Any]) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    timing = _as_dict(result.get("timing"))
    hot_working_memory = _as_dict(result.get("hot_working_memory"))
    hot_budget = _as_dict(hot_working_memory.get("token_budget"))
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    semantic_cache = _as_dict(semantic_runtime.get("cache"))
    semantic_cache_tier = str(semantic_runtime.get("cache_tier") or semantic_cache.get("tier") or "").strip() or None
    ai_landing = _as_dict(result.get("ai_landing_materialization"))
    ai_hard_gate = _as_dict(result.get("ai_materialization_hard_gate") or planner_runtime.get("ai_materialization_hard_gate"))
    return {
        "retrieval_mode": str(result.get("retrieval_mode") or planner_runtime.get("retrieval_mode") or "balanced"),
        "visited_node_count": len(_as_list(result.get("visited_node_ids"))),
        "visited_bucket_count": len(_as_list(result.get("visited_bucket_keys"))),
        "match_count": len(_as_list(result.get("matches"))),
        "branch_count": len(_as_list(result.get("branches"))),
        "landing_count": len(_as_list(result.get("landing_metadata"))),
        "max_matches": planner_runtime.get("max_matches"),
        "probe_limit_reason": planner_runtime.get("probe_limit_reason"),
        "timing": timing,
        "result_surface_ready_ms": result.get("result_surface_ready_ms"),
        "final_materialization_completed_ms": result.get("final_materialization_completed_ms"),
        "nonblocking_first_package": bool(result.get("final_materialization_pending") or _as_dict(result.get("mcp_runtime_boundary") or planner_runtime.get("mcp_runtime_boundary")).get("nonblocking_first_package_returned")),
        "background_completion_inspectable": bool(_as_dict(result.get("mcp_runtime_boundary") or planner_runtime.get("mcp_runtime_boundary")).get("background_completion_inspectable")),
        "llm_allowed": bool(semantic_runtime.get("enabled") and semantic_runtime.get("ai_required")),
        "ai_required": bool(semantic_runtime.get("ai_required") or ai_landing.get("required") or ai_hard_gate.get("required")),
        "ai_material": bool(
            semantic_runtime.get("material")
            or ai_landing.get("materialized")
            or ai_landing.get("route_level_materialized")
            or _ai_hard_gate_satisfied(ai_hard_gate)
        ),
        "semantic_contract_status": semantic_runtime.get("status"),
        "semantic_contract_source": semantic_runtime.get("source"),
        "semantic_contract_material": bool(semantic_runtime.get("material")),
        "semantic_contract_provider_state": semantic_runtime.get("provider_state"),
        "semantic_contract_provider_degraded": bool(semantic_runtime.get("provider_degraded") or semantic_runtime.get("degraded")),
        "semantic_contract": {
            "status": semantic_runtime.get("status"),
            "source": semantic_runtime.get("source"),
            "material": bool(semantic_runtime.get("material")),
            "cache_status": semantic_runtime.get("cache_status"),
            "cache_hit": bool(semantic_runtime.get("cache_hit")),
            "cache_tier": semantic_cache_tier,
            "cache": semantic_cache,
            "provider_state": semantic_runtime.get("provider_state"),
            "provider_degraded": bool(semantic_runtime.get("provider_degraded") or semantic_runtime.get("degraded")),
            "degraded_reason": semantic_runtime.get("degraded_reason"),
            "provider_retry_policy": _as_dict(semantic_runtime.get("provider_retry_policy")),
            "brain_revision": semantic_runtime.get("brain_revision"),
            "cache_scope": semantic_runtime.get("cache_scope"),
        },
        "hot_working_memory_estimated_tokens": int(hot_budget.get("estimated_tokens") or 0),
        "hot_working_memory_max_hot_items": hot_budget.get("max_hot_items"),
    }


def _phase_ms(phase_timings: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in phase_timings:
            ms = _as_ms(phase_timings.get(key))
            if ms is not None:
                return ms
    return None


def _final_stage_timings_by_key(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in _as_list(result.get("final_materialization_stage_timings")):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        key = str(
            row.get("stage_key")
            or row.get("stage")
            or row.get("phase")
            or row.get("name")
            or row.get("label")
            or ""
        ).strip()
        if key:
            by_key[key] = row
    return by_key


def _final_stage_ms(final_stages: dict[str, dict[str, Any]], *keys: str) -> float | None:
    for key in keys:
        row = final_stages.get(key)
        if not row:
            continue
        ms = _first_ms(
            row.get("duration_ms"),
            row.get("ms"),
            row.get("elapsed_ms"),
            row.get("completed_ms"),
            row.get("completed_at_ms"),
        )
        if ms is not None:
            return ms
    return None


def _runtime_stage_timings(result: dict[str, Any], timing: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    timing = dict(timing or {})
    planner_runtime = _as_dict(result.get("planner_runtime"))
    phase_timings = _as_dict(timing.get("phase_timings") or planner_runtime.get("phase_timings"))
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime") or timing.get("semantic_contract"))
    semantic_cache = _as_dict(semantic_runtime.get("cache"))
    context_materialization = _as_dict(result.get("context_package_materialization"))
    document_lookup = _as_dict(result.get("document_lookup"))
    document_workspace = _result_document_workspace(result)
    hot_memory_contract = _as_dict(result.get("hot_working_memory_contract"))
    final_stages = _final_stage_timings_by_key(result)

    def stage(
        key: str,
        label: str,
        *,
        elapsed_ms: float | None = None,
        duration_ms: float | None = None,
        state: str | None = None,
        source: str | None = None,
        blocking_for_first_package: bool = True,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_elapsed_ms = _as_ms(elapsed_ms)
        resolved_duration_ms = _as_ms(duration_ms)
        resolved_state = str(state or "").strip() or (
            "timed" if resolved_elapsed_ms is not None or resolved_duration_ms is not None else "not_recorded"
        )
        return {
            "schema_version": MCP_STAGE_TIMING_SCHEMA_VERSION,
            "stage_key": key,
            "label": label,
            "state": resolved_state,
            "elapsed_ms": resolved_elapsed_ms,
            "duration_ms": resolved_duration_ms,
            "source": source or "runtime",
            "blocking_for_first_package": blocking_for_first_package,
            "details": details or {},
        }

    semantic_cache_tier = str(semantic_runtime.get("cache_tier") or semantic_cache.get("tier") or "").strip() or None
    semantic_cache_hit = bool(semantic_runtime.get("cache_hit"))
    return [
        stage(
            "semantic_contract",
            "Semantic contract / AI landing intent",
            duration_ms=_first_ms(semantic_runtime.get("compiler_ms"), _final_stage_ms(final_stages, "semantic_contract")),
            state=str(semantic_runtime.get("provider_state") or semantic_runtime.get("status") or "waiting"),
            source=str(semantic_runtime.get("source") or "unknown"),
            details={
                "ai_required": bool(semantic_runtime.get("ai_required", True)),
                "material": bool(semantic_runtime.get("material")),
                "cache_hit": semantic_cache_hit,
                "cache_status": semantic_runtime.get("cache_status"),
                "cache_tier": semantic_cache_tier,
                "provider_degraded": bool(semantic_runtime.get("provider_degraded") or semantic_runtime.get("degraded")),
            },
        ),
        stage(
            "landing_planning",
            "Landing planning",
            elapsed_ms=_first_ms(timing.get("first_landing_ms"), phase_timings.get("first_landing_ms"), planner_runtime.get("plan_ms")),
            duration_ms=_phase_ms(phase_timings, "landing_planning_ms", "plan_ms", "planner_ms"),
            state="planned" if _as_list(result.get("landing_metadata")) or _as_list(result.get("branches")) else "waiting",
            source="planner_runtime",
            details={
                "landing_count": len(_as_list(result.get("landing_metadata"))),
                "branch_count": len(_as_list(result.get("branches"))),
                "planner_mode": planner_runtime.get("planner_mode") or result.get("planner_mode"),
            },
        ),
        stage(
            "route_traversal",
            "Route traversal / path corridor",
            elapsed_ms=_phase_ms(phase_timings, "route_traversal_elapsed_ms", "travel_elapsed_ms", "route_truth_elapsed_ms"),
            duration_ms=_first_ms(
                _phase_ms(phase_timings, "route_traversal_ms", "travel_ms", "route_truth_ms"),
                _final_stage_ms(final_stages, "route_traversal", "route_truth", "travel"),
            ),
            state="traversed" if _as_list(result.get("steps")) else "planned" if _as_list(result.get("branches")) else "not_recorded",
            source="route_runtime",
            details={
                "step_count": len(_as_list(result.get("steps"))),
                "visited_node_count": len(_as_list(result.get("visited_node_ids"))),
            },
        ),
        stage(
            "document_lookup",
            "Document lookup / raw document contract",
            elapsed_ms=_phase_ms(phase_timings, "document_lookup_elapsed_ms"),
            duration_ms=_first_ms(
                _phase_ms(phase_timings, "document_lookup_ms", "document_workspace_ms"),
                _final_stage_ms(final_stages, "document_lookup", "document_workspace"),
            ),
            state=str(result.get("document_lookup_kind") or document_lookup.get("kind") or document_workspace.get("status") or "not_requested"),
            source="document_runtime",
            blocking_for_first_package=False,
            details={
                "workspace_status": document_workspace.get("status"),
                "document_count": int(_as_dict(document_workspace.get("metrics")).get("document_count") or len(_as_list(document_workspace.get("documents"))) or 0),
                "document_text_policy": result.get("document_text_policy") or _as_dict(result.get("context_package")).get("document_text_policy"),
            },
        ),
        stage(
            "package_build",
            "First MCP package build",
            elapsed_ms=_first_ms(timing.get("first_context_ms"), context_materialization.get("first_context_ms"), timing.get("result_surface_ready_ms"), result.get("result_surface_ready_ms")),
            duration_ms=_first_ms(_phase_ms(phase_timings, "context_package_build_ms", "package_build_ms"), _final_stage_ms(final_stages, "context_package_build", "package_build")),
            state=str(context_materialization.get("state") or _as_dict(result.get("context_package")).get("status") or "waiting"),
            source="context_package_materialization",
            details={
                "contract_passed": bool(context_materialization.get("contract_passed") or _as_dict(_as_dict(result.get("context_package")).get("contract")).get("passed")),
                "agent_markdown_chars": len(str(_as_dict(result.get("context_package")).get("agent_markdown") or "")),
            },
        ),
        stage(
            "hot_memory_update",
            "Hot working memory update",
            elapsed_ms=_phase_ms(phase_timings, "hot_working_memory_elapsed_ms"),
            duration_ms=_first_ms(_phase_ms(phase_timings, "hot_working_memory_ms", "hot_memory_update_ms"), _final_stage_ms(final_stages, "hot_working_memory", "hot_memory_update")),
            state=str(hot_memory_contract.get("reuse_state") or hot_memory_contract.get("state") or hot_memory_contract.get("store_status") or "not_recorded"),
            source="hot_working_memory",
            blocking_for_first_package=False,
            details={
                "separate_from_context_package": bool(hot_memory_contract.get("separate_from_context_package", True)),
                "demoted_item_count": int(hot_memory_contract.get("demoted_item_count") or 0),
            },
        ),
        stage(
            "final_seal",
            "Final materialization seal",
            elapsed_ms=_first_ms(timing.get("final_materialization_completed_ms"), result.get("final_materialization_completed_ms"), timing.get("total_ms")),
            duration_ms=_first_ms(_phase_ms(phase_timings, "final_materialization_ms", "final_seal_ms"), _final_stage_ms(final_stages, "final_materialization", "final_seal")),
            state=str(result.get("result_materialization_state") or ("pending" if result.get("final_materialization_pending") else "finalized" if result.get("result_ready_terminal") else "not_recorded")),
            source="final_materialization",
            blocking_for_first_package=False,
            details={
                "pending": bool(result.get("final_materialization_pending")),
                "terminal": bool(result.get("result_ready_terminal", False)),
                "stage_count": len(final_stages),
            },
        ),
    ]


def _stage_timing_summary(stage_timings: list[dict[str, Any]]) -> dict[str, Any]:
    timed = [
        stage
        for stage in stage_timings
        if _as_ms(stage.get("elapsed_ms")) is not None or _as_ms(stage.get("duration_ms")) is not None
    ]
    missing = [str(stage.get("stage_key")) for stage in stage_timings if stage not in timed]
    return {
        "schema_version": "agvm.pr12p14n.stage_timing_summary.v1",
        "stage_count": len(stage_timings),
        "timed_stage_count": len(timed),
        "missing_stage_keys": missing,
        "first_package_blocking_stage_keys": [
            str(stage.get("stage_key"))
            for stage in stage_timings
            if bool(stage.get("blocking_for_first_package"))
        ],
        "background_stage_keys": [
            str(stage.get("stage_key"))
            for stage in stage_timings
            if not bool(stage.get("blocking_for_first_package"))
        ],
    }


def _timing(result: dict[str, Any]) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    timing = _as_dict(result.get("timing"))
    phase_timings = _as_dict(timing.get("phase_timings") or planner_runtime.get("phase_timings"))
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    semantic_cache = _as_dict(semantic_runtime.get("cache"))
    semantic_cache_tier = str(semantic_runtime.get("cache_tier") or semantic_cache.get("tier") or "").strip() or None
    context_package_materialization = _as_dict(result.get("context_package_materialization"))
    payload = {
        "schema_version": "agvm.mcp_timing.v1",
        "total_ms": timing.get("total_ms"),
        "first_landing_ms": timing.get("first_landing_ms"),
        "first_context_ms": timing.get("first_context_ms") or context_package_materialization.get("first_context_ms"),
        "answer_first_ms": timing.get("answer_first_ms"),
        "answer_final_ms": timing.get("answer_final_ms"),
        "result_surface_ready_ms": result.get("result_surface_ready_ms") or timing.get("result_surface_ready_ms"),
        "final_materialization_started_ms": result.get("final_materialization_started_ms") or timing.get("final_materialization_started_ms"),
        "final_materialization_completed_ms": result.get("final_materialization_completed_ms") or timing.get("final_materialization_completed_ms"),
        "phase_timings": phase_timings,
        "semantic_contract": {
            "status": semantic_runtime.get("status"),
            "compiler_ms": semantic_runtime.get("compiler_ms"),
            "cache_status": semantic_runtime.get("cache_status"),
            "cache_hit": bool(semantic_runtime.get("cache_hit")),
            "cache_tier": semantic_cache_tier,
            "cache": semantic_cache,
            "cache_key_fingerprint": semantic_runtime.get("cache_key_fingerprint"),
            "brain_revision": semantic_runtime.get("brain_revision"),
            "cache_scope": semantic_runtime.get("cache_scope"),
            "source": semantic_runtime.get("source"),
            "material": bool(semantic_runtime.get("material")),
            "provider_state": semantic_runtime.get("provider_state"),
            "provider_degraded": bool(semantic_runtime.get("provider_degraded") or semantic_runtime.get("degraded")),
            "degraded_reason": semantic_runtime.get("degraded_reason"),
            "provider_retry_policy": _as_dict(semantic_runtime.get("provider_retry_policy")),
            "model_profile": _as_dict(semantic_runtime.get("model_profile")),
        },
    }
    stage_timings = _runtime_stage_timings(result, payload)
    payload["stage_timings"] = stage_timings
    payload["stage_timing_summary"] = _stage_timing_summary(stage_timings)
    return payload


def _latency_contract(result: dict[str, Any], *, tool_name: str, package_field: str, timing: dict[str, Any]) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    runtime_boundary = _as_dict(result.get("mcp_runtime_boundary") or planner_runtime.get("mcp_runtime_boundary"))
    semantic_contract = _as_dict(timing.get("semantic_contract"))
    stage_timings = _as_list(timing.get("stage_timings"))
    stage_timing_summary = _as_dict(timing.get("stage_timing_summary"))
    response_mode = str(result.get("response_mode") or planner_runtime.get("response_mode") or "context")
    first_context_ms = _first_ms(timing.get("first_context_ms"))
    result_surface_ready_ms = _first_ms(timing.get("result_surface_ready_ms"))
    full_completion_ms = _first_ms(
        timing.get("final_materialization_completed_ms"),
        result.get("final_materialization_completed_ms"),
        timing.get("total_ms"),
    )
    first_useful_package_ms = _first_ms(first_context_ms, result_surface_ready_ms, full_completion_ms)
    background_completion_ms = None
    if first_useful_package_ms is not None and full_completion_ms is not None:
        background_completion_ms = round(max(0.0, full_completion_ms - first_useful_package_ms), 2)
    answer_demo_requested = response_mode in {"answer", "both"}
    nonblocking_first_package = bool(
        runtime_boundary.get("nonblocking_first_package_returned")
        or result.get("final_materialization_pending")
        or str(result.get("result_materialization_state") or "") in {"first_package_ready_background_running", "snapshot_ready"}
    )
    return {
        "schema_version": "agvm.mcp_latency_contract.v1",
        "mcp_first": True,
        "tool_name": tool_name,
        "product_surface": package_field,
        "benchmark_latency_basis": "first_useful_package_ms",
        "first_useful_package_ms": first_useful_package_ms,
        "first_context_ms": first_context_ms,
        "first_ai_landing_ms": _first_ms(timing.get("first_landing_ms")),
        "result_surface_ready_ms": result_surface_ready_ms,
        "full_completion_ms": full_completion_ms,
        "background_completion_ms": background_completion_ms,
        "full_completion_is_secondary": not answer_demo_requested,
        "answer_demo_requested": answer_demo_requested,
        "http_response_policy": str(runtime_boundary.get("http_response_policy") or ("nonblocking_first_package_with_background_completion" if nonblocking_first_package else "sync_full_completion_with_reported_first_package")),
        "first_package_wait_seconds": runtime_boundary.get("first_package_wait_seconds"),
        "first_package_returned_before_full_completion": nonblocking_first_package,
        "background_completion_inspectable": bool(runtime_boundary.get("background_completion_inspectable") or nonblocking_first_package),
        "result_materialization_state": result.get("result_materialization_state"),
        "stream_contract": "context_wave_and_result_events_can_surface_first_package_before_full_completion",
        "semantic_contract_compiler_ms": _first_ms(semantic_contract.get("compiler_ms")),
        "semantic_contract_cache_hit": bool(semantic_contract.get("cache_hit")),
        "semantic_contract_cache_status": semantic_contract.get("cache_status"),
        "semantic_contract_cache_tier": semantic_contract.get("cache_tier"),
        "stage_timings": stage_timings,
        "stage_timing_summary": stage_timing_summary,
        "latency_slo": {
            "warm_or_focused_first_package_ms": 2000,
            "general_first_package_ms": 5000,
            "full_completion_reported_not_hidden": True,
        },
    }


def _completion_inspection(tool_name: str, search_id: Any) -> dict[str, Any]:
    normalized_tool = str(tool_name or "").strip()
    if normalized_tool in {"retrieve_path_corridor", "inspect_path_corridor"}:
        inspect_tool = "inspect_path_corridor"
        endpoint = "/mcp/inspect-path-corridor"
    elif normalized_tool in {"retrieve_source_trace", "inspect_route"}:
        inspect_tool = "inspect_route"
        endpoint = "/mcp/inspect-route"
    else:
        inspect_tool = "inspect_context_package"
        endpoint = "/mcp/inspect-context-package"
    return {
        "inspect_tool": inspect_tool,
        "inspect_endpoint": endpoint,
        "arguments": {
            "search_id": str(search_id or "<search_id_from_first_response>"),
            "include_raw_text": normalized_tool in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES,
            "include_answer_demo": False,
        },
        "available": bool(str(search_id or "").strip()),
    }


def _mcp_endpoint_for_tool(tool_name: str) -> str:
    normalized_tool = str(tool_name or "").strip()
    endpoint_by_tool = {
        "retrieve_context": "/mcp/retrieve-context",
        "retrieve_document": "/mcp/retrieve-document",
        "retrieve_document_workspace": "/mcp/retrieve-document-workspace",
        "retrieve_project_workspace": "/mcp/retrieve-project-workspace",
        "retrieve_path_corridor": "/mcp/retrieve-path-corridor",
        "retrieve_source_trace": "/mcp/retrieve-source-trace",
        "inspect_context_package": "/mcp/inspect-context-package",
        "inspect_path_corridor": "/mcp/inspect-path-corridor",
    }
    return endpoint_by_tool.get(normalized_tool, f"/mcp/{normalized_tool.replace('_', '-')}")


def _tool_boundary_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    package_field: str,
    package_payload: Any,
    include_raw_text: bool,
    completion_contract: dict[str, Any],
    runtime_state_contract: dict[str, Any],
    document_ref_contract: dict[str, Any],
    document_delivery_contract: dict[str, Any],
) -> dict[str, Any]:
    normalized_tool = str(tool_name or "").strip()
    search_id = str(result.get("search_id") or "").strip()
    document_id = str(result.get("document_id") or _as_dict(result.get("planner_runtime")).get("document_id") or "").strip()
    document_lookup = _as_dict(result.get("document_lookup"))
    document_workspace = _result_document_workspace(result)
    path_corridors = _as_dict(result.get("path_corridors"))
    path_metrics = _as_dict(path_corridors.get("metrics"))
    path_count = int(path_metrics.get("path_count") or path_metrics.get("planned_path_count") or len(_as_list(path_corridors.get("corridors"))) or 0)
    pointer, primary_text, exact_primary = _primary_payload_pointer_and_text(normalized_tool, package_field, package_payload)
    inspect_args = _as_dict(_as_dict(completion_contract.get("inspection")).get("arguments"))
    is_inspection_tool = normalized_tool.startswith("inspect_")
    is_exact_document_tool = normalized_tool == "retrieve_document"
    is_document_workspace_tool = normalized_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES
    is_document_tool = normalized_tool in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES
    is_path_tool = normalized_tool in {"retrieve_path_corridor", "inspect_path_corridor"}
    is_context_tool = normalized_tool in {"retrieve_context", "inspect_context_package"}
    if normalized_tool == "retrieve_context":
        tool_role = "primary_context_package"
        receives_first = "context_package.agent_markdown"
        first_payload_semantics = "agent-facing context package with document refs, hot/cold promotion state, route truth and optional bounded raw bundle by policy"
        default_follow_up = ["inspect_context_package", "inspect_path_corridor", "retrieve_document"]
    elif normalized_tool == "retrieve_document":
        tool_role = "raw_document_reader"
        receives_first = "document_workspace.documents[*].raw_text" if include_raw_text else "document_workspace with raw availability metadata"
        first_payload_semantics = "exact document payload; raw text is present only when include_raw_text or document_text_policy requests it"
        default_follow_up = ["inspect_context_package"]
    elif is_document_workspace_tool:
        tool_role = "document_workspace_discovery"
        receives_first = "document_workspace.document_refs with raw availability metadata"
        first_payload_semantics = "document evidence workspace discovery with ranked refs, reasons, expected contents and retrieve_document hydration recipes"
        default_follow_up = ["retrieve_document", "inspect_context_package"]
    elif normalized_tool == "retrieve_path_corridor":
        tool_role = "path_corridor_query_run"
        receives_first = "path_corridors"
        first_payload_semantics = "path-focused retrieval run with planned/traversed/promoted corridor material"
        default_follow_up = ["inspect_path_corridor", "inspect_context_package"]
    elif normalized_tool == "inspect_context_package":
        tool_role = "package_inspector"
        receives_first = "context_package"
        first_payload_semantics = "re-open an existing retrieval package by search_id without starting a new retrieval"
        default_follow_up = ["retrieve_document", "inspect_path_corridor"]
    elif normalized_tool == "inspect_path_corridor":
        tool_role = "path_corridor_inspector"
        receives_first = "path_corridors"
        first_payload_semantics = "re-open existing path/corridor material by search_id without starting a new retrieval"
        default_follow_up = ["inspect_context_package"]
    else:
        tool_role = "retrieval_inspector"
        receives_first = pointer
        first_payload_semantics = "structured MCP payload"
        default_follow_up = ["inspect_context_package"]

    exact_document_hit = bool(
        document_id
        or document_lookup.get("document_id")
        or document_lookup.get("state") == "direct_document_id_hit"
        or str(document_workspace.get("workspace_kind") or "") == "exact_document"
    )
    explicit_id_policy = {
        "requires_search_id": is_inspection_tool,
        "search_id_available": bool(search_id),
        "requires_document_id_for_exact_raw": is_exact_document_tool,
        "document_id_available": bool(document_id or document_lookup.get("document_id")),
        "requires_path_id_for_existing_corridor": normalized_tool == "inspect_path_corridor",
        "path_id_available": False,
        "supports_query_fallback": not is_inspection_tool,
        "fallback_query_mode": bool(
            not is_inspection_tool
            and (
                is_document_workspace_tool
                or (is_exact_document_tool and not exact_document_hit)
                or normalized_tool == "retrieve_path_corridor"
            )
        ),
        "fallback_reason": (
            "document_workspace_discovery_query"
            if is_document_workspace_tool
            else
            "document_id_missing_document_hint_or_query_used"
            if is_exact_document_tool and not exact_document_hit
            else "retrieve_path_corridor_starts_path_focused_query_run"
            if normalized_tool == "retrieve_path_corridor"
            else None
        ),
        "inspection_tools_do_not_start_new_runs": is_inspection_tool,
    }
    request_shape: dict[str, Any]
    if is_inspection_tool:
        request_shape = {
            "required": ["search_id"],
            "optional": ["brain_id", "include_debug", "include_raw_text", "include_answer_demo"],
            "example": {
                "search_id": search_id or "<search_id_from_retrieve_context>",
                "include_raw_text": bool(include_raw_text),
                "include_answer_demo": False,
            },
        }
    else:
        request_shape = {
            "required": ["query_text"],
            "optional": [
                "brain_id",
                "thread_id",
                "retrieval_mode",
                "context_package_mode",
                "document_text_policy",
                "max_matches",
                "include_raw_text",
                "include_answer_demo",
                "complete_paths",
                "document_id",
                "document_hint",
            ],
            "example": {
                "query_text": str(result.get("query_text") or "<memory request>"),
                "retrieval_mode": str(result.get("retrieval_mode") or _as_dict(result.get("planner_runtime")).get("retrieval_mode") or "balanced"),
                "document_id": document_id or None,
                "include_raw_text": bool(include_raw_text),
            },
        }

    return {
        "schema_version": MCP_TOOL_BOUNDARY_CONTRACT_SCHEMA_VERSION,
        "tool_name": normalized_tool,
        "tool_role": tool_role,
        "endpoint": _mcp_endpoint_for_tool(normalized_tool),
        "method": "POST",
        "status": status,
        "starts_new_retrieval_run": not is_inspection_tool,
        "inspects_existing_run": is_inspection_tool,
        "primary_response_field": package_field,
        "primary_payload_pointer": pointer,
        "primary_payload_exact_backend_field": exact_primary,
        "primary_payload_char_count": len(primary_text),
        "mcp_client_receives_first": receives_first,
        "first_payload_semantics": first_payload_semantics,
        "request_shape": request_shape,
        "explicit_id_policy": explicit_id_policy,
        "response_stability": {
            "json_first": True,
            "schema_stable": True,
            "answer_demo_default": False,
            "primary_field_always_present": True,
            "runtime_state_contract_required": True,
            "tool_boundary_contract_required": True,
        },
        "document_boundary": {
            "retrieve_context_returns_document_refs": is_context_tool,
            "retrieve_document_workspace_returns_document_refs": is_document_workspace_tool,
            "retrieve_document_returns_raw_text_when_requested": is_exact_document_tool,
            "raw_text_in_current_payload": bool(document_delivery_contract.get("raw_text_already_in_primary_payload") or include_raw_text),
            "raw_text_follow_up_required": bool(document_delivery_contract.get("raw_text_follow_up_required")),
            "document_ref_count": int(document_ref_contract.get("document_ref_count") or 0),
            "raw_available_document_ref_count": int(document_ref_contract.get("raw_available_document_ref_count") or 0),
            "follow_up_tool": "retrieve_document",
        },
        "path_boundary": {
            "path_tool_is_inspector": normalized_tool == "inspect_path_corridor",
            "retrieve_path_corridor_starts_query_if_no_search_id": normalized_tool == "retrieve_path_corridor",
            "retrieve_path_corridor_required_to_view_current_run_landings": False,
            "retrieve_path_corridor_is_new_path_focused_run": normalized_tool == "retrieve_path_corridor",
            "same_run_path_reader_tool": "inspect_path_corridor",
            "same_run_landings_reader": "stream_or_query_result",
            "path_count": path_count,
            "follow_up_tool": "inspect_path_corridor",
        },
        "inspection": {
            "search_id": search_id or None,
            "inspect_tool": _as_dict(completion_contract.get("inspection")).get("inspect_tool"),
            "inspect_endpoint": _as_dict(completion_contract.get("inspection")).get("inspect_endpoint"),
            "inspect_arguments": inspect_args,
            "available": bool(_as_dict(completion_contract.get("inspection")).get("available")),
        },
        "recommended_follow_up_tools": default_follow_up,
        "runtime_state_summary": {
            "payload_state": runtime_state_contract.get("payload_state"),
            "ai_state": runtime_state_contract.get("ai_state"),
            "provider_state": runtime_state_contract.get("provider_state"),
            "document_state": runtime_state_contract.get("document_state"),
            "path_state": runtime_state_contract.get("path_state"),
            "run_state": runtime_state_contract.get("run_state"),
            "operator_state": runtime_state_contract.get("operator_state"),
        },
        "pure_mcp_lab": {
            "display_request_json": True,
            "display_response_json": True,
            "display_primary_field": package_field,
            "display_follow_up_recipe": True,
            "do_not_mix_with_use_answer_demo": True,
        },
    }


def _completion_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    package_field: str,
    package: Any,
    status: str,
    timing: dict[str, Any],
    latency_contract: dict[str, Any],
) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    runtime_boundary = _as_dict(result.get("mcp_runtime_boundary") or planner_runtime.get("mcp_runtime_boundary"))
    context_materialization = _as_dict(result.get("context_package_materialization"))
    package_present = bool(package)
    final_pending = bool(
        result.get("final_materialization_pending")
        or context_materialization.get("final_materialization_pending")
        or runtime_boundary.get("final_materialization_pending")
    )
    raw_state = str(
        result.get("result_materialization_state")
        or planner_runtime.get("result_materialization_state")
        or context_materialization.get("state")
        or runtime_boundary.get("result_materialization_state")
        or ""
    ).strip()
    if status == "blocked":
        state = "blocked"
        reason = str(result.get("stop_reason") or "contract_blocked")
    elif final_pending:
        state = "background_running"
        reason = "first_package_returned_background_completion_pending"
    elif raw_state in {"finalized", "bounded_partial_finalized"} or bool(result.get("result_ready_terminal")) or status == "ok":
        state = "finalized"
        reason = str(result.get("stop_reason") or "final_package_materialized")
    elif package_present:
        state = "first_package_ready"
        reason = "package_available_contract_not_final"
    else:
        state = "waiting"
        reason = "package_not_available"

    first_package_ms = _first_ms(latency_contract.get("first_useful_package_ms"))
    semantic_contract = _as_dict(timing.get("semantic_contract"))
    warm_slo_ms = 2000 if bool(semantic_contract.get("cache_hit")) else 5000
    return {
        "schema_version": MCP_COMPLETION_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "state": state,
        "visible_reason": reason,
        "status": status,
        "raw_result_materialization_state": raw_state or None,
        "final_materialization_pending": final_pending,
        "result_ready_terminal": bool(result.get("result_ready_terminal", state == "finalized")),
        "nonblocking_first_package_returned": bool(runtime_boundary.get("nonblocking_first_package_returned") or final_pending),
        "first_package": {
            "present": package_present,
            "field": package_field,
            "returned_before_full_completion": bool(latency_contract.get("first_package_returned_before_full_completion")),
            "first_useful_package_ms": first_package_ms,
            "slo_ms": warm_slo_ms,
            "under_slo": first_package_ms is not None and first_package_ms <= warm_slo_ms,
        },
        "background_completion": {
            "inspectable": bool(runtime_boundary.get("background_completion_inspectable") or latency_contract.get("background_completion_inspectable") or final_pending),
            "http_response_policy": latency_contract.get("http_response_policy"),
            "full_completion_ms": latency_contract.get("full_completion_ms"),
            "background_completion_ms": latency_contract.get("background_completion_ms"),
            "final_materialization_started_ms": timing.get("final_materialization_started_ms"),
            "final_materialization_completed_ms": timing.get("final_materialization_completed_ms"),
        },
        "inspection": _completion_inspection(tool_name, result.get("search_id")),
        "stage_timings": _as_list(timing.get("stage_timings")),
        "stage_timing_summary": _as_dict(timing.get("stage_timing_summary")),
        "parallelism_contract": {
            "semantic_contract_blocks_route_start": True,
            "route_traversal_not_blocked_by_answer_demo": True,
            "document_lookup_can_complete_after_first_package": True,
            "answer_demo_secondary": True,
            "silent_heuristic_completion_allowed": False,
        },
        "operator_message": (
            "First MCP package returned; poll the inspection tool for final package."
            if state == "background_running"
            else "Final MCP package is available."
            if state == "finalized"
            else "MCP package is blocked by a runtime contract."
            if state == "blocked"
            else "MCP package is not ready yet."
        ),
    }


def _run_lifecycle_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    timing: dict[str, Any],
    completion_contract: dict[str, Any],
    payload_truth_contract: dict[str, Any],
) -> dict[str, Any]:
    semantic_contract = _as_dict(timing.get("semantic_contract"))
    context_materialization = _as_dict(result.get("context_package_materialization"))
    primary_payload = _as_dict(payload_truth_contract.get("primary_mcp_payload"))
    completion_state = str(completion_contract.get("state") or "").strip() or "waiting"
    provider_degraded = bool(semantic_contract.get("provider_degraded") or semantic_contract.get("degraded"))
    ai_required = bool(
        semantic_contract.get("ai_required")
        or _as_dict(result.get("ai_materialization_hard_gate")).get("required")
        or _as_dict(result.get("ai_landing_materialization")).get("required")
    )
    ai_material = bool(
        semantic_contract.get("material")
        or _as_dict(result.get("ai_landing_materialization")).get("materialized")
        or _as_dict(result.get("ai_landing_materialization")).get("route_level_materialized")
        or _ai_hard_gate_satisfied(_as_dict(result.get("ai_materialization_hard_gate")))
    )
    return {
        "schema_version": "agvm.pr12p14q_c.run_lifecycle_contract.v1",
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "status": status,
        "terminal_state": completion_state,
        "terminal": completion_state in {"finalized", "blocked", "failed", "no_match"},
        "provider_state": semantic_contract.get("provider_state") or semantic_contract.get("status") or "unknown",
        "provider_degraded": provider_degraded,
        "ai_required": ai_required,
        "ai_material": ai_material,
        "first_package_present": bool(primary_payload.get("present")),
        "first_package_field": primary_payload.get("field"),
        "first_package_char_count": int(primary_payload.get("char_count") or 0),
        "package_revision_id": context_materialization.get("package_revision_id"),
        "final_materialization_pending": bool(completion_contract.get("final_materialization_pending")),
        "result_ready_terminal": bool(completion_contract.get("result_ready_terminal")),
        "visible_reason": completion_contract.get("visible_reason"),
        "inspection": _as_dict(completion_contract.get("inspection")),
        "surface_law": "context_package.agent_markdown is the MCP payload; hot/cold/docs/answer are separate unless explicitly promoted or requested.",
    }


def _runtime_state_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    package_field: str,
    package: Any,
    timing: dict[str, Any],
    completion_contract: dict[str, Any],
    payload_truth_contract: dict[str, Any],
    run_lifecycle_contract: dict[str, Any],
) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    semantic_contract = _as_dict(
        result.get("semantic_contract_runtime")
        or planner_runtime.get("semantic_contract_runtime")
        or timing.get("semantic_contract")
    )
    semantic_cache = _as_dict(semantic_contract.get("cache"))
    ai_landing = _as_dict(result.get("ai_landing_materialization") or planner_runtime.get("ai_landing_materialization"))
    ai_gate = _as_dict(result.get("ai_materialization_hard_gate") or planner_runtime.get("ai_materialization_hard_gate"))
    answer_demo = _as_dict(result.get("answer_demo_materialization") or planner_runtime.get("answer_demo_materialization"))
    context_materialization = _as_dict(result.get("context_package_materialization") or planner_runtime.get("context_package_materialization"))
    document_readiness = _document_tool_readiness(result)
    path_corridors = _as_dict(result.get("path_corridors"))
    path_metrics = _as_dict(path_corridors.get("metrics"))
    path_lifecycle = _as_dict(path_corridors.get("lifecycle"))
    primary_payload = _as_dict(payload_truth_contract.get("primary_mcp_payload"))
    completion_state = str(completion_contract.get("state") or "").strip() or "waiting"
    raw_provider_state = str(
        semantic_contract.get("provider_state")
        or semantic_contract.get("status")
        or semantic_contract.get("cache_status")
        or ""
    ).strip()
    provider_state_info = _semantic_provider_state(semantic_contract, raw_provider_state)
    provider_degraded = bool(provider_state_info.get("degraded"))
    provider_state = str(provider_state_info.get("state") or "unknown")

    package_present = bool(primary_payload.get("present") or package)
    if status == "failed":
        payload_state = "failed"
    elif status == "no_match":
        payload_state = "no_match"
    elif completion_state == "finalized":
        payload_state = "final_ready"
    elif package_present:
        payload_state = "first_ready"
    else:
        payload_state = "waiting"

    ai_required = bool(
        semantic_contract.get("ai_required")
        or ai_landing.get("required")
        or ai_gate.get("required")
    )
    semantic_material = bool(semantic_contract.get("material"))
    route_material = bool(ai_landing.get("materialized") or ai_landing.get("route_level_materialized"))
    hard_gate_satisfied = _ai_hard_gate_satisfied(ai_gate)
    ai_state_payload = _ai_materialization_state(
        ai_required=ai_required,
        semantic_material=semantic_material,
        route_material=route_material,
        hard_gate_satisfied=hard_gate_satisfied,
        gate_blocked=bool(ai_gate.get("blocked")),
        semantic_runtime=semantic_contract,
        provider_state=provider_state_info,
    )
    ai_state = str(ai_state_payload.get("state") or "not_materialized")
    ai_satisfied = bool(ai_state_payload.get("certifiable"))
    cache_hit = bool(
        semantic_contract.get("cache_hit")
        or semantic_contract.get("cached")
        or semantic_cache.get("hit")
    )
    cache_tier = str(semantic_contract.get("cache_tier") or semantic_cache.get("tier") or "").strip() or None
    source = str(semantic_contract.get("source") or semantic_contract.get("cached_source") or "").strip()
    workspace_kind = str(document_readiness.get("workspace_kind") or "")
    lookup_kind = str(document_readiness.get("document_lookup_kind") or "")
    if (
        lookup_kind == "no_document_found"
        or workspace_kind == "no_document_found"
        or str(document_readiness.get("workspace_status") or "") == "no_document_found"
    ):
        document_state = "no_match"
    elif bool(document_readiness.get("exact_document_ready")):
        document_state = "exact_ready"
    elif int(document_readiness.get("full_text_document_count") or 0) > 0:
        document_state = "raw_ready"
    elif bool(document_readiness.get("workspace_ready")) or int(_as_dict(payload_truth_contract.get("documents")).get("document_ref_count") or 0) > 0:
        document_state = "refs_ready"
    elif tool_name in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES:
        document_state = "pending"
    else:
        document_state = "not_requested"

    completed_paths = int(path_metrics.get("completed_path_count") or 0)
    stopped_paths = int(path_metrics.get("stopped_path_count") or 0)
    pending_paths = int(path_metrics.get("pending_path_count") or 0)
    planned_paths = int(
        path_metrics.get("path_count")
        or path_metrics.get("planned_path_count")
        or len(_as_list(path_corridors.get("corridors")))
        or 0
    )
    if not path_corridors and not planned_paths:
        path_state = "not_requested"
    elif completed_paths and completed_paths >= planned_paths and bool(path_lifecycle.get("all_planned_paths_accounted_for", True)):
        path_state = "completed"
    elif completed_paths or stopped_paths:
        path_state = "partial"
    elif pending_paths:
        path_state = "traversing"
    else:
        path_state = "planned"

    answer_requested = bool(answer_demo.get("requested"))
    answer_raw_state = str(answer_demo.get("state") or "").strip()
    if not answer_requested:
        answer_demo_state = "not_requested"
    elif answer_raw_state in {"ready", "materialized_unsealed", "context_ready_answer_pending"} and not bool(ai_gate.get("blocked")):
        answer_demo_state = "secondary_ready"
    elif answer_raw_state in {"mismatched", "answer_context_mismatch"}:
        answer_demo_state = "mismatched"
    elif bool(ai_gate.get("blocked")) or answer_raw_state.startswith("blocked"):
        answer_demo_state = "blocked"
    else:
        answer_demo_state = "pending"

    final_pending = bool(
        completion_contract.get("final_materialization_pending")
        or context_materialization.get("final_materialization_pending")
        or result.get("final_materialization_pending")
    )
    if status == "failed":
        run_state = "failed"
    elif completion_state in {"finalized", "blocked", "failed", "no_match"}:
        run_state = "terminal"
    elif final_pending or completion_state == "background_running":
        run_state = "background_running"
    elif completion_state == "first_package_ready":
        run_state = "finalizing"
    else:
        run_state = "running"

    blockers: list[str] = []
    if provider_degraded:
        blockers.append("provider_degraded")
    if bool(ai_gate.get("blocked")):
        blockers.extend(str(item) for item in _as_list(ai_gate.get("blockers")) if str(item).strip())
    if status == "blocked" and not blockers:
        blockers.append(str(result.get("stop_reason") or completion_contract.get("visible_reason") or "runtime_contract_blocked"))
    if payload_state in {"first_ready", "final_ready"} and run_state == "terminal" and status == "blocked":
        operator_state = "payload_ready_runtime_blocked"
    elif payload_state == "final_ready" and ai_state in _AI_CERTIFYING_STATES:
        operator_state = "ready"
    elif payload_state == "first_ready" and run_state == "background_running":
        operator_state = "first_payload_ready_background_running"
    elif payload_state in {"first_ready", "final_ready"} and ai_state in {"provider_degraded", "timeout"}:
        operator_state = "payload_ready_ai_degraded"
    elif payload_state == "no_match":
        operator_state = "no_match"
    elif payload_state == "failed" or run_state == "failed":
        operator_state = "failed"
    elif blockers:
        operator_state = "blocked"
    else:
        operator_state = "running"

    if operator_state == "ready":
        operator_message = "Final MCP payload is ready and AI material is available."
    elif operator_state == "first_payload_ready_background_running":
        operator_message = "First MCP payload is ready; background materialization can continue through inspection."
    elif operator_state == "payload_ready_runtime_blocked":
        operator_message = "An MCP payload exists, but a separate runtime or finalization contract is blocked."
    elif operator_state == "payload_ready_ai_degraded":
        operator_message = "An MCP payload exists, but the semantic AI provider degraded during the run."
    elif operator_state == "no_match":
        operator_message = "The retrieval completed with no matching memory or document."
    elif operator_state == "failed":
        operator_message = "The retrieval failed before a usable MCP payload was produced."
    elif operator_state == "blocked":
        operator_message = "The retrieval is blocked by one or more runtime contract axes."
    else:
        operator_message = "The retrieval is still running or waiting for first payload materialization."

    return {
        "schema_version": MCP_RUNTIME_STATE_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "payload_state": payload_state,
        "ai_state": ai_state,
        "provider_state": provider_state,
        "document_state": document_state,
        "path_state": path_state,
        "answer_demo_state": answer_demo_state,
        "run_state": run_state,
        "operator_state": operator_state,
        "operator_message": operator_message,
        "status_legacy": status,
        "completion_state": completion_state,
        "first_payload": {
            "present": package_present,
            "field": primary_payload.get("field") or package_field,
            "char_count": int(primary_payload.get("char_count") or 0),
            "sha256": primary_payload.get("sha256"),
        },
        "ai": {
            "required": ai_required,
            "semantic_material": semantic_material,
            "route_material": route_material,
            "hard_gate_satisfied": _ai_hard_gate_satisfied(ai_gate),
            "hard_gate_blocked": bool(ai_gate.get("blocked")),
            "landing_count": int(ai_landing.get("ai_landing_count") or ai_landing.get("landing_count") or 0),
            "validation_state": ai_landing.get("validation_state") or ai_gate.get("validation_state"),
            "cache_hit": cache_hit,
            "cache_tier": cache_tier,
            "cache_valid_for_ai": bool(_as_dict(ai_state_payload.get("cache_validity")).get("valid_for_ai")),
            "cache_validity_missing": _as_list(_as_dict(ai_state_payload.get("cache_validity")).get("missing")),
            "first_ai_contract_ms": ai_state_payload.get("first_ai_contract_ms"),
            "source": source or None,
        },
        "provider": {
            "raw_state": raw_provider_state or None,
            "degraded": provider_degraded,
            "state": provider_state,
            "timed_out": bool(provider_state_info.get("timed_out")),
            "degraded_reason": semantic_contract.get("degraded_reason"),
            "retry_policy": _as_dict(semantic_contract.get("provider_retry_policy")),
        },
        "documents": {
            "state": document_state,
            "document_count": int(document_readiness.get("document_count") or 0),
            "full_text_document_count": int(document_readiness.get("full_text_document_count") or 0),
            "raw_text_char_count": int(document_readiness.get("raw_text_char_count") or 0),
            "workspace_kind": workspace_kind or None,
            "lookup_kind": lookup_kind or None,
            "follow_up_tool": "retrieve_document",
        },
        "paths": {
            "state": path_state,
            "planned": planned_paths,
            "completed": completed_paths,
            "stopped": stopped_paths,
            "pending": pending_paths,
        },
        "answer_demo": {
            "state": answer_demo_state,
            "requested": answer_requested,
            "raw_state": answer_raw_state or None,
            "secondary": True,
        },
        "run": {
            "state": run_state,
            "terminal": run_state in {"terminal", "failed"},
            "background_inspectable": bool(_as_dict(completion_contract.get("background_completion")).get("inspectable")),
            "inspection": _as_dict(completion_contract.get("inspection")),
            "final_materialization_pending": final_pending,
            "result_ready_terminal": bool(completion_contract.get("result_ready_terminal")),
        },
        "blocking_axes": _dedupe_contract_items(blockers, limit=16),
        "ui_contract": {
            "single_badge_for_ai_forbidden": True,
            "display_payload_state_separately_from_run_state": True,
            "display_provider_state_separately_from_ai_state": True,
            "display_answer_demo_as_secondary": True,
        },
    }


def _ai_materialization_resilience_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    timing: dict[str, Any],
    runtime_state_contract: dict[str, Any],
    completion_contract: dict[str, Any],
) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    semantic_runtime = _as_dict(
        result.get("semantic_contract_runtime")
        or planner_runtime.get("semantic_contract_runtime")
        or timing.get("semantic_contract")
    )
    semantic_cache = _as_dict(semantic_runtime.get("cache"))
    ai_landing = _as_dict(result.get("ai_landing_materialization") or planner_runtime.get("ai_landing_materialization"))
    ai_gate = _as_dict(result.get("ai_materialization_hard_gate") or planner_runtime.get("ai_materialization_hard_gate"))
    retry_policy = _as_dict(semantic_runtime.get("provider_retry_policy"))
    provider_state_raw = str(
        semantic_runtime.get("provider_state")
        or runtime_state_contract.get("provider_state")
        or semantic_runtime.get("status")
        or ""
    ).strip()
    provider_state_info = _semantic_provider_state(semantic_runtime, provider_state_raw)
    provider_degraded = bool(provider_state_info.get("degraded"))
    ai_required = bool(
        semantic_runtime.get("ai_required")
        or ai_landing.get("required")
        or ai_gate.get("required")
        or _as_dict(runtime_state_contract.get("ai")).get("required")
    )
    semantic_material = bool(semantic_runtime.get("material"))
    route_material = bool(
        ai_landing.get("materialized")
        or ai_landing.get("route_level_materialized")
        or _as_dict(runtime_state_contract.get("ai")).get("route_material")
    )
    hard_gate_satisfied = bool(_ai_hard_gate_satisfied(ai_gate) or _as_dict(runtime_state_contract.get("ai")).get("hard_gate_satisfied"))
    ai_materialized = bool(semantic_material or route_material or hard_gate_satisfied)
    cache_hit = bool(
        semantic_runtime.get("cache_hit")
        or semantic_runtime.get("cached_ai_contract")
        or semantic_cache.get("hit")
    )
    cache_tier = str(semantic_runtime.get("cache_tier") or semantic_cache.get("tier") or "").strip() or None
    source = str(semantic_runtime.get("source") or "").strip()
    cached_source = str(semantic_runtime.get("cached_source") or "").strip()
    semantic_status = str(semantic_runtime.get("status") or "").strip()
    gate_blocked = bool(ai_gate.get("blocked"))
    ai_state_payload = _ai_materialization_state(
        ai_required=ai_required,
        semantic_material=semantic_material,
        route_material=route_material,
        hard_gate_satisfied=hard_gate_satisfied,
        gate_blocked=gate_blocked,
        semantic_runtime=semantic_runtime,
        provider_state=provider_state_info,
    )
    blockers: list[str] = []
    if provider_degraded and ai_required and not ai_materialized:
        blockers.append("provider_degraded_without_ai_material")
    if ai_required and not ai_materialized:
        blockers.append("ai_material_missing")
    if gate_blocked:
        blockers.extend(str(item) for item in _as_list(ai_gate.get("blockers")) if str(item).strip())
    if status == "blocked":
        blockers.append(str(result.get("stop_reason") or completion_contract.get("visible_reason") or "runtime_contract_blocked"))

    materialization_source = str(ai_state_payload.get("state") or "not_materialized")

    if materialization_source in {"fresh_llm", "cached_llm", "route_materialized"}:
        operator_label = materialization_source
    elif materialization_source in {"provider_degraded", "timeout"}:
        operator_label = materialization_source
    elif materialization_source == "ai_pending":
        operator_label = "ai_pending"
    elif gate_blocked:
        operator_label = "blocked"
    elif materialization_source == "not_required":
        operator_label = "not_required"
    else:
        operator_label = "missing"

    certifiable = bool(ai_state_payload.get("certifiable"))

    retry_used = bool(
        retry_policy.get("retry_used")
        or semantic_runtime.get("retry_used")
        or int(semantic_runtime.get("attempt_count") or 0) > 1
    )
    return {
        "schema_version": MCP_AI_MATERIALIZATION_RESILIENCE_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "status": status,
        "ai_required": ai_required,
        "ai_materialized": ai_materialized,
        "materialization_source": materialization_source,
        "operator_label": operator_label,
        "readiness_certifiable": certifiable,
        "heuristic_only_certification_allowed": False,
        "silent_heuristic_completion_allowed": False,
        "provider": {
            "state": str(provider_state_info.get("state") or "") or None,
            "raw_state": provider_state_raw or None,
            "degraded": provider_degraded,
            "timed_out": bool(provider_state_info.get("timed_out")),
            "degraded_reason": semantic_runtime.get("degraded_reason"),
            "fresh_provider_call": bool(semantic_runtime.get("fresh_provider_call")),
            "enabled": bool(semantic_runtime.get("enabled")),
            "model": semantic_runtime.get("model"),
            "model_profile": _as_dict(semantic_runtime.get("model_profile")),
        },
        "cache": {
            "enabled": bool(semantic_runtime.get("cache_enabled") or semantic_cache.get("enabled")),
            "hit": cache_hit,
            "status": semantic_runtime.get("cache_status") or semantic_cache.get("status"),
            "tier": cache_tier,
            "cached_source": cached_source or None,
            "cached_status": semantic_runtime.get("cached_status"),
            "cache_age_ms": _as_ms(semantic_runtime.get("cache_age_ms")),
            "hit_count": int(semantic_runtime.get("cache_hit_count") or 0),
            "key_fingerprint": semantic_runtime.get("cache_key_fingerprint") or semantic_cache.get("key_fingerprint"),
            "brain_revision": semantic_runtime.get("brain_revision") or semantic_cache.get("brain_revision"),
            "cache_scope": semantic_runtime.get("cache_scope") or semantic_cache.get("cache_scope"),
            "valid_for_ai": bool(_as_dict(ai_state_payload.get("cache_validity")).get("valid_for_ai")),
            "validity_missing": _as_list(_as_dict(ai_state_payload.get("cache_validity")).get("missing")),
        },
        "retry": {
            "policy": retry_policy,
            "used": retry_used,
            "status": retry_policy.get("retry_status") or semantic_runtime.get("retry_status"),
            "attempt_count": int(semantic_runtime.get("attempt_count") or (2 if retry_used else 1)),
            "primary_error": semantic_runtime.get("primary_error") or retry_policy.get("primary_error"),
            "retry_error": semantic_runtime.get("retry_error") or retry_policy.get("retry_error"),
            "retry_timeout_seconds": semantic_runtime.get("retry_timeout_seconds") or retry_policy.get("retry_timeout_seconds"),
            "benchmark_retry_allowed": bool(retry_policy.get("benchmark_retry_allowed")),
        },
        "semantic_contract": {
            "status": semantic_status or None,
            "source": source or None,
            "material": semantic_material,
            "compiler_ms": _as_ms(semantic_runtime.get("compiler_ms")),
            "material_source_valid_for_ai": materialization_source in {"fresh_llm", "cached_llm"},
            "first_ai_contract_ms": ai_state_payload.get("first_ai_contract_ms"),
            "cache_valid_for_ai": bool(_as_dict(ai_state_payload.get("cache_validity")).get("valid_for_ai")),
        },
        "route_materialization": {
            "material": route_material,
            "landing_count": int(ai_landing.get("ai_landing_count") or ai_landing.get("landing_count") or 0),
            "validation_state": ai_landing.get("validation_state") or ai_gate.get("validation_state"),
            "hard_gate_satisfied": hard_gate_satisfied,
            "hard_gate_blocked": gate_blocked,
        },
        "blockers": _dedupe_contract_items(blockers, limit=16),
        "ui_contract": {
            "display_as_dedicated_ai_materialization_panel": True,
            "do_not_merge_with_answer_demo": True,
            "show_cache_retry_provider_as_separate_rows": True,
            "normal_use_badge": operator_label,
            "blocked_without_ai_material_must_remain_red": bool(ai_required and not ai_materialized),
        },
    }


def _semantic_contract_payload(result: dict[str, Any], timing: dict[str, Any] | None = None) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    return _as_dict(
        result.get("semantic_contract")
        or planner_runtime.get("semantic_contract")
    )


def _compact_contract_field_presence(semantic_contract: dict[str, Any]) -> dict[str, Any]:
    contract = _as_dict(semantic_contract)
    context_contract = _as_dict(contract.get("context_contract"))
    landing_plan = _as_dict(contract.get("landing_plan"))
    intent = _as_dict(contract.get("intent"))
    semantic_slots = [
        _as_dict(item)
        for item in _as_list(contract.get("semantic_slot_contracts"))
        if isinstance(item, dict)
    ]
    required_slots = _dedupe_contract_items(
        _as_list(context_contract.get("semantic_required_slot_keys"))
        + _as_list(context_contract.get("required_sections"))
        + [
            item.get("slot_key") or item.get("slot_id") or item.get("section")
            for item in semantic_slots
            if bool(item.get("required"))
        ],
        limit=32,
    )
    landing_hypotheses = _as_list(landing_plan.get("landing_hypotheses") or landing_plan.get("landings"))
    path_goals = _as_list(landing_plan.get("paths") or contract.get("paths"))
    forbidden_evidence = _as_list(contract.get("forbidden_evidence"))
    if not forbidden_evidence:
        forbidden_evidence = [
            {"topic": item}
            for slot in semantic_slots
            for item in _as_list(slot.get("forbidden_evidence") or slot.get("negative_conditions"))
        ]
    stop_contract = _as_dict(contract.get("stop_contract"))
    present = {
        "intent": bool(intent),
        "required_slots": bool(required_slots),
        "landing_hypotheses": bool(landing_hypotheses),
        "path_goals": bool(path_goals),
        "forbidden_evidence": bool(forbidden_evidence),
        "stop_contract": bool(stop_contract),
    }
    missing = [key for key, value in present.items() if not value]
    return {
        "present": present,
        "missing": missing,
        "counts": {
            "required_slot_count": len(required_slots),
            "landing_hypothesis_count": len(landing_hypotheses),
            "path_goal_count": len(path_goals),
            "forbidden_evidence_count": len(forbidden_evidence),
            "stop_required_pass_count": len(_as_list(stop_contract.get("required_passes"))),
        },
        "required_slots": required_slots[:24],
        "intent_primary": intent.get("primary"),
        "intent_secondary": _as_list(intent.get("secondary"))[:12],
    }


def _mcp_record_families(record: dict[str, Any], *, default: str = "heuristic") -> set[str]:
    row = _as_dict(record)
    families: set[str] = set()
    for raw in _as_list(row.get("origin_families")):
        family = str(raw or "").strip().lower()
        if family in {"ai", "heuristic"}:
            families.add(family)
    for key in _as_dict(row.get("source_probe_ids_by_family")).keys():
        family = str(key or "").strip().lower()
        if family in {"ai", "heuristic"}:
            families.add(family)
    for key in ("planner_family", "family_attribution"):
        family = str(row.get(key) or "").strip().lower()
        if family in {"ai", "heuristic"}:
            families.add(family)
    if not families:
        landing_basis = str(row.get("landing_basis") or row.get("worker_kind") or "").strip().lower()
        if "llm" in landing_basis or "ai" in landing_basis:
            families.add("ai")
    if not families:
        fallback = str(default or "heuristic").strip().lower()
        families.add("ai" if fallback == "ai" else "heuristic")
    return families


def _mcp_family_count(rows: list[Any], family: str) -> int:
    normalized_family = str(family or "").strip().lower()
    return sum(
        1
        for row in rows
        if isinstance(row, dict) and normalized_family in _mcp_record_families(row)
    )


def _mcp_mode_route_budget(mode: str) -> dict[str, int]:
    normalized = str(mode or "balanced").strip().lower()
    if normalized == "flash":
        return {"landing_family_min": 1, "landing_family_max": 3, "path_max": 3, "node_budget": 36}
    if normalized == "heavy":
        return {"landing_family_min": 6, "landing_family_max": 12, "path_max": 12, "node_budget": 192}
    if normalized == "forensic":
        return {"landing_family_min": 6, "landing_family_max": 16, "path_max": 18, "node_budget": 320}
    return {"landing_family_min": 3, "landing_family_max": 6, "path_max": 6, "node_budget": 96}


def _compact_route_plan_summary(semantic_contract: dict[str, Any], *, mode: str) -> dict[str, Any]:
    contract = _as_dict(semantic_contract)
    landing_plan = _as_dict(contract.get("landing_plan"))
    landing_rows = [
        _as_dict(item)
        for item in _as_list(landing_plan.get("landing_hypotheses") or landing_plan.get("landings"))
        if isinstance(item, dict)
    ]
    path_rows = [
        _as_dict(item)
        for item in _as_list(landing_plan.get("paths") or contract.get("paths"))
        if isinstance(item, dict)
    ]
    bridge_rows = [
        row
        for row in path_rows
        if str(row.get("route_kind") or "").strip().lower() == "explicit_cross_landing_bridge"
    ]
    field_presence = _compact_contract_field_presence(contract)
    mode_budget = _mcp_mode_route_budget(mode)
    requested_node_budget = 0
    for landing in landing_rows:
        route_budget = _as_dict(landing.get("route_budget"))
        try:
            requested_node_budget += max(0, int(route_budget.get("max_nodes") or 0))
        except (TypeError, ValueError):
            continue
    for path in path_rows:
        try:
            requested_node_budget += max(0, int(path.get("max_intermediate_nodes") or 0))
        except (TypeError, ValueError):
            continue
    if requested_node_budget <= 0:
        requested_node_budget = min(
            mode_budget["node_budget"],
            max(1, len(path_rows) or len(landing_rows)) * 12,
        )
    landing_families: list[dict[str, Any]] = []
    paths_by_landing: dict[str, int] = {}
    for path in path_rows:
        landing_id = str(path.get("from_landing_id") or path.get("origin_landing_id") or "").strip()
        if landing_id:
            paths_by_landing[landing_id] = paths_by_landing.get(landing_id, 0) + 1
    for index, landing in enumerate(landing_rows, start=1):
        landing_id = str(landing.get("landing_id") or f"L{index}").strip() or f"L{index}"
        landing_families.append(
            {
                "landing_id": landing_id,
                "target_evidence_ids": _dedupe_contract_items(_as_list(landing.get("target_evidence_ids")), limit=8),
                "textual_probe": str(landing.get("textual_probe") or "")[:240],
                "path_goal_count": paths_by_landing.get(landing_id, 0),
                "route_budget": _as_dict(landing.get("route_budget")),
            }
        )
    path_goals: list[dict[str, Any]] = []
    for index, path in enumerate(path_rows, start=1):
        path_goals.append(
            {
                "path_id": str(path.get("path_id") or f"P{index}").strip() or f"P{index}",
                "route_kind": str(path.get("route_kind") or "landing_origin_corridor").strip() or "landing_origin_corridor",
                "from_landing_id": str(path.get("from_landing_id") or path.get("origin_landing_id") or "").strip() or None,
                "to_landing_id": str(path.get("to_landing_id") or path.get("target_landing_id") or "").strip() or None,
                "read_intermediate_nodes": bool(path.get("read_intermediate_nodes", True)),
                "max_intermediate_nodes": path.get("max_intermediate_nodes"),
                "preferred_edges": _dedupe_contract_items(_as_list(path.get("preferred_edges")), limit=6),
            }
        )
    return {
        "present": bool(contract and landing_rows and path_rows),
        "mode": str(mode or "balanced").strip() or "balanced",
        "mode_budget": mode_budget,
        "preferred_strategy": landing_plan.get("preferred_strategy"),
        "min_landings": landing_plan.get("min_landings"),
        "max_landings": landing_plan.get("max_landings"),
        "landing_families": landing_families[:16],
        "path_goals": path_goals[:24],
        "counts": {
            "landing_family_count": len(landing_rows),
            "path_goal_count": len(path_rows),
            "bridge_goal_count": len(bridge_rows),
            "forbidden_region_count": len(_as_list(contract.get("forbidden_evidence"))),
            "required_slot_count": int(_as_dict(field_presence.get("counts")).get("required_slot_count") or 0),
            "requested_node_budget": requested_node_budget,
        },
        "required_slots": _as_list(field_presence.get("required_slots"))[:24],
        "forbidden_regions": [
            str(_as_dict(item).get("topic") or item).strip()
            for item in _as_list(contract.get("forbidden_evidence"))[:16]
            if str(_as_dict(item).get("topic") or item).strip()
        ],
        "stop_contract_present": bool(_as_dict(contract.get("stop_contract"))),
    }


def _route_arbitration_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    runtime_state_contract: dict[str, Any],
    ai_critical_path_contract: dict[str, Any],
) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    semantic_contract = _semantic_contract_payload(result)
    ai_spatial_landing_contract = _as_dict(
        result.get("ai_spatial_landing_contract") or planner_runtime.get("ai_spatial_landing_contract")
    )
    spatial_metrics = _as_dict(ai_spatial_landing_contract.get("metrics"))
    mode = str(
        result.get("retrieval_mode")
        or _as_dict(_as_dict(result.get("semantic_contract_runtime")).get("model_profile")).get("retrieval_mode")
        or ""
    ).strip() or "balanced"
    route_plan = _compact_route_plan_summary(semantic_contract, mode=mode)
    branches = [_as_dict(item) for item in _as_list(result.get("branches")) if isinstance(item, dict)]
    probes = [_as_dict(item) for item in _as_list(result.get("probes")) if isinstance(item, dict)]
    landings = [_as_dict(item) for item in _as_list(result.get("landing_metadata")) if isinstance(item, dict)]
    steps = [_as_dict(item) for item in _as_list(result.get("steps")) if isinstance(item, dict)]
    route_truth = _as_dict(result.get("route_truth_summary") or planner_runtime.get("route_truth_summary"))
    path_corridors = _as_dict(result.get("path_corridors"))
    path_metrics = _as_dict(path_corridors.get("metrics"))
    context_package = _as_dict(result.get("context_package"))
    context_contract = _as_dict(context_package.get("contract"))
    ai_critical = _as_dict(ai_critical_path_contract)
    ai_runtime = _as_dict(runtime_state_contract.get("ai"))
    ai_required = bool(ai_critical.get("ai_required") or ai_runtime.get("required"))
    ai_state = str(ai_critical.get("state") or runtime_state_contract.get("ai_state") or "not_materialized").strip()
    compact_certifiable = bool(ai_critical.get("certifiable"))
    contract_payload_present = bool(semantic_contract)
    route_material = bool(ai_runtime.get("route_material") or _as_dict(result.get("ai_landing_materialization")).get("route_level_materialized"))
    ai_branch_count = _mcp_family_count(branches, "ai")
    heuristic_branch_count = _mcp_family_count(branches, "heuristic")
    ai_probe_count = _mcp_family_count(probes, "ai")
    heuristic_probe_count = _mcp_family_count(probes, "heuristic")
    ai_landing_count = _mcp_family_count(landings, "ai")
    heuristic_landing_count = _mcp_family_count(landings, "heuristic")
    dual_origin_branch_count = sum(
        1
        for branch in branches
        if {"ai", "heuristic"}.issubset(_mcp_record_families(branch))
    )
    route_step_count = int(route_truth.get("route_step_count") or len(steps))
    travel_step_count = int(route_truth.get("travel_step_count") or 0)
    path_budget_used = int(
        path_metrics.get("completed_path_count")
        or path_metrics.get("path_count")
        or route_truth.get("path_count")
        or len(_as_list(path_corridors.get("paths")))
        or 0
    )
    path_budget_requested = int(_as_dict(route_plan.get("counts")).get("path_goal_count") or 0)
    unresolved_slots = _dedupe_contract_items(_as_list(context_contract.get("unresolved_sections")), limit=24)
    ai_family_runtime_present = bool(ai_branch_count or ai_probe_count or ai_landing_count)
    ai_plan_present = bool(route_plan.get("present") or ai_spatial_landing_contract.get("materialized"))
    route_plan_counts = _as_dict(route_plan.get("counts"))
    planned_ai_landing_count = max(
        int(route_plan_counts.get("landing_family_count") or 0),
        int(spatial_metrics.get("ai_landing_count") or 0),
    )
    planned_ai_path_count = max(
        int(route_plan_counts.get("path_goal_count") or 0),
        int(spatial_metrics.get("ai_path_count") or 0),
    )
    bridge_goal_count = int(_as_dict(route_plan.get("counts")).get("bridge_goal_count") or 0)
    if not ai_required:
        arbitration_state = "not_required"
    elif ai_state in {"provider_degraded", "timeout", "not_materialized"}:
        arbitration_state = "ai_not_materialized"
    elif not contract_payload_present and (ai_family_runtime_present or route_material):
        arbitration_state = "runtime_arbitrated_contract_payload_unavailable"
    elif not compact_certifiable:
        arbitration_state = "compact_contract_not_certifiable"
    elif not ai_plan_present:
        arbitration_state = "ai_route_plan_missing"
    elif ai_family_runtime_present or route_material:
        arbitration_state = "arbitrated"
    else:
        arbitration_state = "ai_plan_ready_runtime_pending"
    blockers: list[Any] = []
    pending: list[Any] = []
    if ai_required and contract_payload_present and not compact_certifiable:
        blockers.append("compact_ai_contract_not_certifiable")
    if ai_required and contract_payload_present and not ai_plan_present:
        blockers.append("ai_route_plan_missing")
    if ai_required and not contract_payload_present and (ai_family_runtime_present or route_material):
        pending.append("compact_route_contract_payload_unavailable_legacy_runtime")
    if ai_required and ai_plan_present and not (ai_family_runtime_present or route_material):
        pending.append("ai_route_runtime_pending")
    if ai_required and path_budget_requested and path_budget_used == 0 and route_step_count == 0:
        pending.append("path_budget_not_traversed_yet")
    certifiable_for_first_payload = bool(
        (not ai_required)
        or (
            not contract_payload_present
            and ai_state in _AI_CERTIFYING_STATES
            and (route_material or ai_family_runtime_present)
        )
        or (
            compact_certifiable
            and ai_plan_present
            and ai_state in _AI_CERTIFYING_STATES
            and (route_material or ai_family_runtime_present or path_budget_requested > 0)
        )
    )
    runtime_arbitrated = bool(ai_family_runtime_present or route_material)
    if (
        ai_required
        and contract_payload_present
        and not certifiable_for_first_payload
        and arbitration_state not in {"ai_plan_ready_runtime_pending"}
    ):
        blockers.append("route_arbitration_not_certifiable")
    family_summary = {
        "ai": {
            "probe_count": ai_probe_count,
            "landing_count": max(ai_landing_count, planned_ai_landing_count),
            "runtime_landing_count": ai_landing_count,
            "planned_landing_count": planned_ai_landing_count,
            "planned_path_count": planned_ai_path_count,
            "branch_count": ai_branch_count,
            "route_step_ratio": float(route_truth.get("ai_family_route_step_ratio") or 0.0),
            "count_source": "runtime_and_semantic_contract_plan" if planned_ai_landing_count else "runtime",
        },
        "heuristic": {
            "probe_count": heuristic_probe_count,
            "landing_count": heuristic_landing_count,
            "branch_count": heuristic_branch_count,
            "route_step_ratio": float(route_truth.get("heuristic_family_route_step_ratio") or 0.0),
        },
        "dual_origin": {
            "branch_count": dual_origin_branch_count,
            "route_step_ratio": float(route_truth.get("dual_origin_family_route_step_ratio") or 0.0),
        },
    }
    return {
        "schema_version": MCP_ROUTE_ARBITRATION_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "status": status,
        "state": arbitration_state,
        "ai_required": ai_required,
        "ai_state": ai_state,
        "contract_payload_present": contract_payload_present,
        "certifiable_for_first_payload": certifiable_for_first_payload,
        "runtime_arbitrated": runtime_arbitrated,
        "heuristic_can_certify": False,
        "ai_owns_required_slots_landing_path_stop": bool(ai_required and compact_certifiable and ai_plan_present),
        "route_plan": route_plan,
        "path_budget": {
            "mode": mode,
            "requested_landing_families": planned_ai_landing_count,
            "requested_paths": path_budget_requested,
            "used_paths": path_budget_used,
            "route_steps": route_step_count,
            "travel_steps": travel_step_count,
            "bridge_goal_count": bridge_goal_count,
            "unresolved_slot_expansion_count": len(unresolved_slots),
            "unresolved_slots": unresolved_slots,
        },
        "candidate_families": family_summary,
        "candidate_classification": {
            "ai_seed": ai_probe_count + max(ai_landing_count, planned_ai_landing_count),
            "heuristic_support": heuristic_probe_count + heuristic_landing_count,
            "bridge_support": bridge_goal_count + dual_origin_branch_count,
            "reservoir": int(_as_dict(context_package.get("metrics")).get("cold_item_count") or 0),
            "forbidden": int(_as_dict(route_plan.get("counts")).get("forbidden_region_count") or 0),
        },
        "promotion_policy": {
            "ai_contract_required_before_certification": True,
            "heuristic_must_match_ai_contract_or_stay_support": True,
            "llm_per_node_arbitration_allowed": False,
            "deterministic_slot_matching_after_ai_plan": True,
            "reservoir_not_primary_payload": True,
        },
        "pending_reasons": _dedupe_contract_items(pending, limit=16),
        "blockers": _dedupe_contract_items(blockers, limit=24),
        "ui_contract": {
            "show_ai_and_heuristic_counts_separately": True,
            "show_path_budget_requested_vs_used": True,
            "show_runtime_arbitrated_vs_plan_only": True,
            "never_render_heuristic_support_as_ai_landing": True,
        },
    }


def _ai_critical_path_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    timing: dict[str, Any],
    runtime_state_contract: dict[str, Any],
    ai_materialization_resilience_contract: dict[str, Any],
) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    semantic_runtime = _as_dict(
        result.get("semantic_contract_runtime")
        or planner_runtime.get("semantic_contract_runtime")
        or timing.get("semantic_contract")
    )
    semantic_contract = _semantic_contract_payload(result, timing)
    field_presence = _compact_contract_field_presence(semantic_contract)
    ai_resilience = _as_dict(ai_materialization_resilience_contract)
    runtime_ai = _as_dict(runtime_state_contract.get("ai"))
    provider_state_info = _semantic_provider_state(semantic_runtime, _as_dict(ai_resilience.get("provider")).get("raw_state"))
    ai_landing = _as_dict(result.get("ai_landing_materialization") or planner_runtime.get("ai_landing_materialization"))
    ai_gate = _as_dict(result.get("ai_materialization_hard_gate") or planner_runtime.get("ai_materialization_hard_gate"))
    ai_required = bool(
        ai_resilience.get("ai_required")
        or runtime_ai.get("required")
        or semantic_runtime.get("ai_required")
        or ai_landing.get("required")
        or ai_gate.get("required")
    )
    semantic_material = bool(semantic_runtime.get("material"))
    route_material = bool(
        ai_landing.get("materialized")
        or ai_landing.get("route_level_materialized")
        or runtime_ai.get("route_material")
    )
    hard_gate_satisfied = bool(_ai_hard_gate_satisfied(ai_gate) or runtime_ai.get("hard_gate_satisfied"))
    ai_state_payload = _ai_materialization_state(
        ai_required=ai_required,
        semantic_material=semantic_material,
        route_material=route_material,
        hard_gate_satisfied=hard_gate_satisfied,
        gate_blocked=bool(ai_gate.get("blocked")),
        semantic_runtime=semantic_runtime,
        provider_state=provider_state_info,
    )
    state = str(ai_state_payload.get("state") or "not_materialized")
    contract_payload_present = bool(semantic_contract)
    compact_contract_ready = bool(contract_payload_present and not field_presence["missing"])
    compact_contract_required = bool(ai_required and state in {"fresh_llm", "cached_llm"})
    blockers: list[Any] = []
    if compact_contract_required and not contract_payload_present:
        blockers.append("compact_ai_contract_payload_missing")
    if compact_contract_required and contract_payload_present and not compact_contract_ready:
        blockers.append("compact_ai_contract_incomplete")
        blockers.extend(f"compact_field_missing:{item}" for item in field_presence["missing"])
    if ai_required and not bool(ai_state_payload.get("certifiable")):
        blockers.append("ai_material_not_certifiable")
    certifiable = bool(ai_state_payload.get("certifiable"))
    if compact_contract_required:
        certifiable = bool(certifiable and compact_contract_ready)
    semantic_cache = _as_dict(semantic_runtime.get("cache"))
    cache_hit = bool(
        semantic_runtime.get("cache_hit")
        or semantic_runtime.get("cached_ai_contract")
        or semantic_cache.get("hit")
    )
    mode = str(
        result.get("retrieval_mode")
        or semantic_runtime.get("retrieval_mode")
        or _as_dict(semantic_runtime.get("model_profile")).get("retrieval_mode")
        or ""
    ).strip()
    mode_slo_ms = {
        "flash": 1500,
        "balanced": 5000,
        "heavy": 9000,
        "forensic": 15000,
    }.get(mode, 5000)
    first_ai_ms = _as_ms(semantic_runtime.get("compiler_ms"))
    return {
        "schema_version": MCP_AI_CRITICAL_PATH_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "status": status,
        "critical_path_role": "certification_gate",
        "ai_required": ai_required,
        "state": state,
        "certifiable": certifiable,
        "compact_contract_required": compact_contract_required,
        "contract_payload_present": contract_payload_present,
        "compact_contract_ready": compact_contract_ready,
        "compact_contract_fields": field_presence["present"],
        "missing_compact_fields": field_presence["missing"],
        "counts": field_presence["counts"],
        "intent": {
            "primary": field_presence["intent_primary"],
            "secondary": field_presence["intent_secondary"],
            "required_slots": field_presence["required_slots"],
        },
        "latency": {
            "first_ai_contract_ms": first_ai_ms,
            "mode_slo_ms": mode_slo_ms,
            "under_mode_slo": first_ai_ms is not None and first_ai_ms <= mode_slo_ms,
        },
        "cache": {
            "hit": cache_hit,
            "valid_for_ai": bool(_as_dict(ai_state_payload.get("cache_validity")).get("valid_for_ai")),
            "validity_missing": _as_list(_as_dict(ai_state_payload.get("cache_validity")).get("missing")),
            "age_ms": _as_ms(semantic_runtime.get("cache_age_ms")),
            "tier": semantic_runtime.get("cache_tier") or semantic_cache.get("tier"),
            "key_fingerprint": semantic_runtime.get("cache_key_fingerprint") or semantic_cache.get("key_fingerprint"),
            "brain_revision": semantic_runtime.get("brain_revision") or semantic_cache.get("brain_revision"),
            "model_profile": _as_dict(semantic_runtime.get("model_profile")),
        },
        "parallelism_contract": {
            "heuristic_can_start_before_ai_contract": True,
            "heuristic_can_certify": False,
            "heuristic_only_payload_must_be_partial_or_blocked": True,
            "ai_contract_must_own_intent_landing_path_stop": True,
        },
        "blockers": _dedupe_contract_items(blockers, limit=24),
        "ui_contract": {
            "show_as_ai_critical_path_panel": True,
            "show_compact_contract_missing_fields": True,
            "show_cache_vs_fresh_ai": True,
            "never_hide_heuristic_only_state": True,
        },
    }


def _first_package_background_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    package_field: str,
    timing: dict[str, Any],
    completion_contract: dict[str, Any],
    runtime_state_contract: dict[str, Any],
    tool_boundary_contract: dict[str, Any],
) -> dict[str, Any]:
    planner_runtime = _as_dict(result.get("planner_runtime"))
    runtime_boundary = _as_dict(result.get("mcp_runtime_boundary") or planner_runtime.get("mcp_runtime_boundary"))
    context_materialization = _as_dict(result.get("context_package_materialization"))
    first_payload = _as_dict(runtime_state_contract.get("first_payload"))
    completion_first = _as_dict(completion_contract.get("first_package"))
    completion_background = _as_dict(completion_contract.get("background_completion"))
    completion_inspection = _as_dict(completion_contract.get("inspection"))
    raw_materialization_state = str(
        result.get("result_materialization_state")
        or planner_runtime.get("result_materialization_state")
        or runtime_boundary.get("result_materialization_state")
        or context_materialization.get("state")
        or ""
    ).strip()
    final_pending = bool(
        result.get("final_materialization_pending")
        or context_materialization.get("final_materialization_pending")
        or runtime_boundary.get("final_materialization_pending")
        or completion_contract.get("final_materialization_pending")
    )
    result_terminal = bool(
        result.get("result_ready_terminal")
        or planner_runtime.get("result_ready_terminal")
        or completion_contract.get("result_ready_terminal")
    )
    first_package_present = bool(first_payload.get("present") or completion_first.get("present"))
    nonblocking = bool(
        runtime_boundary.get("nonblocking_first_package_returned")
        or completion_contract.get("nonblocking_first_package_returned")
        or completion_first.get("returned_before_full_completion")
        or final_pending
        or raw_materialization_state in {"first_package_ready_background_running", "snapshot_ready"}
    )
    completion_state = str(completion_contract.get("state") or "").strip()
    if status == "failed":
        background_state = "failed"
    elif status == "blocked" and final_pending:
        background_state = "blocked_pending_inspection"
    elif status == "blocked":
        background_state = "blocked"
    elif final_pending or completion_state == "background_running":
        background_state = "running"
    elif result_terminal or completion_state == "finalized" or raw_materialization_state == "finalized":
        background_state = "finalized"
    elif first_package_present:
        background_state = "not_started_or_not_observed"
    else:
        background_state = "waiting_for_first_package"

    if background_state == "running":
        stream_state = "open_background_running"
        final_seal_state = "pending"
        operator_state = "first_payload_ready_background_running"
    elif background_state == "finalized":
        stream_state = "closed_finalized"
        final_seal_state = "done"
        operator_state = "finalized"
    elif background_state.startswith("blocked"):
        stream_state = "closed_or_blocked"
        final_seal_state = "blocked"
        operator_state = "blocked"
    elif background_state == "failed":
        stream_state = "closed_failed"
        final_seal_state = "failed"
        operator_state = "failed"
    elif first_package_present:
        stream_state = "first_package_ready_stream_not_observed"
        final_seal_state = "unknown"
        operator_state = "first_payload_ready"
    else:
        stream_state = "waiting"
        final_seal_state = "waiting"
        operator_state = "waiting_for_first_payload"

    search_id = str(result.get("search_id") or "").strip()
    inspect_endpoint = str(completion_inspection.get("inspect_endpoint") or "/mcp/inspect-context-package")
    query_result_endpoint = f"/memory/query-result/{search_id}" if search_id else None
    trace_endpoint = f"/memory/get-trace/{search_id}" if search_id else None
    stream_endpoint = f"/memory/query-stream/{search_id}" if search_id else None
    can_reattach = bool(search_id and (first_package_present or result_terminal or final_pending or completion_inspection.get("available")))
    normalized_tool = str(tool_name or "").strip()
    context_package = _as_dict(result.get("context_package"))
    context_contract = _as_dict(context_package.get("contract"))
    unresolved_sections = _as_list(context_contract.get("unresolved_sections"))
    context_contract_passed = bool(
        context_contract.get("passed")
        or (context_materialization.get("contract_passed") and not unresolved_sections)
    )
    runtime_ai = _as_dict(runtime_state_contract.get("ai"))
    ai_required = bool(runtime_ai.get("required"))
    ai_state = str(runtime_state_contract.get("ai_state") or "").strip()
    ai_available = bool((not ai_required) or ai_state in _AI_CERTIFYING_STATES)
    document_state = str(runtime_state_contract.get("document_state") or "").strip()
    path_state = str(runtime_state_contract.get("path_state") or "").strip()
    is_context_tool = normalized_tool in {"retrieve_context", "inspect_context_package"}
    is_document_tool = normalized_tool in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES
    is_path_tool = normalized_tool in {"retrieve_path_corridor", "inspect_path_corridor"}
    first_package_document_refs_terminal = False
    if normalized_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES:
        semantic_runtime = _as_dict(
            result.get("semantic_contract_runtime")
            or _as_dict(result.get("planner_runtime")).get("semantic_contract_runtime")
        )
        semantic_contract = _as_dict(
            result.get("semantic_contract")
            or _as_dict(result.get("planner_runtime")).get("semantic_contract")
            or _as_dict(semantic_runtime.get("semantic_contract"))
        )
        context_package = _as_dict(result.get("context_package"))
        document_workspace = _result_document_workspace(result)
        first_package_document_refs_terminal = bool(
            _document_workspace_refs_terminality_contract(
                result,
                tool_name=normalized_tool,
                target_document_need_contract=_as_dict(
                    result.get("target_document_need_contract")
                    or semantic_contract.get("target_document_need_contract")
                    or _as_dict(semantic_contract.get("document_contract")).get("target_document_need_contract")
                ),
                master_judgement=_as_dict(
                    result.get("master_judgement")
                    or context_package.get("master_judgement")
                    or _as_dict(result.get("planner_runtime")).get("master_judgement")
                ),
                document_ref_contract=_as_dict(
                    result.get("document_ref_contract")
                    or context_package.get("document_ref_contract")
                    or document_workspace.get("document_ref_contract")
                ),
                document_delivery_contract=_as_dict(
                    result.get("document_delivery_contract")
                    or context_package.get("document_delivery_contract")
                    or document_workspace.get("document_delivery_contract")
                ),
            ).get("terminal_for_client")
        )

    if status in {"blocked", "failed"}:
        first_package_client_usable = False
    elif normalized_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES:
        first_package_client_usable = bool(first_package_present and first_package_document_refs_terminal)
    elif is_document_tool:
        first_package_client_usable = bool(first_package_present and document_state in {"exact_ready", "raw_ready", "refs_ready"})
    elif is_path_tool:
        first_package_client_usable = bool(first_package_present and path_state == "completed")
    elif is_context_tool:
        first_package_client_usable = bool(first_package_present and context_contract_passed and ai_available)
    else:
        first_package_client_usable = bool(first_package_present and ai_available)

    if status == "no_match":
        first_response_terminal = True
        first_response_terminal_reason = "no_match_terminal_result"
    elif first_package_client_usable:
        first_response_terminal = True
        first_response_terminal_reason = "first_mcp_payload_usable_for_client"
    elif status == "failed":
        first_response_terminal = False
        first_response_terminal_reason = "failed_before_usable_mcp_payload"
    elif status == "blocked":
        first_response_terminal = False
        first_response_terminal_reason = "runtime_blocked_payload_inspectable_only"
    elif not first_package_present:
        first_response_terminal = False
        first_response_terminal_reason = "first_mcp_payload_not_available"
    elif is_context_tool and not ai_available:
        first_response_terminal = False
        first_response_terminal_reason = "ai_material_pending_or_missing"
    elif is_context_tool and not context_contract_passed:
        first_response_terminal = False
        first_response_terminal_reason = "context_contract_not_passed"
    elif unresolved_sections:
        first_response_terminal = False
        first_response_terminal_reason = "unresolved_context_sections"
    elif is_document_tool:
        first_response_terminal = False
        first_response_terminal_reason = "document_payload_not_ready"
    elif is_path_tool:
        first_response_terminal = False
        first_response_terminal_reason = "path_corridor_not_completed"
    else:
        first_response_terminal = False
        first_response_terminal_reason = "first_mcp_payload_available_but_partial"
    return {
        "schema_version": MCP_FIRST_PACKAGE_BACKGROUND_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": result.get("search_id"),
        "status": status,
        "operator_state": operator_state,
        "first_response_payload_available": first_package_present,
        "first_response_terminal": first_response_terminal,
        "first_response_terminal_reason": first_response_terminal_reason,
        "first_package": {
            "present": first_package_present,
            "field": first_payload.get("field") or completion_first.get("field") or package_field,
            "char_count": int(first_payload.get("char_count") or 0),
            "sha256": first_payload.get("sha256"),
            "first_useful_package_ms": completion_first.get("first_useful_package_ms"),
            "slo_ms": completion_first.get("slo_ms"),
            "under_slo": bool(completion_first.get("under_slo")),
            "returned_before_full_completion": nonblocking,
            "available_for_inspection": first_package_present,
            "terminal_for_mcp_client": first_package_client_usable,
        },
        "background_completion": {
            "state": background_state,
            "pending": final_pending,
            "inspectable": bool(completion_background.get("inspectable") or runtime_boundary.get("background_completion_inspectable") or can_reattach),
            "http_response_policy": completion_background.get("http_response_policy") or runtime_boundary.get("http_response_policy"),
            "full_completion_ms": completion_background.get("full_completion_ms"),
            "background_completion_ms": completion_background.get("background_completion_ms"),
            "final_materialization_started_ms": completion_background.get("final_materialization_started_ms") or timing.get("final_materialization_started_ms"),
            "final_materialization_completed_ms": completion_background.get("final_materialization_completed_ms") or timing.get("final_materialization_completed_ms"),
            "raw_result_materialization_state": raw_materialization_state or None,
            "result_ready_terminal": result_terminal,
        },
        "stream_vs_final": {
            "stream_state": stream_state,
            "final_seal_state": final_seal_state,
            "stream_done_is_not_final_seal": True,
            "stream_endpoint": stream_endpoint,
            "final_package_endpoint": query_result_endpoint,
        },
        "reattach": {
            "available": can_reattach,
            "refresh_starts_new_run": False,
            "search_id_required": True,
            "search_id_available": bool(search_id),
            "query_result_endpoint": query_result_endpoint,
            "trace_endpoint": trace_endpoint,
            "inspect_tool": completion_inspection.get("inspect_tool"),
            "inspect_endpoint": inspect_endpoint,
            "inspect_arguments": _as_dict(completion_inspection.get("arguments")),
            "tool_boundary_inspection_available": bool(_as_dict(tool_boundary_contract.get("inspection")).get("available")),
        },
        "ui_contract": {
            "display_first_package_state_before_answer_demo": True,
            "display_background_state_as_separate_badge": True,
            "never_label_background_running_as_final": True,
            "never_rerun_on_refresh_without_user_action": True,
            "display_payload_available_separately_from_client_terminal": True,
            "blocked_requires_subreason": status == "blocked",
        },
    }


def _mcp_delivery_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    package_field: str,
    package_payload: Any,
    completion_contract: dict[str, Any],
    runtime_state_contract: dict[str, Any],
    tool_boundary_contract: dict[str, Any],
    ai_materialization_resilience_contract: dict[str, Any],
    ai_critical_path_contract: dict[str, Any],
    route_arbitration_contract: dict[str, Any],
    first_package_background_contract: dict[str, Any],
    payload_truth_contract: dict[str, Any],
    document_ref_contract: dict[str, Any],
    document_delivery_contract: dict[str, Any],
) -> dict[str, Any]:
    normalized_tool = str(tool_name or "").strip()
    effective_tool = _effective_delivery_tool_name(normalized_tool, result)
    originating_tool = _originating_mcp_tool_name(result)
    search_id = str(result.get("search_id") or "").strip()
    planner_runtime = _as_dict(result.get("planner_runtime"))
    semantic_runtime = _as_dict(result.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime"))
    semantic_contract = _as_dict(
        result.get("semantic_contract")
        or planner_runtime.get("semantic_contract")
        or _as_dict(semantic_runtime.get("semantic_contract"))
    )
    target_document_need_contract = _as_dict(
        result.get("target_document_need_contract")
        or semantic_contract.get("target_document_need_contract")
        or _as_dict(semantic_contract.get("document_contract")).get("target_document_need_contract")
    )
    target_document_need = _as_dict(
        result.get("target_document_need")
        or semantic_contract.get("target_document_need")
        or _as_dict(semantic_contract.get("document_contract")).get("target_document_need")
        or target_document_need_contract.get("target_document_need")
    )
    if (
        effective_tool in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES | {"retrieve_source_trace"}
        and not bool(target_document_need_contract.get("document_evidence"))
    ):
        target_document_need_contract = build_target_document_need_contract(
            str(result.get("query_text") or ""),
            tool_name=effective_tool,
        )
        target_document_need = _as_dict(target_document_need_contract.get("target_document_need"))
    metamemory_snapshot = _as_dict(result.get("metamemory_snapshot") or planner_runtime.get("metamemory_snapshot"))
    metamemory_spatial_brief = _as_dict(result.get("metamemory_spatial_brief") or planner_runtime.get("metamemory_spatial_brief"))
    metamemory_spatial_readiness = _as_dict(
        result.get("metamemory_spatial_readiness")
        or planner_runtime.get("metamemory_spatial_readiness")
        or metamemory_spatial_brief.get("spatial_readiness_contract")
    )
    ai_spatial_landing_contract = _as_dict(
        result.get("ai_spatial_landing_contract") or planner_runtime.get("ai_spatial_landing_contract")
    )
    ai_spatial_contract_observed = bool(
        ai_spatial_landing_contract
        and ("ai_spatial_landing_contract" in result or "ai_spatial_landing_contract" in planner_runtime)
    )
    metamemory_contract_observed = bool("metamemory_snapshot" in result or "metamemory_snapshot" in planner_runtime)
    metamemory_base_ready = bool(
        metamemory_snapshot.get("guide_exists")
        and metamemory_snapshot.get("spatial_brief_exists")
        and str(metamemory_snapshot.get("hash") or "").strip()
    )
    metamemory_readiness_observed = bool(metamemory_spatial_readiness)
    metamemory_ready = bool(
        metamemory_base_ready
        and (
            not metamemory_readiness_observed
            or bool(metamemory_spatial_readiness.get("certifiable"))
        )
    )
    metamemory_missing = bool(metamemory_contract_observed and not metamemory_ready)
    context_package = _as_dict(result.get("context_package"))
    context_contract = _as_dict(context_package.get("contract"))
    context_metrics = _as_dict(context_package.get("metrics"))
    master_judgement = _as_dict(
        result.get("master_judgement")
        or context_package.get("master_judgement")
        or planner_runtime.get("master_judgement")
    )
    master_state = str(master_judgement.get("master_state") or "").strip()
    master_next_action = str(
        master_judgement.get("next_recommended_call")
        or _as_dict(master_judgement.get("continuation_recommendation")).get("tool_action")
        or ""
    ).strip()
    path_truth_contract = _as_dict(context_contract.get("path_truth") or context_package.get("path_truth_contract"))
    runtime = _as_dict(runtime_state_contract)
    ai_resilience = _as_dict(ai_materialization_resilience_contract)
    ai_critical_path = _as_dict(ai_critical_path_contract)
    route_arbitration = _as_dict(route_arbitration_contract)
    first_bg = _as_dict(first_package_background_contract)
    first_package = _as_dict(first_bg.get("first_package"))
    background = _as_dict(first_bg.get("background_completion"))
    stream_vs_final = _as_dict(first_bg.get("stream_vs_final"))
    reattach = _as_dict(first_bg.get("reattach"))
    background_cap = _as_dict(result.get("mcp_background_cap"))
    primary_payload = _as_dict(payload_truth_contract.get("primary_mcp_payload"))
    runtime_inspection = _as_dict(_as_dict(completion_contract.get("inspection")))
    tool_inspection = _as_dict(tool_boundary_contract.get("inspection"))
    inspection = tool_inspection or runtime_inspection
    package_present = bool(
        primary_payload.get("present")
        or first_package.get("present")
        or (isinstance(package_payload, dict) and bool(package_payload))
        or (isinstance(package_payload, list) and bool(package_payload))
    )

    completion_state = str(completion_contract.get("state") or runtime.get("completion_state") or "waiting").strip()
    background_state = str(background.get("state") or "unknown").strip() or "unknown"
    stop_reason = str(result.get("stop_reason") or completion_contract.get("visible_reason") or "").strip()
    low_yield_stop = bool(
        "low_yield" in stop_reason
        or str(result.get("background_enrichment_stop_reason") or "").strip() in {
            "low_yield",
            "background_low_yield",
            "background_low_yield_converged",
            "low_yield_converged",
        }
    )
    if low_yield_stop and package_present and completion_state in {"background_running", "first_package_ready", "waiting"}:
        completion_state = "partial_complete_low_yield"

    final_materialization_pending = bool(
        completion_contract.get("final_materialization_pending")
        or background.get("pending")
        or result.get("final_materialization_pending")
    )
    if completion_state == "partial_complete_low_yield":
        final_materialization_pending = False

    ai_required = bool(ai_resilience.get("ai_required") or _as_dict(runtime.get("ai")).get("required"))
    ai_materialized = bool(
        ai_resilience.get("readiness_certifiable")
        or runtime.get("ai_state") in _AI_CERTIFYING_STATES
    )
    ai_spatial_materialized = bool(
        ai_spatial_landing_contract.get("certifiable")
        or ai_spatial_landing_contract.get("materialized")
    )
    ai_spatial_missing_reasons = {
        str(item or "").strip()
        for item in _as_list(ai_spatial_landing_contract.get("missing_reasons"))
        if str(item or "").strip()
    }
    ai_spatial_deferred = bool(
        _ai_spatial_pending_or_retryable(
            ai_spatial_landing_contract,
            observed=ai_spatial_contract_observed,
            ai_required=ai_required,
            materialized=ai_spatial_materialized,
        )
    )
    ai_spatial_missing = bool(
        ai_spatial_contract_observed
        and ai_required
        and not ai_spatial_materialized
        and not ai_spatial_deferred
    )
    original_ai_spatial_observed = ai_spatial_contract_observed
    original_ai_spatial_materialized = ai_spatial_materialized
    original_ai_spatial_deferred = ai_spatial_deferred
    original_ai_spatial_missing = ai_spatial_missing
    exact_field_no_match_terminal = bool(
        status == "no_match"
        and int(context_metrics.get("exact_field_requirement_count") or 0) > 0
        and int(context_metrics.get("exact_field_missing_count") or 0)
        >= int(context_metrics.get("exact_field_requirement_count") or 0)
        and (
            bool(context_contract.get("no_match"))
            or str(context_metrics.get("package_breadth_state") or "") == "exact_field_no_match"
        )
    )
    exact_no_match_ai_exception = bool(
        exact_field_no_match_terminal
        and semantic_runtime.get("exact_no_match_ai_exception")
        and semantic_runtime.get("material")
    )
    exact_no_match_certifiable = bool(
        exact_field_no_match_terminal
        and (bool(ai_resilience.get("readiness_certifiable")) or exact_no_match_ai_exception)
    )
    critical_path_blocked = bool(
        ai_critical_path.get("ai_required")
        and ai_critical_path.get("compact_contract_required")
        and ai_critical_path.get("contract_payload_present")
        and not bool(ai_critical_path.get("certifiable"))
    )
    route_arbitration_blocked = bool(
        route_arbitration.get("ai_required")
        and route_arbitration.get("contract_payload_present")
        and not bool(route_arbitration.get("certifiable_for_first_payload"))
        and str(route_arbitration.get("state") or "").strip() not in {"ai_plan_ready_runtime_pending"}
    )
    if exact_no_match_certifiable:
        ai_spatial_deferred = False
        ai_spatial_missing = False
        critical_path_blocked = False
        route_arbitration_blocked = False
        ai_materialized = True
    no_route_terminal_contract = {
        "schema_version": "agvm.ai_no_route_terminal_contract.v1",
        "present": exact_no_match_certifiable,
        "reason": "exact_private_or_exact_field_absence",
        "route_not_required": exact_no_match_certifiable,
        "semantic_ai_materialized": bool(ai_resilience.get("readiness_certifiable") or exact_no_match_ai_exception),
        "semantic_ai_exception": bool(exact_no_match_ai_exception),
        "exact_field_requirement_count": int(context_metrics.get("exact_field_requirement_count") or 0),
        "exact_field_missing_count": int(context_metrics.get("exact_field_missing_count") or 0),
        "no_match": bool(context_contract.get("no_match") or status == "no_match"),
    }
    if critical_path_blocked:
        ai_materialized = False
    if route_arbitration_blocked:
        ai_materialized = False
    if ai_spatial_missing:
        ai_materialized = False
    def metric_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    fast_ai_partial_material = bool(
        context_metrics.get("ai_slot_fast_package")
        and package_present
        and (
            metric_int(context_metrics.get("hot_item_count")) > 0
            or metric_int(context_metrics.get("document_ref_count")) > 0
            or bool(_as_list(context_package.get("hot_sections")))
            or bool(str(context_package.get("agent_markdown") or "").strip())
        )
    )
    materialization_source = str(ai_resilience.get("materialization_source") or "").strip()
    if ai_materialized and materialization_source in {"fresh_llm", "cached_llm", "route_materialized"}:
        ai_state = materialization_source
    elif ai_materialized:
        ai_state = "materialized"
    else:
        ai_state = str(runtime.get("ai_state") or ai_resilience.get("operator_label") or "pending").strip()
    provider_state = str(runtime.get("provider_state") or _as_dict(ai_resilience.get("provider")).get("state") or "unknown").strip()
    document_state = str(runtime.get("document_state") or "not_requested").strip()
    path_state = str(runtime.get("path_state") or "not_requested").strip()
    ai_missing = bool(ai_required and not ai_materialized and not fast_ai_partial_material)
    if ai_spatial_missing:
        fast_ai_partial_material = False
        ai_missing = True
    context_passed = bool(context_contract.get("passed"))
    context_unresolved_sections = [
        str(item or "").strip()
        for item in _as_list(context_contract.get("unresolved_sections"))
        if str(item or "").strip()
    ]
    context_soft_unresolved_sections_allowed = bool(
        context_unresolved_sections
        and _selected_context_package_soft_unresolved_sections_allowed(
            {
                **result,
                "document_ref_contract": document_ref_contract,
                "document_delivery_contract": document_delivery_contract,
            },
            context_package=context_package,
        )
    )
    context_effective_passed = bool(context_passed or context_soft_unresolved_sections_allowed)
    context_path_truth_required = bool(
        path_truth_contract.get("required")
        or context_metrics.get("path_truth_required")
    )
    context_path_truth_ready = bool(
        path_truth_contract.get("ready")
        or context_metrics.get("path_truth_ready")
    )
    context_path_truth_pending = bool(context_path_truth_required and not context_path_truth_ready)
    if context_path_truth_required and context_path_truth_ready and path_state in {"planned", "traversing", "partial"}:
        path_state = "completed"

    is_exact_document_tool = effective_tool == "retrieve_document"
    is_document_workspace_tool = effective_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES
    is_document_tool = effective_tool in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES
    is_path_tool = effective_tool in {"retrieve_path_corridor", "inspect_path_corridor"}
    document_workspace_refs_terminality = _document_workspace_refs_terminality_contract(
        result,
        tool_name=effective_tool,
        target_document_need_contract=target_document_need_contract,
        master_judgement=master_judgement,
        document_ref_contract=document_ref_contract,
        document_delivery_contract=document_delivery_contract,
    )
    document_workspace_refs_ready = bool(document_workspace_refs_terminality.get("refs_ready"))
    document_workspace_refs_terminal = bool(document_workspace_refs_terminality.get("terminal_for_client"))
    if document_workspace_refs_terminal:
        ai_missing = False
        ai_state = "document_evidence_certified"
        ai_spatial_deferred = False
        ai_spatial_missing = False
        metamemory_missing = False
        critical_path_blocked = False
        route_arbitration_blocked = False
    answerability_required_slot_count = metric_int(context_metrics.get("answerability_required_slot_count"))
    answerability_covered_slot_count = metric_int(context_metrics.get("answerability_covered_slot_count"))
    answerability_missing_slot_count = metric_int(context_metrics.get("answerability_missing_slot_count"))
    semantic_required_slot_keys = {
        str(item or "").strip()
        for item in _as_list(
            context_metrics.get("semantic_required_slot_keys")
            or context_contract.get("semantic_required_slot_keys")
        )
        if str(item or "").strip()
    }
    def canonical_answerability_slot(slot: Any) -> str:
        normalized = str(slot or "").strip().lower().replace(" ", "_")
        if not normalized:
            return ""
        base = normalized.split(":", 1)[0].split("/", 1)[0]
        if base and base != normalized:
            base_value = canonical_answerability_slot(base)
            if base_value:
                return base_value
        if normalized in {"work", "projects", "work_detail", "work_company", "company_founding"} or "company" in normalized:
            return "work"
        if normalized in {"relationships", "relationship", "family", "relation_detail"} or normalized.startswith("family"):
            return "relationships"
        if normalized in {"history", "temporal", "temporal_inventory", "timeline"}:
            return "history"
        if normalized in {"document", "documents", "source", "sources", "raw"}:
            return "documents"
        if normalized in {"identity", "name", "profile"}:
            return "identity"
        if normalized in {"privacy", "privacy_boundary", "private_boundary"}:
            return "privacy_boundary"
        return normalized

    answerability_required_slot_keys = {
        canonical_answerability_slot(item)
        for item in _as_list(
            context_metrics.get("answerability_required_slots")
            or context_contract.get("answerability_required_slots")
        )
        if canonical_answerability_slot(item)
    }
    effective_required_slot_keys = answerability_required_slot_keys or {
        canonical_answerability_slot(item)
        for item in semantic_required_slot_keys
        if canonical_answerability_slot(item)
    }
    folded_query_text = re.sub(r"\s+", " ", str(result.get("query_text") or "").casefold()).strip()
    folded_agent_markdown = re.sub(
        r"\s+",
        " ",
        str(context_package.get("agent_markdown") or "").casefold(),
    ).strip()
    actionable_document_ref_count = metric_int(
        document_ref_contract.get("actionable_document_ref_count")
        or document_delivery_contract.get("actionable_document_ref_count")
        or context_metrics.get("document_ref_count")
        or len(_as_list(context_package.get("document_refs")))
    )
    timeline_or_event_query = bool(
        any(
            marker in folded_query_text
            for marker in (
                "timeline",
                "cronologia",
                "eventi",
                "evento",
                "event ",
                "events",
            )
        )
    )
    relation_chain_query = bool(
        any(marker in folded_query_text for marker in ("relazione", "relazioni", "relationship", "relationships", "mappa", "map", "spiega", "explain"))
        and any(marker in folded_query_text for marker in (" tra ", " fra ", " between ", ","))
    )
    positive_exact_source_surface_ready = bool(
        actionable_document_ref_count > 0
        or not timeline_or_event_query
        or "identity" in effective_required_slot_keys
    )
    positive_exact_relation_scope_ready = bool(
        not relation_chain_query
        or "identity" in effective_required_slot_keys
        or actionable_document_ref_count > 0
    )
    positive_exact_allowed_slots = {"identity", "temporal", "work", "work_company", "company", "companies"}
    positive_exact_slot_scope = bool(
        effective_required_slot_keys
        and effective_required_slot_keys.issubset({canonical_answerability_slot(item) for item in positive_exact_allowed_slots})
        and answerability_required_slot_count > 0
        and answerability_required_slot_count <= 3
        and answerability_covered_slot_count >= answerability_required_slot_count
        and answerability_missing_slot_count == 0
        and positive_exact_source_surface_ready
        and positive_exact_relation_scope_ready
    )
    positive_exact_sufficiency = bool(
        normalized_tool in {"retrieve_context", "inspect_context_package"}
        and not is_document_tool
        and not is_path_tool
        and status not in {"blocked", "failed", "no_match"}
        and context_effective_passed
        and package_present
        and positive_exact_slot_scope
        and bool(ai_resilience.get("readiness_certifiable"))
        and bool(route_arbitration.get("certifiable_for_first_payload"))
        and not route_arbitration_blocked
        and not context_path_truth_pending
        and not metamemory_missing
        and not ai_spatial_missing
    )
    positive_exact_terminal_certification_allowed = bool(
        positive_exact_sufficiency
        and original_ai_spatial_materialized
        and not original_ai_spatial_deferred
        and not original_ai_spatial_missing
    )
    positive_exact_sufficiency_contract = {
        "schema_version": "agvm.positive_exact_sufficiency_contract.v1",
        "present": positive_exact_sufficiency,
        "reason": "ai_master_exact_positive_content_sufficient" if positive_exact_sufficiency else None,
        "semantic_ai_materialized": bool(ai_resilience.get("readiness_certifiable")),
        "context_contract_passed": context_effective_passed,
        "answerability_required_slot_count": answerability_required_slot_count,
        "answerability_covered_slot_count": answerability_covered_slot_count,
        "answerability_missing_slot_count": answerability_missing_slot_count,
        "semantic_required_slot_keys": sorted(semantic_required_slot_keys),
        "route_arbitration_certifiable": bool(route_arbitration.get("certifiable_for_first_payload")),
        "spatial_route_deferred_not_blocking_first_payload": bool(ai_spatial_deferred),
        "spatial_route_deferred_blocks_terminal_certification": bool(
            positive_exact_sufficiency and original_ai_spatial_deferred
        ),
        "terminal_certification_allowed": positive_exact_terminal_certification_allowed,
        "content_sufficiency_only": bool(
            positive_exact_sufficiency and not positive_exact_terminal_certification_allowed
        ),
        "terminal_blocked_by": (
            "ai_spatial_pending"
            if positive_exact_sufficiency and original_ai_spatial_deferred
            else "ai_spatial_missing"
            if positive_exact_sufficiency and original_ai_spatial_missing
            else "ai_spatial_unmaterialized"
            if positive_exact_sufficiency and not original_ai_spatial_materialized
            else None
        ),
        "path_truth_required": context_path_truth_required,
    }
    public_fact_query = bool(
        any(
            marker in folded_query_text
            for marker in (
                "fatti personali pubblici",
                "fatto personale pubblico",
                "fatti pubblici",
                "public personal facts",
                "personal public facts",
                "public facts",
                "relazione familiare",
                "family relation",
                "evento pubblico",
                "public event",
            )
        )
    )
    public_fact_payload_anchor = bool(
        any(
            marker in folded_agent_markdown
            for marker in (
                "monumento",
                "monument",
                "padre",
                "father",
                "famiglia",
                "family",
                "inaugur",
                "dedicat",
                "dedicated",
            )
        )
    )
    public_fact_allowed_slots = {"identity", "relationships", "history", "privacy_boundary", "documents"}
    public_fact_slot_scope = bool(
        public_fact_query
        and effective_required_slot_keys
        and effective_required_slot_keys.issubset(public_fact_allowed_slots)
        and bool(effective_required_slot_keys & {"relationships", "history"})
        and answerability_required_slot_count > 0
        and answerability_required_slot_count <= 4
        and answerability_covered_slot_count >= answerability_required_slot_count
        and answerability_missing_slot_count == 0
    )
    public_fact_sufficiency = bool(
        normalized_tool in {"retrieve_context", "inspect_context_package"}
        and not is_document_tool
        and not is_path_tool
        and status not in {"blocked", "failed", "no_match"}
        and context_effective_passed
        and package_present
        and public_fact_slot_scope
        and public_fact_payload_anchor
        and actionable_document_ref_count > 0
        and bool(ai_resilience.get("readiness_certifiable"))
        and bool(route_arbitration.get("certifiable_for_first_payload"))
        and not route_arbitration_blocked
        and not context_path_truth_pending
        and not metamemory_missing
        and not ai_spatial_missing
    )
    public_fact_terminal_certification_allowed = bool(
        public_fact_sufficiency
        and original_ai_spatial_materialized
        and not original_ai_spatial_deferred
        and not original_ai_spatial_missing
    )
    public_fact_sufficiency_contract = {
        "schema_version": "agvm.public_fact_sufficiency_contract.v1",
        "present": public_fact_sufficiency,
        "reason": "public_fact_context_content_sufficient" if public_fact_sufficiency else None,
        "semantic_ai_materialized": bool(ai_resilience.get("readiness_certifiable")),
        "context_contract_passed": context_effective_passed,
        "answerability_required_slot_count": answerability_required_slot_count,
        "answerability_covered_slot_count": answerability_covered_slot_count,
        "answerability_missing_slot_count": answerability_missing_slot_count,
        "effective_required_slot_keys": sorted(effective_required_slot_keys),
        "actionable_document_ref_count": actionable_document_ref_count,
        "payload_public_fact_anchor": public_fact_payload_anchor,
        "route_arbitration_certifiable": bool(route_arbitration.get("certifiable_for_first_payload")),
        "spatial_route_deferred_not_blocking_first_payload": bool(ai_spatial_deferred),
        "spatial_route_deferred_blocks_terminal_certification": bool(
            public_fact_sufficiency and original_ai_spatial_deferred
        ),
        "terminal_certification_allowed": public_fact_terminal_certification_allowed,
        "content_sufficiency_only": bool(public_fact_sufficiency and not public_fact_terminal_certification_allowed),
        "terminal_blocked_by": (
            "ai_spatial_pending"
            if public_fact_sufficiency and original_ai_spatial_deferred
            else "ai_spatial_missing"
            if public_fact_sufficiency and original_ai_spatial_missing
            else "ai_spatial_unmaterialized"
            if public_fact_sufficiency and not original_ai_spatial_materialized
            else None
        ),
        "path_truth_required": context_path_truth_required,
    }
    cap_reason = str(
        background_cap.get("reason")
        or background_cap.get("background_cap_reason")
        or _as_dict(result.get("mcp_runtime_boundary")).get("background_cap_reason")
        or _as_dict(planner_runtime.get("mcp_runtime_boundary")).get("background_cap_reason")
        or result.get("background_enrichment_stop_reason")
        or stop_reason
        or completion_contract.get("visible_reason")
        or ""
    ).strip()
    verifiable_followup_document_cap = bool(
        cap_reason == "mcp_verifiable_followup_document_backed_context_satisfied_spatial_deferred_cap"
        or str(context_package.get("package_mode") or "").strip() == "verifiable_followup_document_first_package"
        or bool(context_metrics.get("ai_slot_fast_verifiable_followup_document_support_requested"))
        or bool(context_metrics.get("verifiable_followup_document_first_package"))
    )
    verifiable_followup_document_sufficiency_scope = bool(
        verifiable_followup_document_cap
        and "documents" in effective_required_slot_keys
        and actionable_document_ref_count > 0
        and answerability_required_slot_count > 0
        and answerability_covered_slot_count >= answerability_required_slot_count
        and answerability_missing_slot_count == 0
    )
    answerability_direct_allowed_slots = {"values", "style"}
    answerability_direct_slot_scope = bool(
        effective_required_slot_keys
        and effective_required_slot_keys.issubset(answerability_direct_allowed_slots)
        and bool(effective_required_slot_keys & answerability_direct_allowed_slots)
        and answerability_required_slot_count > 0
        and answerability_required_slot_count <= 3
        and answerability_covered_slot_count >= answerability_required_slot_count
        and answerability_missing_slot_count == 0
    )
    answerability_route_arbitration_certifiable = bool(
        route_arbitration.get("certifiable_for_first_payload") or not context_path_truth_required
    )
    answerability_sufficiency = bool(
        normalized_tool in {"retrieve_context", "inspect_context_package"}
        and not is_document_tool
        and not is_path_tool
        and status not in {"blocked", "failed", "no_match"}
        and context_effective_passed
        and package_present
        and (answerability_direct_slot_scope or verifiable_followup_document_sufficiency_scope)
        and bool(ai_resilience.get("readiness_certifiable"))
        and answerability_route_arbitration_certifiable
        and (not route_arbitration_blocked or not context_path_truth_required)
        and not context_path_truth_pending
        and not metamemory_missing
        and (not ai_spatial_missing or verifiable_followup_document_sufficiency_scope)
    )
    answerability_terminal_certification_allowed = bool(
        answerability_sufficiency
        and original_ai_spatial_materialized
        and not original_ai_spatial_deferred
        and not original_ai_spatial_missing
    )
    answerability_sufficiency_contract = {
        "schema_version": "agvm.answerability_sufficiency_contract.v1",
        "present": answerability_sufficiency,
        "reason": (
            "verifiable_followup_document_context_content_sufficient"
            if answerability_sufficiency and verifiable_followup_document_sufficiency_scope
            else "slot_answerability_context_content_sufficient"
            if answerability_sufficiency
            else None
        ),
        "semantic_ai_materialized": bool(ai_resilience.get("readiness_certifiable")),
        "context_contract_passed": context_effective_passed,
        "answerability_required_slot_count": answerability_required_slot_count,
        "answerability_covered_slot_count": answerability_covered_slot_count,
        "answerability_missing_slot_count": answerability_missing_slot_count,
        "effective_required_slot_keys": sorted(effective_required_slot_keys),
        "actionable_document_ref_count": actionable_document_ref_count,
        "verifiable_followup_document_cap": verifiable_followup_document_cap,
        "route_arbitration_certifiable": answerability_route_arbitration_certifiable,
        "spatial_route_deferred_not_blocking_first_payload": bool(ai_spatial_deferred),
        "spatial_route_deferred_blocks_terminal_certification": bool(
            answerability_sufficiency and original_ai_spatial_deferred
        ),
        "terminal_certification_allowed": answerability_terminal_certification_allowed,
        "content_sufficiency_only": bool(answerability_sufficiency and not answerability_terminal_certification_allowed),
        "terminal_blocked_by": (
            "ai_spatial_pending"
            if answerability_sufficiency and original_ai_spatial_deferred
            else "ai_spatial_missing"
            if answerability_sufficiency and original_ai_spatial_missing
            else "ai_spatial_unmaterialized"
            if answerability_sufficiency and not original_ai_spatial_materialized
            else None
        ),
        "path_truth_required": context_path_truth_required,
    }
    path_corridors = _as_dict(result.get("path_corridors"))
    path_metrics = _as_dict(path_corridors.get("metrics"))
    path_storage_contract = _as_dict(result.get("path_tool_storage_contract"))
    path_mission_surface_missing = bool(
        is_path_tool
        and (
            path_storage_contract.get("mission_surface_missing")
            or path_metrics.get("mission_surface_missing")
        )
    )
    route_trace = _as_dict(result.get("route_trace"))
    route_events = _as_list(route_trace.get("branch_route_events"))
    embedded_path_route_event_count = sum(
        len(_as_list(path.get("route_events")))
        for path in _as_list(path_corridors.get("paths") or path_corridors.get("corridors"))
        if isinstance(path, dict)
    )
    visible_route_event_count = max(embedded_path_route_event_count, len(route_events))
    try:
        path_count = int(
            path_metrics.get("path_count")
            or path_metrics.get("planned_corridor_count")
            or len(_as_list(path_corridors.get("paths")))
            or 0
        )
    except (TypeError, ValueError):
        path_count = 0
    try:
        metric_route_event_count = int(path_metrics.get("route_event_count") or 0)
    except (TypeError, ValueError):
        metric_route_event_count = 0
    if (
        is_path_tool
        and str(path_metrics.get("route_event_count_source") or "") == "route_trace"
        and visible_route_event_count <= 0
    ):
        route_event_count = 0
    else:
        try:
            route_event_count = int(
                metric_route_event_count
                or embedded_path_route_event_count
                or path_metrics.get("traversed_count")
                or path_metrics.get("route_step_count")
                or len(route_events)
                or 0
            )
        except (TypeError, ValueError):
            route_event_count = 0
    try:
        pending_path_count = int(path_metrics.get("pending_path_count") or 0)
    except (TypeError, ValueError):
        pending_path_count = 0
    try:
        terminal_path_count = int(
            path_metrics.get("terminal_path_count")
            or path_metrics.get("completed_path_count")
            or path_metrics.get("completed_corridor_count")
            or 0
        )
    except (TypeError, ValueError):
        terminal_path_count = 0
    path_payload_has_traversal = bool(
        is_path_tool
        and package_present
        and path_count > 0
        and route_event_count > 0
    )
    path_route_first_sufficiency_contract = (
        _path_tool_route_first_sufficiency_contract(result)
        if is_path_tool
        else {"schema_version": "agvm.path_route_first_sufficiency_contract.v1", "present": False}
    )
    path_route_first_sufficiency = bool(path_route_first_sufficiency_contract.get("present"))
    if path_route_first_sufficiency:
        ai_spatial_deferred = False
        ai_spatial_missing = False
        critical_path_blocked = False
        route_arbitration_blocked = False
        ai_materialized = True
        ai_missing = False
        ai_state = "route_first_sufficient"
    path_payload_has_completed_route_truth = bool(
        path_payload_has_traversal
        and path_route_first_sufficiency
        and not path_mission_surface_missing
        and (
            path_state == "completed"
            or terminal_path_count > 0
            or pending_path_count == 0
        )
    )
    path_payload_has_useful_route_truth = bool(
        path_payload_has_traversal
        and path_route_first_sufficiency
        and not path_mission_surface_missing
        and not ai_missing
        and not metamemory_missing
        and not ai_spatial_missing
        and not ai_spatial_deferred
    )
    if is_path_tool and (path_payload_has_completed_route_truth or path_payload_has_useful_route_truth):
        context_path_truth_required = True
        context_path_truth_ready = True
        context_path_truth_pending = False
        if path_state in {"not_requested", "planned", "traversing", "partial"}:
            path_state = "completed"
        path_truth_contract = {
            **path_truth_contract,
            "required": True,
            "ready": True,
            "state": str(path_truth_contract.get("state") or "route_truth_ready"),
            "planned_path_count": path_truth_contract.get("planned_path_count") or path_count,
            "path_count": path_truth_contract.get("path_count") or path_count,
            "route_event_count": path_truth_contract.get("route_event_count") or route_event_count,
            "pending_reasons": [],
            "missing_reasons": [],
            "source": str(path_truth_contract.get("source") or "path_corridors_visible_route_truth"),
        }
    if status == "failed":
        client_payload_state = "failed"
    elif status == "blocked":
        client_payload_state = "blocked"
    elif status == "no_match":
        client_payload_state = "no_match"
    elif not package_present:
        client_payload_state = "waiting"
    elif document_workspace_refs_terminal:
        client_payload_state = "document_refs_ready"
    elif is_document_workspace_tool and document_workspace_refs_ready:
        client_payload_state = "partial_document_refs"
    elif is_document_workspace_tool and package_present:
        client_payload_state = "partial_document_refs"
    elif is_exact_document_tool and document_state in {"exact_ready", "raw_ready", "refs_ready"}:
        client_payload_state = "document_payload_ready"
    elif is_path_tool and path_payload_has_useful_route_truth:
        client_payload_state = "path_payload_ready"
    elif is_path_tool and (path_count > 0 or path_state in {"partial", "planned", "traversing", "completed"}):
        client_payload_state = "partial_path_payload"
    elif (
        not is_document_tool
        and not is_path_tool
        and context_effective_passed
        and not context_path_truth_pending
        and not ai_missing
        and not metamemory_missing
        and not ai_spatial_missing
        and not ai_spatial_deferred
    ):
        client_payload_state = "usable_context"
    elif package_present and context_path_truth_pending:
        client_payload_state = "partial_context"
    elif package_present and ai_spatial_deferred:
        client_payload_state = "partial_context"
    elif package_present and not ai_missing and not metamemory_missing and not ai_spatial_missing and not ai_spatial_deferred:
        client_payload_state = "partial_context"
    elif package_present and ai_missing and ai_state == "ai_pending":
        client_payload_state = "partial_context"
    elif package_present and metamemory_missing:
        client_payload_state = "partial_context"
    else:
        client_payload_state = "blocked" if ai_missing else "partial_context"

    master_override_blocked_by_snapshot_no_match = bool(
        master_state == "no_match"
        and context_passed
        and package_present
        and (
            final_materialization_pending
            or completion_state in {"background_running", "first_package_ready", "waiting"}
            or (
                not ai_missing
                and _mission_ledger_is_legacy_empty_route_snapshot(
                    _as_dict(result.get("mission_evidence_ledger") or planner_runtime.get("mission_evidence_ledger"))
                )
            )
        )
    )
    master_override_applied = False
    if (
        normalized_tool in {"retrieve_context", "inspect_context_package"}
        and not is_document_tool
        and master_state
        and status not in {"failed", "blocked", "no_match"}
        and not master_override_blocked_by_snapshot_no_match
    ):
        if master_state == "terminal":
            client_payload_state = "usable_context"
            master_override_applied = True
        elif master_state == "no_match":
            client_payload_state = "no_match"
            master_override_applied = True
        elif master_state in {"usable_partial", "needs_hydration", "needs_more_search"}:
            client_payload_state = "partial_context"
            master_override_applied = True
        elif master_state == "provider_degraded" and client_payload_state == "usable_context":
            client_payload_state = "partial_context"
            master_override_applied = True
        elif master_state == "blocked":
            client_payload_state = "blocked"
            master_override_applied = True
    if positive_exact_sufficiency and client_payload_state != "usable_context":
        client_payload_state = "usable_context"
        master_override_applied = True
    if public_fact_sufficiency and client_payload_state != "usable_context":
        client_payload_state = "usable_context"
        master_override_applied = True
    if answerability_sufficiency and client_payload_state != "usable_context":
        client_payload_state = "usable_context"
        master_override_applied = True
    if path_route_first_sufficiency and client_payload_state != "path_payload_ready":
        client_payload_state = "path_payload_ready"
        master_override_applied = True
    if document_workspace_refs_terminal and client_payload_state != "document_refs_ready":
        client_payload_state = "document_refs_ready"
        master_override_applied = True

    terminal_ai_spatial_content_sufficiency_present = bool(
        positive_exact_sufficiency
        or public_fact_sufficiency
        or answerability_sufficiency
    )
    terminal_ai_spatial_ready = bool(
        original_ai_spatial_materialized
        and not original_ai_spatial_deferred
        and not original_ai_spatial_missing
    )
    terminal_ai_spatial_supervisor_before_state = client_payload_state
    terminal_ai_spatial_supervisor_applies = bool(
        normalized_tool in {"retrieve_context", "inspect_context_package"}
        and not is_document_tool
        and not is_path_tool
        and not document_workspace_refs_terminal
        and ai_required
        and package_present
        and status not in {"blocked", "failed", "no_match"}
        and not exact_no_match_certifiable
        and (
            terminal_ai_spatial_content_sufficiency_present
            or client_payload_state == "usable_context"
            or master_state == "terminal"
        )
        and (
            original_ai_spatial_observed
            or terminal_ai_spatial_content_sufficiency_present
            or master_state == "terminal"
        )
    )
    terminal_ai_spatial_supervisor_waiting = bool(
        terminal_ai_spatial_supervisor_applies and not terminal_ai_spatial_ready
    )
    terminal_ai_spatial_supervisor_state = (
        "not_applicable"
        if not terminal_ai_spatial_supervisor_applies
        else "terminal_certified"
        if terminal_ai_spatial_ready
        else "waiting_ai_spatial"
        if original_ai_spatial_deferred
        else "missing_ai_spatial"
        if original_ai_spatial_missing
        else "unmaterialized_ai_spatial"
    )
    if terminal_ai_spatial_supervisor_waiting and client_payload_state == "usable_context":
        client_payload_state = "partial_context"
    if (
        package_present
        and status not in {"blocked", "failed", "no_match"}
        and original_ai_spatial_deferred
        and not document_workspace_refs_terminal
        and (
            normalized_tool in {"retrieve_context", "inspect_context_package"}
            or (is_path_tool and not path_route_first_sufficiency)
        )
    ):
        final_materialization_pending = True

    selected_payload_client_sealed = bool(
        normalized_tool in {"retrieve_context", "inspect_context_package"}
        and not is_document_tool
        and master_state == "terminal"
        and str(master_judgement.get("source") or "").strip() == "selected_context_package_contract"
        and context_effective_passed
        and package_present
        and not ai_missing
        and not metamemory_missing
        and not ai_spatial_missing
        and not ai_spatial_deferred
        and not context_path_truth_pending
        and status not in {"blocked", "failed", "no_match"}
    )
    if selected_payload_client_sealed:
        final_materialization_pending = False
    positive_exact_client_sealed = bool(
        positive_exact_terminal_certification_allowed
        and client_payload_state == "usable_context"
    )
    if positive_exact_client_sealed:
        final_materialization_pending = False
    public_fact_client_sealed = bool(
        public_fact_terminal_certification_allowed
        and client_payload_state == "usable_context"
    )
    if public_fact_client_sealed:
        final_materialization_pending = False
    answerability_client_sealed = bool(
        answerability_terminal_certification_allowed
        and client_payload_state == "usable_context"
    )
    if answerability_client_sealed:
        final_materialization_pending = False
    document_refs_client_sealed = bool(
        document_workspace_refs_terminal
        and client_payload_state == "document_refs_ready"
    )
    if document_refs_client_sealed:
        final_materialization_pending = False

    missing_reasons: list[Any] = []
    pending_reasons: list[Any] = []
    if status == "blocked":
        missing_reasons.append(stop_reason or "runtime_contract_blocked")
    if ai_missing and ai_state == "ai_pending":
        pending_reasons.append("ai_material_pending")
    elif ai_missing:
        missing_reasons.append("ai_material_missing")
    if provider_state in {"provider_degraded", "timeout"} and ai_missing:
        missing_reasons.append("provider_degraded_without_ai_material")
    if metamemory_missing:
        missing_reasons.append(
            "metamemory_spatial_brief_incomplete"
            if metamemory_readiness_observed and metamemory_base_ready
            else "metamemory_spatial_brief_missing"
        )
        missing_reasons.extend(
            str(item)
            for item in _as_list(metamemory_spatial_readiness.get("missing_reasons"))
            if str(item or "").strip()
        )
        missing_reasons.extend(
            str(item)
            for item in _as_list(metamemory_spatial_readiness.get("stale_reasons"))
            if str(item or "").strip()
        )
    if ai_spatial_missing:
        missing_reasons.append("blocked_no_ai_spatial_material")
        missing_reasons.extend(
            str(item)
            for item in _as_list(ai_spatial_landing_contract.get("missing_reasons"))
            if str(item or "").strip()
        )
    elif ai_spatial_deferred:
        pending_reasons.append("ai_spatial_material_pending")
        pending_reasons.extend(
            str(item)
            for item in _as_list(ai_spatial_landing_contract.get("missing_reasons"))
            if str(item or "").strip()
        )
    if terminal_ai_spatial_supervisor_waiting:
        pending_reasons.append("terminal_supervisor_waiting_ai_spatial")
    if critical_path_blocked:
        missing_reasons.append("compact_ai_contract_incomplete")
        missing_reasons.extend(str(item) for item in _as_list(ai_critical_path.get("blockers")) if str(item).strip())
    if route_arbitration_blocked:
        missing_reasons.append("route_arbitration_not_certifiable")
        missing_reasons.extend(str(item) for item in _as_list(route_arbitration.get("blockers")) if str(item).strip())
    if not package_present:
        missing_reasons.append("primary_mcp_payload_missing")
    if context_path_truth_pending:
        pending_reasons.extend(
            str(item)
            for item in _as_list(path_truth_contract.get("pending_reasons"))
            if str(item or "").strip()
        )
        if not any(str(item).startswith("path_truth") for item in pending_reasons):
            pending_reasons.append("path_truth_pending")
        if str(path_truth_contract.get("follow_up_tool") or "") == "inspect_path_corridor":
            pending_reasons.append("inspect_path_corridor_available")
        missing_reasons.extend(
            str(item)
            for item in _as_list(path_truth_contract.get("missing_reasons"))
            if str(item or "").strip()
        )
    unresolved_sections = list(context_unresolved_sections)
    if bool(document_workspace_refs_terminality.get("pure_document_evidence")) and (
        document_workspace_refs_ready or document_workspace_refs_terminal
    ):
        unresolved_sections = [
            item
            for item in unresolved_sections
            if any(marker in str(item or "").casefold() for marker in ("document", "source", "raw"))
        ]
    if is_path_tool and path_payload_has_useful_route_truth:
        unresolved_sections = [
            item
            for item in unresolved_sections
            if str(item or "").strip() != "path_truth"
        ]
    if unresolved_sections and is_path_tool and path_payload_has_useful_route_truth:
        pending_reasons.append("secondary_context_unresolved:" + ",".join(str(item) for item in unresolved_sections[:8]))
    elif unresolved_sections and context_soft_unresolved_sections_allowed:
        pass
    elif unresolved_sections:
        missing_reasons.append("unresolved_context_sections:" + ",".join(str(item) for item in unresolved_sections[:8]))
    semantic_missing_slot_keys = [
        str(item or "").strip()
        for item in _as_list(
            context_contract.get("semantic_missing_slot_keys")
            or context_contract.get("missing_semantic_slots")
        )
        if str(item or "").strip()
    ]
    if bool(document_workspace_refs_terminality.get("pure_document_evidence")) and (
        document_workspace_refs_ready or document_workspace_refs_terminal
    ):
        semantic_missing_slot_keys = [
            item
            for item in semantic_missing_slot_keys
            if any(marker in str(item or "").casefold() for marker in ("document", "source", "raw"))
        ]
    if semantic_missing_slot_keys:
        missing_reasons.append("semantic_missing_slots:" + ",".join(semantic_missing_slot_keys[:8]))
    blockers = _as_list(ai_resilience.get("blockers")) + _as_list(runtime.get("blocking_axes"))
    if document_workspace_refs_terminal:
        document_refs_suppressed_blockers = {
            "ai_material_missing",
            "ai_material_not_certifiable",
            "blocked_ai_material_missing",
            "compact_ai_contract_incomplete",
            "compact_ai_contract_not_certifiable",
            "route_arbitration_not_certifiable",
            "ai_spatial_landing_contract_missing",
            "blocked_no_ai_spatial_material",
            "background_ai_materialization_pending",
        }
        blockers = [
            blocker
            for blocker in blockers
            if str(blocker or "").strip() not in document_refs_suppressed_blockers
        ]
    if path_route_first_sufficiency:
        route_first_suppressed_blockers = {
            "ai_material_missing",
            "ai_material_not_certifiable",
            "blocked_ai_material_missing",
            "compact_ai_contract_incomplete",
            "compact_ai_contract_not_certifiable",
            "route_arbitration_not_certifiable",
            "ai_spatial_landing_contract_missing",
            "blocked_no_ai_spatial_material",
        }
        blockers = [
            blocker
            for blocker in blockers
            if str(blocker or "").strip() not in route_first_suppressed_blockers
        ]
    if ai_state == "ai_pending":
        pending_blockers = {"ai_material_missing", "ai_material_not_certifiable"}
        missing_reasons.extend(
            str(item)
            for item in blockers
            if str(item or "").strip() and str(item or "").strip() not in pending_blockers
        )
    else:
        missing_reasons.extend(str(item) for item in blockers if str(item or "").strip())
    if final_materialization_pending and not document_workspace_refs_terminal:
        pending_reasons.append("final_materialization_pending")
    if (
        master_state in {"usable_partial", "needs_hydration", "needs_more_search"}
        and not positive_exact_terminal_certification_allowed
        and not public_fact_terminal_certification_allowed
        and not answerability_terminal_certification_allowed
        and not path_route_first_sufficiency
        and not document_workspace_refs_terminal
    ):
        pending_reasons.append(f"master_state:{master_state}")
    background_cap_still_pending = bool(
        bool(background_cap.get("requested"))
        and (
            final_materialization_pending
            or completion_state not in {"finalized", "blocked", "failed", "no_match", "partial_complete_low_yield"}
        )
    )
    if (
        background_cap_still_pending
        and not positive_exact_terminal_certification_allowed
        and not public_fact_terminal_certification_allowed
        and not answerability_terminal_certification_allowed
        and not path_route_first_sufficiency
        and not document_workspace_refs_terminal
    ):
        pending_reasons.append("background_cap_requested")
    raw_document_follow_up_available = bool(document_delivery_contract.get("raw_text_follow_up_required"))
    if raw_document_follow_up_available and not document_workspace_refs_terminal:
        pending_reasons.append("raw_document_follow_up_available")
    if is_path_tool and path_count > 0 and not path_payload_has_completed_route_truth:
        pending_reasons.append(
            "path_corridor_background_completion_available"
            if path_payload_has_useful_route_truth
            else "path_corridor_not_fully_completed"
        )
        if route_event_count <= 0:
            pending_reasons.append("path_route_truth_missing")
        if path_mission_surface_missing:
            pending_reasons.append("path_mission_surface_missing")

    exact_document_payload_terminal = bool(
        is_exact_document_tool
        and client_payload_state == "document_payload_ready"
        and document_state in {"exact_ready", "raw_ready", "refs_ready"}
        and status not in {"blocked", "failed"}
    )
    if exact_document_payload_terminal and ai_missing:
        pending_reasons.append("ai_context_certification_deferred_after_exact_document_payload")
    path_payload_terminal = bool(
        is_path_tool
        and client_payload_state == "path_payload_ready"
        and path_payload_has_useful_route_truth
        and not ai_missing
        and not metamemory_missing
        and not ai_spatial_missing
        and not ai_spatial_deferred
        and status not in {"blocked", "failed"}
    )
    terminal_for_client = bool(
        exact_document_payload_terminal
        or document_workspace_refs_terminal
        or path_payload_terminal
        or (
            not is_document_tool
            and client_payload_state
            in {
                "usable_context",
                "no_match",
                "document_refs_ready",
            }
            and not ai_missing
            and not metamemory_missing
            and not ai_spatial_missing
            and not ai_spatial_deferred
            and not context_path_truth_pending
            and status not in {"blocked", "failed"}
        )
    )
    if (
        completion_state == "partial_complete_low_yield"
        and package_present
        and not ai_missing
        and not metamemory_missing
        and not ai_spatial_missing
        and not ai_spatial_deferred
        and not context_path_truth_pending
    ):
        terminal_for_client = True
    run_finished = bool(
        completion_state
        in {
            "finalized",
            "blocked",
            "failed",
            "no_match",
            "partial_complete_low_yield",
        }
        and not final_materialization_pending
    )
    terminal_for_inspection = bool(run_finished and package_present)

    inspect_endpoint = (
        inspection.get("inspect_endpoint")
        or reattach.get("inspect_endpoint")
        or _as_dict(completion_contract.get("inspection")).get("inspect_endpoint")
    )
    stream_endpoint = stream_vs_final.get("stream_endpoint") or (f"/memory/query-stream/{search_id}" if search_id else None)
    query_result_endpoint = reattach.get("query_result_endpoint") or (f"/memory/query-result/{search_id}" if search_id else None)
    document_follow_up_needed = bool(document_delivery_contract.get("raw_text_follow_up_required"))
    master_follow_up_tool = ""
    if master_next_action:
        if master_next_action in {
            "retrieve_context",
            "retrieve_document",
            "retrieve_document_workspace",
            "retrieve_project_workspace",
            "retrieve_path_corridor",
            "retrieve_source_trace",
            "inspect_context_package",
            "inspect_path_corridor",
        }:
            master_follow_up_tool = master_next_action
        elif master_next_action in {
            "continue_ai_guided_branch_search",
            "widen_radius_or_traverse_bridge",
            "rerun_ai_spatial_or_matrix_calibration_preview",
        }:
            master_follow_up_tool = "retrieve_path_corridor"
    if master_follow_up_tool and client_payload_state == "partial_context":
        next_recommended_call = {
            "tool": master_follow_up_tool,
            "endpoint": _mcp_endpoint_for_tool(master_follow_up_tool),
            "arguments": {"search_id": search_id or "<search_id_from_first_response>"},
            "reason": f"master_judgement:{master_state or 'partial'}",
        }
    elif client_payload_state in {"blocked", "failed", "waiting"} and inspect_endpoint:
        next_recommended_call = {
            "tool": inspection.get("inspect_tool") or "inspect_context_package",
            "endpoint": inspect_endpoint,
            "arguments": _as_dict(inspection.get("inspect_arguments") or inspection.get("arguments")),
            "reason": "inspect_blocked_or_waiting_run_state",
        }
    elif context_path_truth_pending and inspect_endpoint:
        next_recommended_call = {
            "tool": "inspect_context_package",
            "endpoint": inspect_endpoint,
            "arguments": _as_dict(inspection.get("inspect_arguments") or inspection.get("arguments")),
            "reason": "context_path_truth_pending",
        }
    elif terminal_ai_spatial_supervisor_waiting and inspect_endpoint:
        next_recommended_call = {
            "tool": inspection.get("inspect_tool") or "inspect_context_package",
            "endpoint": inspect_endpoint,
            "arguments": _as_dict(inspection.get("inspect_arguments") or inspection.get("arguments")),
            "reason": "terminal_supervisor_waiting_ai_spatial",
        }
    elif document_follow_up_needed and int(document_ref_contract.get("actionable_document_ref_count") or 0) > 0:
        next_recommended_call = {
            "tool": "retrieve_document",
            "endpoint": "/mcp/retrieve-document",
            "arguments": _as_dict(document_delivery_contract.get("exact_follow_up_recipe")).get("arguments") or {},
            "reason": "raw_document_text_available_by_follow_up",
        }
    elif final_materialization_pending and inspect_endpoint:
        next_recommended_call = {
            "tool": inspection.get("inspect_tool") or "inspect_context_package",
            "endpoint": inspect_endpoint,
            "arguments": _as_dict(inspection.get("inspect_arguments") or inspection.get("arguments")),
            "reason": "background_completion_pending",
        }
    elif is_path_tool and path_state in {"planned", "traversing", "partial"} and inspect_endpoint:
        next_recommended_call = {
            "tool": "inspect_path_corridor",
            "endpoint": "/mcp/inspect-path-corridor",
            "arguments": {"search_id": search_id or "<search_id_from_first_response>"},
            "reason": "path_corridor_can_be_reinspected",
        }
    else:
        next_recommended_call = None

    return {
        "schema_version": MCP_DELIVERY_CONTRACT_SCHEMA_VERSION,
        "tool_name": normalized_tool,
        "effective_delivery_tool_name": effective_tool,
        "originating_tool_name": originating_tool or None,
        "search_id": result.get("search_id"),
        "status_legacy": status,
        "master_state": master_state or None,
        "master_next_recommended_call": master_next_action or None,
        "master_override_applied": master_override_applied,
        "master_override_blocked_reason": "snapshot_context_contract_passed_while_master_no_match" if master_override_blocked_by_snapshot_no_match else None,
        "client_payload_state": client_payload_state,
        "completion_state": completion_state,
        "path_truth": {
            "required": context_path_truth_required,
            "ready": context_path_truth_ready,
            "mission_surface_missing": path_mission_surface_missing,
            "state": path_truth_contract.get("state"),
            "planned_path_count": path_truth_contract.get("planned_path_count"),
            "path_count": path_truth_contract.get("path_count"),
            "route_event_count": path_truth_contract.get("route_event_count"),
            "changed_context_package_path_count": path_truth_contract.get("changed_context_package_path_count"),
            "pending_reasons": _as_list(path_truth_contract.get("pending_reasons")),
            "missing_reasons": _as_list(path_truth_contract.get("missing_reasons")),
        },
        "run_finished": run_finished,
        "terminal_for_inspection": terminal_for_inspection,
        "background_state": background_state,
        "background_cap": {
            "requested": bool(background_cap.get("requested")),
            "reason": background_cap.get("reason"),
            "policy": background_cap.get("policy"),
        },
        "terminal_for_client": terminal_for_client,
        "final_materialization_pending": final_materialization_pending,
        "client_payload_finalized_over_background": bool(
            selected_payload_client_sealed
            or positive_exact_client_sealed
            or public_fact_client_sealed
            or answerability_client_sealed
            or document_refs_client_sealed
        ),
        "terminal_ai_spatial_supervisor": {
            "schema_version": "agvm.terminal_ai_spatial_supervisor.v1",
            "applies": terminal_ai_spatial_supervisor_applies,
            "state": terminal_ai_spatial_supervisor_state,
            "terminal_certification_allowed": bool(
                not terminal_ai_spatial_supervisor_applies
                or terminal_ai_spatial_ready
            ),
            "content_sufficiency_present": terminal_ai_spatial_content_sufficiency_present,
            "blocked_terminal_candidate": terminal_ai_spatial_supervisor_waiting,
            "client_payload_state_before": terminal_ai_spatial_supervisor_before_state,
            "client_payload_state_after": client_payload_state,
            "ai_spatial_observed": original_ai_spatial_observed,
            "ai_spatial_materialized": original_ai_spatial_materialized,
            "ai_spatial_deferred": original_ai_spatial_deferred,
            "ai_spatial_missing": original_ai_spatial_missing,
            "content_contracts": {
                "positive_exact": positive_exact_sufficiency,
                "public_fact": public_fact_sufficiency,
                "answerability": answerability_sufficiency,
            },
        },
        "partial_for_client": client_payload_state.startswith("partial_"),
        "context_contract": {
            "passed": context_passed,
            "effective_passed": context_effective_passed,
            "soft_unresolved_sections_allowed": context_soft_unresolved_sections_allowed,
            "unresolved_sections": context_unresolved_sections[:12],
        },
        "target_document_need_contract": {
            "observed": bool(target_document_need_contract),
            "schema_version": target_document_need_contract.get("schema_version"),
            "classification": target_document_need_contract.get("classification"),
            "need_type": target_document_need_contract.get("need_type"),
            "document_evidence": bool(target_document_need_contract.get("document_evidence")),
            "pure_document_evidence": bool(target_document_need_contract.get("pure_document_evidence")),
            "normal_context_required": bool(target_document_need_contract.get("normal_context_required")),
            "semantic_document_mode": target_document_need_contract.get("semantic_document_mode"),
            "reason_codes": _as_list(target_document_need_contract.get("reason_codes"))[:16],
            "target_document_need": target_document_need,
        },
        "first_payload": {
            "present": package_present,
            "field": primary_payload.get("field") or first_package.get("field") or package_field,
            "char_count": int(primary_payload.get("char_count") or first_package.get("char_count") or 0),
            "sha256": primary_payload.get("sha256") or first_package.get("sha256"),
        },
        "ai_state": ai_state,
        "metamemory": {
            "observed": metamemory_contract_observed,
            "ready": metamemory_ready,
            "base_ready": metamemory_base_ready,
            "spatial_readiness_observed": metamemory_readiness_observed,
            "spatial_readiness_status": metamemory_spatial_readiness.get("status"),
            "spatial_readiness_certifiable": metamemory_spatial_readiness.get("certifiable"),
            "missing_reasons": _as_list(metamemory_spatial_readiness.get("missing_reasons"))[:12],
            "stale_reasons": _as_list(metamemory_spatial_readiness.get("stale_reasons"))[:12],
            "revision_chain": _as_dict(metamemory_spatial_readiness.get("revision_chain")),
            "schema_version": metamemory_snapshot.get("schema_version"),
            "revision": metamemory_snapshot.get("snapshot_version"),
            "hash": metamemory_snapshot.get("hash"),
            "spatial_brief_exists": bool(metamemory_snapshot.get("spatial_brief_exists")),
            "spatial_revision": metamemory_spatial_brief.get("revision"),
        },
        "ai_spatial_landing_contract": {
            "observed": ai_spatial_contract_observed,
            "materialized": ai_spatial_materialized,
            "certifiable": bool(ai_spatial_landing_contract.get("certifiable")),
            "schema_version": ai_spatial_landing_contract.get("schema_version"),
            "status": ai_spatial_landing_contract.get("status"),
            "source": ai_spatial_landing_contract.get("source"),
            "planner_latency_ms": ai_spatial_landing_contract.get("planner_latency_ms"),
            "metamemory_spatial_revision": ai_spatial_landing_contract.get("metamemory_spatial_revision"),
            "metrics": _as_dict(ai_spatial_landing_contract.get("metrics")),
            "missing_reasons": _as_list(ai_spatial_landing_contract.get("missing_reasons"))[:12],
        },
        "path_mission_contract": {
            "observed": bool(
                _as_dict(result.get("path_mission_contract"))
                or _as_dict(planner_runtime.get("path_mission_contract"))
            ),
            "status": (
                _as_dict(result.get("path_mission_contract"))
                or _as_dict(planner_runtime.get("path_mission_contract"))
            ).get("status"),
            "materialized": bool(
                (
                    _as_dict(result.get("path_mission_contract"))
                    or _as_dict(planner_runtime.get("path_mission_contract"))
                ).get("materialized")
            ),
            "mission_count": int(
                (
                    _as_dict(result.get("path_mission_contract"))
                    or _as_dict(planner_runtime.get("path_mission_contract"))
                ).get("mission_count")
                or len(
                    _as_list(result.get("path_missions"))
                    or _as_list(planner_runtime.get("path_missions"))
                )
                or 0
            ),
            "schema_version": (
                _as_dict(result.get("path_mission_contract"))
                or _as_dict(planner_runtime.get("path_mission_contract"))
            ).get("schema_version"),
        },
        "ai": {
            "required": ai_required,
            "materialized": ai_materialized,
            "no_route_terminal_contract": no_route_terminal_contract,
            "positive_exact_sufficiency_contract": positive_exact_sufficiency_contract,
            "public_fact_sufficiency_contract": public_fact_sufficiency_contract,
            "answerability_sufficiency_contract": answerability_sufficiency_contract,
            "path_route_first_sufficiency_contract": path_route_first_sufficiency_contract,
            "raw_material_observed": bool(ai_resilience.get("ai_materialized")),
            "materialization_source": ai_resilience.get("materialization_source"),
            "readiness_certifiable": bool(ai_resilience.get("readiness_certifiable")),
            "critical_path_state": ai_critical_path.get("state"),
            "critical_path_certifiable": bool(ai_critical_path.get("certifiable")),
            "compact_contract_ready": bool(ai_critical_path.get("compact_contract_ready")),
            "compact_contract_required": bool(ai_critical_path.get("compact_contract_required")),
            "first_ai_contract_ms": _as_dict(ai_resilience.get("semantic_contract")).get("first_ai_contract_ms"),
            "cache_valid_for_ai": bool(_as_dict(ai_resilience.get("cache")).get("valid_for_ai")),
            "route_arbitration_state": route_arbitration.get("state"),
            "route_arbitration_certifiable": bool(route_arbitration.get("certifiable_for_first_payload")),
            "runtime_route_arbitrated": bool(route_arbitration.get("runtime_arbitrated")),
            "heuristic_can_certify": bool(route_arbitration.get("heuristic_can_certify")),
        },
        "route_arbitration": {
            "state": route_arbitration.get("state"),
            "certifiable_for_first_payload": bool(route_arbitration.get("certifiable_for_first_payload")),
            "runtime_arbitrated": bool(route_arbitration.get("runtime_arbitrated")),
            "ai_route_plan_present": bool(_as_dict(route_arbitration.get("route_plan")).get("present")),
            "ai_spatial_route_plan_present": bool(ai_spatial_landing_contract.get("materialized")),
            "heuristic_can_certify": bool(route_arbitration.get("heuristic_can_certify")),
            "path_budget": _as_dict(route_arbitration.get("path_budget")),
            "candidate_families": _as_dict(route_arbitration.get("candidate_families")),
        },
        "provider_state": provider_state,
        "document_state": document_state,
        "document_workspace_refs_terminality": document_workspace_refs_terminality,
        "path_state": path_state,
        "missing_reasons": _dedupe_contract_items(missing_reasons, limit=24),
        "pending_reasons": _dedupe_contract_items(pending_reasons, limit=16),
        "available_follow_up_reasons": (
            ["raw_document_follow_up_available"]
            if raw_document_follow_up_available and document_workspace_refs_terminal
            else []
        ),
        "inspect_endpoint": inspect_endpoint,
        "stream_endpoint": stream_endpoint,
        "query_result_endpoint": query_result_endpoint,
        "next_recommended_call": next_recommended_call,
        "client_law": {
            "use_client_payload_state_not_legacy_status": True,
            "status_legacy_kept_for_backward_compatibility": True,
            "agent_payload_is_primary": True,
            "answer_demo_secondary": True,
            "refresh_must_reattach_by_search_id": bool(search_id),
        },
        "ui_contract": {
            "single_terminal_badge_source": "mcp_delivery_contract.client_payload_state",
            "show_missing_reasons_when_blocked": True,
            "show_pending_reasons_separately_from_missing_reasons": True,
            "show_inspect_and_stream_endpoints": True,
        },
    }


def _client_supervised_master_judgement(
    master_judgement: dict[str, Any],
    *,
    mcp_delivery_contract: dict[str, Any],
) -> dict[str, Any]:
    exposed = deepcopy(_as_dict(master_judgement))
    if not exposed:
        return {}
    delivery = _as_dict(mcp_delivery_contract)
    if not delivery:
        return exposed

    delivery_terminal_for_client = bool(delivery.get("terminal_for_client"))
    previous_terminal_for_client = bool(exposed.get("terminal_for_client"))
    client_payload_state = str(delivery.get("client_payload_state") or "").strip()
    supervisor = _as_dict(delivery.get("terminal_ai_spatial_supervisor"))
    supervisor_blocked = bool(supervisor.get("blocked_terminal_candidate"))
    terminal_blocked = bool(previous_terminal_for_client and not delivery_terminal_for_client)
    terminal_blocked_by = (
        "terminal_ai_spatial_supervisor"
        if supervisor_blocked
        else "mcp_delivery_contract"
        if terminal_blocked
        else None
    )

    if "terminal_for_client" in exposed:
        exposed["terminal_for_client_before_delivery_supervision"] = previous_terminal_for_client
    exposed["terminal_for_client"] = delivery_terminal_for_client
    exposed["delivery_terminal_for_client"] = delivery_terminal_for_client
    if client_payload_state:
        exposed["client_payload_state"] = client_payload_state
    exposed["delivery_supervision"] = {
        "schema_version": "agvm.master_judgement.delivery_supervision.v1",
        "source": "mcp_delivery_contract",
        "terminal_for_client": delivery_terminal_for_client,
        "client_payload_state": client_payload_state or None,
        "terminal_blocked": terminal_blocked,
        "terminal_blocked_by": terminal_blocked_by,
        "terminal_ai_spatial_supervisor_state": supervisor.get("state"),
        "terminal_ai_spatial_certification_allowed": supervisor.get("terminal_certification_allowed"),
    }
    if terminal_blocked:
        reason_code = (
            "terminal_blocked_by_ai_spatial_supervisor"
            if supervisor_blocked
            else "terminal_blocked_by_delivery_contract"
        )
        exposed["reason_codes"] = _dedupe_contract_items(
            _as_list(exposed.get("reason_codes")) + [reason_code],
            limit=24,
        )
    return exposed


def _projection_render_instruction(event_type: str, event: dict[str, Any]) -> str:
    event_type = str(event_type or "").strip()
    node_id = str(event.get("node_id") or "").strip()
    if event_type in {"landing_materialized", "ai_landing_materialized"}:
        return "spawn_landing_dot"
    if event_type == "node_materialized":
        if node_id.startswith("landing::"):
            return "spawn_landing_dot"
        return "spawn_node_dot"
    if event_type == "route_traversed":
        return "draw_traversed_path_segment"
    if event_type in {"context_promoted", "package_promoted"}:
        return "pulse_promoted_hot_node"
    if event_type in {"document_ref_materialized", "document_anchor_materialized"}:
        return "spawn_document_anchor"
    return "append_trace_event"


def _run_projection_event_stream_contract(
    result: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    run_projection_truth: dict[str, Any],
    first_package_background_contract: dict[str, Any],
) -> dict[str, Any]:
    nodes = [_as_dict(node) for node in _as_list(run_projection_truth.get("nodes")) if isinstance(node, dict)]
    edges = [_as_dict(edge) for edge in _as_list(run_projection_truth.get("edges")) if isinstance(edge, dict)]
    paths = [_as_dict(path) for path in _as_list(run_projection_truth.get("paths")) if isinstance(path, dict)]
    events = [_as_dict(event) for event in _as_list(run_projection_truth.get("events")) if isinstance(event, dict)]
    summary = _as_dict(run_projection_truth.get("summary"))
    invariants = _as_dict(run_projection_truth.get("invariants"))
    search_id = str(result.get("search_id") or run_projection_truth.get("search_id") or "").strip()
    event_kinds = _dedupe_contract_items([event.get("type") or event.get("event_type") for event in events], limit=32)
    node_ids = {str(node.get("id") or "").strip() for node in nodes if str(node.get("id") or "").strip()}
    edge_node_ids = {
        str(value or "").strip()
        for edge in edges
        for value in (edge.get("from"), edge.get("to"), edge.get("from_node_id"), edge.get("to_node_id"))
        if str(value or "").strip()
    }
    orphan_edge_node_ids = sorted(edge_node_ids - node_ids)
    replay_sequence: list[dict[str, Any]] = []
    for index, event in enumerate(events[:160], start=1):
        event_type = str(event.get("type") or event.get("event_type") or "event").strip()
        replay_sequence.append(
            {
                "seq": int(event.get("index") or event.get("seq") or index),
                "time_ms": event.get("time_ms"),
                "event_type": event_type,
                "render_instruction": _projection_render_instruction(event_type, event),
                "node_id": event.get("node_id"),
                "landing_id": event.get("landing_id"),
                "edge_id": event.get("edge_id"),
                "path_id": event.get("path_id") or event.get("route_id"),
                "from_node_id": event.get("from_node_id"),
                "to_node_id": event.get("to_node_id"),
                "origin_kind": event.get("origin_kind"),
                "source_truth": "run_projection_truth.events",
            }
        )
    projection_present = bool(nodes or edges or paths or events)
    replay_available = bool(replay_sequence)
    projection_state = (
        "projection_ready"
        if projection_present and replay_available and not orphan_edge_node_ids
        else "projection_partial_missing_endpoint_nodes"
        if projection_present and orphan_edge_node_ids
        else "diagnostic_empty_projection"
    )
    stream_endpoint = (
        _as_dict(first_package_background_contract.get("stream_vs_final")).get("stream_endpoint")
        or (f"/memory/query-stream/{search_id}" if search_id else None)
    )
    return {
        "schema_version": MCP_RUN_PROJECTION_EVENT_STREAM_CONTRACT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": search_id or None,
        "status": status,
        "projection_state": projection_state,
        "selected_run_projection_available": projection_present,
        "event_source": {
            "primary": "run_projection_truth.events",
            "truth_schema_version": run_projection_truth.get("schema_version") or run_projection_truth.get("schema"),
            "event_count": len(events),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "path_count": len(paths),
            "event_kinds_present": event_kinds,
        },
        "replay": {
            "available": replay_available,
            "sequence": replay_sequence,
            "sequence_count": len(replay_sequence),
            "truncated": len(events) > len(replay_sequence),
            "replay_after_refresh_available": bool(search_id and invariants.get("can_replay_after_refresh", True)),
            "trace_endpoint": f"/memory/get-trace/{search_id}" if search_id else None,
            "query_result_endpoint": f"/memory/query-result/{search_id}" if search_id else None,
            "stream_endpoint": stream_endpoint,
        },
        "counts": {
            "ai_landing_count": int(summary.get("ai_landings") or 0),
            "heuristic_probe_count": int(summary.get("heuristic_probes") or 0),
            "promoted_node_count": int(summary.get("promoted_nodes") or 0),
            "hot_node_count": int(summary.get("hot_nodes") or 0),
            "cold_node_count": int(summary.get("cold_nodes") or 0),
            "document_ref_count": int(summary.get("document_refs") or 0),
            "route_edge_count": len(edges),
            "planned_path_count": len(paths),
            "blocked_path_count": int(summary.get("blocked_events") or 0),
        },
        "rendering_contract": {
            "validation_motion_source": "backend_projection_events_only",
            "synthetic_motion_allowed": False,
            "css_perpetual_motion_allowed": False,
            "labels_default_visible": False,
            "labels_on_hover_or_selection_only": True,
            "dot_first_render": True,
            "render_selected_run_only": True,
            "full_brain_render_is_separate": True,
            "orphan_edge_node_ids": orphan_edge_node_ids[:24],
        },
        "blank_fallback": {
            "blank_canvas_allowed": False,
            "state": "none_required" if projection_present else "diagnostic_empty_projection_required",
            "message": (
                "Selected-run projection has backend events and can render."
                if projection_present
                else "No selected-run projection events are available; render a diagnostic state with search id, tool and follow-up endpoints instead of a blank canvas."
            ),
            "fallback_card_required": not projection_present,
        },
        "ui_contract": {
            "render_2d_and_3d_from_this_contract": True,
            "do_not_infer_motion_from_counts_or_debug_tables": True,
            "never_use_random_motion_for_validation": True,
            "open_3d_requires_projection_or_diagnostic": True,
            "show_landing_dots_without_labels_until_hover": True,
            "show_progress_from_replay_sequence": True,
        },
    }


def _answer_demo(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": _as_dict(result.get("answer")),
        "answer_short": result.get("answer_short"),
        "answer_full": result.get("answer_full"),
        "answer_surface_state": result.get("answer_surface_state"),
        "materialization": _as_dict(result.get("answer_demo_materialization")),
    }


def build_mcp_retrieval_tool_output(
    tool_name: str,
    result: dict[str, Any],
    *,
    include_answer_demo: bool = False,
    include_raw_text: bool = False,
) -> dict[str, Any]:
    if tool_name not in PR12J_B_RETRIEVAL_TOOL_NAMES - {"inspect_route", "inspect_memory_object"}:
        raise ValueError(f"unsupported_mcp_retrieval_tool:{tool_name}")
    normalized = normalize_retrieve_response_payload(dict(result or {}))
    fallback_document_workspace = _result_document_workspace(normalized)
    if fallback_document_workspace and not _document_workspace_has_documents(_as_dict(normalized.get("document_workspace"))):
        normalized["document_workspace"] = deepcopy(fallback_document_workspace)
    embedded_context_path_corridors = _as_dict(_as_dict(normalized.get("context_package")).get("path_corridors"))
    if embedded_context_path_corridors and not _as_dict(normalized.get("path_corridors")):
        normalized["path_corridors"] = embedded_context_path_corridors
    package_field, package = _package_for_tool(tool_name, normalized)
    if package_field == "context_package" and _context_package_exact_no_match(_as_dict(package)):
        package = _redact_exact_no_match_context_package(_as_dict(package))
        normalized["context_package"] = package
        query_text = _query_text_for_master(normalized, _as_dict(package))
        exact_no_match_ledger = _exact_no_match_mission_ledger(query_text, _as_dict(package))
        exact_no_match_master = _exact_no_match_master_judgement(query_text, _as_dict(package))
        planner_runtime = _as_dict(normalized.get("planner_runtime"))
        planner_runtime["mission_evidence_ledger"] = exact_no_match_ledger
        planner_runtime["master_judgement"] = exact_no_match_master
        normalized["planner_runtime"] = planner_runtime
        normalized["mission_evidence_ledger"] = exact_no_match_ledger
        normalized["master_judgement"] = exact_no_match_master
        package["master_judgement"] = exact_no_match_master
        normalized["document_refs"] = []
        normalized["document_workspace"] = {}
        normalized["supporting_documents"] = []
        normalized["document_packets"] = []
        normalized["source_trace"] = []
        normalized["document_ref_contract"] = {
            "schema_version": "agvm.document_ref_contract.v1",
            "state": "suppressed_for_exact_field_no_match",
            "document_ref_count": 0,
            "actionable_document_ref_count": 0,
            "raw_available_document_ref_count": 0,
            "all_refs_actionable": True,
        }
        normalized["document_delivery_contract"] = {
            "schema_version": "agvm.document_delivery_contract.v1",
            "state": "suppressed_for_exact_field_no_match",
            "document_text_policy": "refs_only",
            "primary_payload_field": "context_package.agent_markdown",
            "mcp_client_receives_first": "no_match_exact_field_absence",
            "raw_text_already_in_primary_payload": False,
            "raw_text_follow_up_required": False,
            "document_ref_count": 0,
            "actionable_document_ref_count": 0,
            "raw_available_document_ref_count": 0,
            "raw_included_document_count": 0,
            "raw_available_not_included_count": 0,
            "metadata_only_document_ref_count": 0,
            "all_refs_actionable": True,
        }
        normalized["document_bundle"] = {}
    package_payload = deepcopy(package)
    if not include_raw_text:
        package_payload = _strip_raw_text(package_payload)
    else:
        package_payload = _cap_raw_text(package_payload)
        if package_field == "document_workspace":
            package_payload = _cap_source_trace_text(package_payload, include_agent_markdown=True)
    context_package = _as_dict(normalized.get("context_package"))
    document_workspace = _as_dict(normalized.get("document_workspace"))
    effective_output_tool = _effective_delivery_tool_name(tool_name, normalized)
    if effective_output_tool in DOCUMENT_WORKSPACE_MCP_TOOL_NAMES:
        visible_document_refs = _visible_document_refs(
            document_workspace.get("document_refs"),
            document_workspace.get("primary_document_refs"),
            document_workspace.get("candidate_document_refs"),
            _document_refs_from_workspace_documents(document_workspace),
            normalized.get("document_refs"),
            context_package.get("document_refs"),
            _document_refs_from_workspace_documents(_as_dict(context_package.get("document_workspace"))),
            _document_refs_from_workspace_documents({"documents": normalized.get("supporting_documents")}),
            _document_refs_from_workspace_documents({"documents": normalized.get("document_packets")}),
        )
    else:
        visible_document_refs = _visible_document_refs(
            normalized.get("document_refs"),
            context_package.get("document_refs"),
            document_workspace.get("document_refs"),
            _document_refs_from_workspace_documents(document_workspace),
            _document_refs_from_workspace_documents(_as_dict(context_package.get("document_workspace"))),
            _document_refs_from_workspace_documents({"documents": normalized.get("supporting_documents")}),
            _document_refs_from_workspace_documents({"documents": normalized.get("document_packets")}),
        )
    document_ref_contract = _as_dict(
        normalized.get("document_ref_contract")
        or context_package.get("document_ref_contract")
        or document_workspace.get("document_ref_contract")
    )
    document_ref_contract = _repair_document_ref_contract_from_visible_refs(document_ref_contract, visible_document_refs)
    document_delivery_contract = _as_dict(
        normalized.get("document_delivery_contract")
        or context_package.get("document_delivery_contract")
        or document_workspace.get("document_delivery_contract")
    )
    document_bundle_payload = deepcopy(_as_dict(normalized.get("document_bundle") or context_package.get("document_bundle")))
    if not include_raw_text:
        document_bundle_payload = _strip_raw_text(document_bundle_payload)
    else:
        document_bundle_payload = _cap_raw_text(document_bundle_payload)
    document_text_policy = str(
        normalized.get("document_text_policy")
        or context_package.get("document_text_policy")
        or document_bundle_payload.get("document_text_policy")
        or "refs_only"
    )
    document_delivery_contract = _repair_document_delivery_contract_from_visible_refs(
        document_delivery_contract,
        refs=visible_document_refs,
        document_ref_contract=document_ref_contract,
        document_text_policy=document_text_policy,
        document_bundle_payload=document_bundle_payload,
    )
    normalized["document_ref_contract"] = document_ref_contract
    normalized["document_delivery_contract"] = document_delivery_contract
    context_package["document_ref_contract"] = document_ref_contract
    context_package["document_delivery_contract"] = document_delivery_contract
    normalized["context_package"] = context_package
    timing_payload = _timing(normalized)
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    normalized["planner_runtime"] = planner_runtime
    _repair_master_judgement_from_delivery_ledger(
        normalized,
        package_payload=package_payload,
        visible_document_refs=visible_document_refs,
    )
    planner_runtime = _as_dict(normalized.get("planner_runtime"))
    semantic_contract_runtime = _as_dict(
        normalized.get("semantic_contract_runtime")
        or planner_runtime.get("semantic_contract_runtime")
        or timing_payload.get("semantic_contract")
    )
    semantic_contract_payload = _as_dict(
        normalized.get("semantic_contract")
        or planner_runtime.get("semantic_contract")
        or _as_dict(semantic_contract_runtime.get("semantic_contract"))
    )
    target_document_need_contract = _as_dict(
        normalized.get("target_document_need_contract")
        or semantic_contract_payload.get("target_document_need_contract")
        or _as_dict(semantic_contract_payload.get("document_contract")).get("target_document_need_contract")
    )
    target_document_need = _as_dict(
        normalized.get("target_document_need")
        or semantic_contract_payload.get("target_document_need")
        or _as_dict(semantic_contract_payload.get("document_contract")).get("target_document_need")
        or target_document_need_contract.get("target_document_need")
    )
    if (
        effective_output_tool in DOCUMENT_PAYLOAD_MCP_TOOL_NAMES | {"retrieve_source_trace"}
        and not bool(target_document_need_contract.get("document_evidence"))
    ):
        target_document_need_contract = build_target_document_need_contract(
            str(normalized.get("query_text") or ""),
            tool_name=effective_output_tool,
        )
        target_document_need = _as_dict(target_document_need_contract.get("target_document_need"))
        normalized["target_document_need_contract"] = target_document_need_contract
        normalized["target_document_need"] = target_document_need
    status = _status_for_tool(tool_name, normalized, package)
    _reconcile_master_judgement_with_selected_context_package(
        normalized,
        tool_name=tool_name,
        status=status,
        package_payload=package_payload,
    )
    mission_learning_rollup = _refresh_mission_learning_rollup_for_output(
        normalized,
        package_payload=package_payload,
    )
    run_projection_truth = _as_dict(normalized.get("run_projection_truth"))
    if _projection_truth_stale_for_ai_materialization(normalized, run_projection_truth):
        run_projection_truth = build_run_projection_truth(normalized, status=status)
        normalized["run_projection_truth"] = run_projection_truth
    latency_payload = _latency_contract(normalized, tool_name=tool_name, package_field=package_field, timing=timing_payload)
    completion_payload = _completion_contract(
        normalized,
        tool_name=tool_name,
        package_field=package_field,
        package=package,
        status=status,
        timing=timing_payload,
        latency_contract=latency_payload,
    )
    latency_payload["completion_contract"] = {
        "schema_version": completion_payload["schema_version"],
        "state": completion_payload["state"],
        "visible_reason": completion_payload["visible_reason"],
        "inspection": completion_payload["inspection"],
    }
    payload_integrity = _payload_integrity(normalized, tool_name=tool_name, package_field=package_field, package=package)
    payload_truth_contract = _payload_truth_contract(
        normalized,
        tool_name=tool_name,
        package_field=package_field,
        package_payload=package_payload,
        status=status,
        completion_contract=completion_payload,
        payload_integrity=payload_integrity,
        document_ref_contract=document_ref_contract,
        document_delivery_contract=document_delivery_contract,
        document_bundle_payload=document_bundle_payload,
    )
    run_lifecycle_contract = _run_lifecycle_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        timing=timing_payload,
        completion_contract=completion_payload,
        payload_truth_contract=payload_truth_contract,
    )
    runtime_state_contract = _runtime_state_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        package_field=package_field,
        package=package,
        timing=timing_payload,
        completion_contract=completion_payload,
        payload_truth_contract=payload_truth_contract,
        run_lifecycle_contract=run_lifecycle_contract,
    )
    tool_boundary_contract = _tool_boundary_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        package_field=package_field,
        package_payload=package_payload,
        include_raw_text=include_raw_text,
        completion_contract=completion_payload,
        runtime_state_contract=runtime_state_contract,
        document_ref_contract=document_ref_contract,
        document_delivery_contract=document_delivery_contract,
    )
    ai_materialization_resilience_contract = _ai_materialization_resilience_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        timing=timing_payload,
        runtime_state_contract=runtime_state_contract,
        completion_contract=completion_payload,
    )
    ai_critical_path_contract = _ai_critical_path_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        timing=timing_payload,
        runtime_state_contract=runtime_state_contract,
        ai_materialization_resilience_contract=ai_materialization_resilience_contract,
    )
    route_arbitration_contract = _route_arbitration_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        runtime_state_contract=runtime_state_contract,
        ai_critical_path_contract=ai_critical_path_contract,
    )
    first_package_background_contract = _first_package_background_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        package_field=package_field,
        timing=timing_payload,
        completion_contract=completion_payload,
        runtime_state_contract=runtime_state_contract,
        tool_boundary_contract=tool_boundary_contract,
    )
    run_projection_event_stream_contract = _run_projection_event_stream_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        run_projection_truth=run_projection_truth,
        first_package_background_contract=first_package_background_contract,
    )
    mcp_delivery_contract = _mcp_delivery_contract(
        normalized,
        tool_name=tool_name,
        status=status,
        package_field=package_field,
        package_payload=package_payload,
        completion_contract=completion_payload,
        runtime_state_contract=runtime_state_contract,
        tool_boundary_contract=tool_boundary_contract,
        ai_materialization_resilience_contract=ai_materialization_resilience_contract,
        ai_critical_path_contract=ai_critical_path_contract,
        route_arbitration_contract=route_arbitration_contract,
        first_package_background_contract=first_package_background_contract,
        payload_truth_contract=payload_truth_contract,
        document_ref_contract=document_ref_contract,
        document_delivery_contract=document_delivery_contract,
    )
    raw_master_judgement = _as_dict(
        normalized.get("master_judgement")
        or _as_dict(package_payload).get("master_judgement")
        or _as_dict(normalized.get("planner_runtime")).get("master_judgement")
    )
    exposed_master_judgement = _client_supervised_master_judgement(
        raw_master_judgement,
        mcp_delivery_contract=mcp_delivery_contract,
    )
    if exposed_master_judgement and isinstance(package_payload, dict):
        package_payload = deepcopy(package_payload)
        package_payload["master_judgement"] = exposed_master_judgement
    output: dict[str, Any] = {
        "schema_version": MCP_RETRIEVAL_OUTPUT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "search_id": normalized.get("search_id"),
        "status": status,
        package_field: package_payload,
        "context_package_materialization": _as_dict(normalized.get("context_package_materialization")),
        "hot_working_memory": _as_dict(normalized.get("hot_working_memory")),
        "hot_working_memory_contract": _as_dict(normalized.get("hot_working_memory_contract")),
        "answer_demo_materialization": _as_dict(normalized.get("answer_demo_materialization")),
        "semantic_contract": semantic_contract_payload,
        "semantic_contract_runtime": semantic_contract_runtime,
        "target_document_need_contract": target_document_need_contract,
        "target_document_need": target_document_need,
        "metamemory_snapshot": _as_dict(normalized.get("metamemory_snapshot") or _as_dict(normalized.get("planner_runtime")).get("metamemory_snapshot")),
        "metamemory_spatial_brief": _as_dict(
            normalized.get("metamemory_spatial_brief")
            or _as_dict(normalized.get("planner_runtime")).get("metamemory_spatial_brief")
        ),
        "metamemory_spatial_brief_summary": _as_dict(
            normalized.get("metamemory_spatial_brief_summary")
            or _as_dict(normalized.get("planner_runtime")).get("metamemory_spatial_brief_summary")
        ),
        "metamemory_spatial_readiness": _as_dict(
            normalized.get("metamemory_spatial_readiness")
            or _as_dict(normalized.get("planner_runtime")).get("metamemory_spatial_readiness")
            or _as_dict(
                normalized.get("metamemory_spatial_brief")
                or _as_dict(normalized.get("planner_runtime")).get("metamemory_spatial_brief")
            ).get("spatial_readiness_contract")
        ),
        "ai_spatial_landing_contract": _as_dict(
            normalized.get("ai_spatial_landing_contract")
            or _as_dict(normalized.get("planner_runtime")).get("ai_spatial_landing_contract")
        ),
        "ai_spatial_landing_contract_runtime": _as_dict(
            normalized.get("ai_spatial_landing_contract_runtime")
            or _as_dict(normalized.get("planner_runtime")).get("ai_spatial_landing_contract_runtime")
        ),
        "path_mission_contract": _as_dict(
            normalized.get("path_mission_contract")
            or _as_dict(normalized.get("planner_runtime")).get("path_mission_contract")
        ),
        "path_missions": _as_list(
            normalized.get("path_missions")
            or _as_dict(normalized.get("planner_runtime")).get("path_missions")
        ),
        "mission_aware_merge_summary": _as_dict(
            normalized.get("mission_aware_merge_summary")
            or _as_dict(normalized.get("planner_runtime")).get("mission_aware_merge_summary")
            or _as_dict(normalized.get("planner_runtime")).get("ai_spatial_merge_summary")
        ),
        "mission_evidence_ledger": _as_dict(
            normalized.get("mission_evidence_ledger")
            or _as_dict(normalized.get("planner_runtime")).get("mission_evidence_ledger")
        ),
        "master_judgement": exposed_master_judgement,
        "mission_learning_rollup": _as_dict(
            mission_learning_rollup
            or normalized.get("mission_learning_rollup")
            or _as_dict(package_payload).get("mission_learning_rollup")
            or _as_dict(normalized.get("planner_runtime")).get("mission_learning_rollup")
        ),
        "ai_landing_materialization": _as_dict(normalized.get("ai_landing_materialization")),
        "ai_materialization_hard_gate": _as_dict(normalized.get("ai_materialization_hard_gate")),
        "mcp_background_cap": _as_dict(normalized.get("mcp_background_cap")),
        "document_text_policy": document_text_policy,
        "document_refs": visible_document_refs,
        "document_ref_contract": document_ref_contract,
        "document_delivery_contract": document_delivery_contract,
        "document_bundle": document_bundle_payload,
        "path_tool_storage_contract": _as_dict(normalized.get("path_tool_storage_contract")),
        "path_corridors": deepcopy(_as_dict(normalized.get("path_corridors"))),
        "source_trace": (
            _cap_source_trace_text(_cap_raw_text(_as_list(normalized.get("source_trace"))))
            if include_raw_text
            else _strip_raw_text(_as_list(normalized.get("source_trace")))
        ),
        "completeness": _completeness(normalized, tool_name=tool_name, package_field=package_field, package=package),
        "payload_integrity": payload_integrity,
        "payload_truth_contract": payload_truth_contract,
        "budget": _budget(normalized),
        "timing": timing_payload,
        "latency_contract": latency_payload,
        "completion_contract": completion_payload,
        "run_lifecycle_contract": run_lifecycle_contract,
        "runtime_state_contract": runtime_state_contract,
        "tool_boundary_contract": tool_boundary_contract,
        "ai_materialization_resilience_contract": ai_materialization_resilience_contract,
        "ai_critical_path_contract": ai_critical_path_contract,
        "route_arbitration_contract": route_arbitration_contract,
        "first_package_background_contract": first_package_background_contract,
        "run_projection_event_stream_contract": run_projection_event_stream_contract,
        "mcp_delivery_contract": mcp_delivery_contract,
        "run_projection_truth": run_projection_truth,
        "model_profile": _as_dict(_as_dict(normalized.get("planner_runtime")).get("fast_model_profile")),
    }
    if package_field != "document_workspace" and document_workspace:
        document_workspace_payload = deepcopy(document_workspace)
        if include_raw_text:
            document_workspace_payload = _cap_source_trace_text(
                _cap_raw_text(document_workspace_payload),
                include_agent_markdown=True,
            )
        else:
            document_workspace_payload = _strip_raw_text(document_workspace_payload)
        output["document_workspace"] = document_workspace_payload
    if tool_name in {"retrieve_path_corridor", "inspect_path_corridor"}:
        output["route_trace"] = _as_dict(normalized.get("route_trace"))
    if include_answer_demo:
        output["answer_demo"] = _answer_demo(normalized)
    return output


def build_mcp_route_trace_output(
    *,
    search_id: str,
    trace: dict[str, Any],
    include_debug: bool = False,
) -> dict[str, Any]:
    session = _as_dict(trace.get("session"))
    result = _as_dict(trace.get("result"))
    planner_runtime = _as_dict(result.get("planner_runtime"))
    search_map_2d_truth = _as_dict(
        trace.get("search_map_2d_truth")
        or result.get("search_map_2d_truth")
        or planner_runtime.get("search_map_2d_truth")
    )
    events = [_as_dict(event) for event in _as_list(trace.get("events")) if isinstance(event, dict)]
    if not include_debug:
        events = [
            {
                "seq": event.get("seq"),
                "event_type": event.get("event_type"),
                "payload": _as_dict(event.get("payload")),
                "created_at": event.get("created_at"),
            }
            for event in events
        ]
    route_trace = {
        "schema_version": MCP_ROUTE_TRACE_SCHEMA_VERSION,
        "search_id": search_id,
        "thread_id": trace.get("thread_id") or session.get("thread_id"),
        "query_text": session.get("query_text"),
        "status": session.get("status"),
        "session": {
            "search_id": session.get("search_id") or search_id,
            "thread_id": session.get("thread_id"),
            "query_text": session.get("query_text"),
            "response_mode": session.get("response_mode"),
            "status": session.get("status"),
            "stop_reason": session.get("stop_reason"),
            "answerability_state": session.get("answerability_state"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
        },
        "events": events,
        "landing_metadata": _as_list(trace.get("landing_metadata")),
        "context_waves": _as_list(trace.get("context_waves")),
        "search_map_2d_truth": search_map_2d_truth,
        "worker_stop_reasons": _as_dict(trace.get("worker_stop_reasons")),
        "planner_metadata": _as_dict(trace.get("planner_metadata")),
        "blackboard": _as_dict(trace.get("blackboard")) if include_debug else {},
    }
    run_projection_truth = build_run_projection_truth(
        {
            **result,
            "search_id": search_id,
            "thread_id": trace.get("thread_id") or session.get("thread_id"),
            "search_map_2d_truth": search_map_2d_truth,
            "landing_metadata": _as_list(trace.get("landing_metadata")),
        },
        search_id=search_id,
        thread_id=trace.get("thread_id") or session.get("thread_id"),
        status=session.get("status"),
    )
    run_projection_event_stream_contract = _run_projection_event_stream_contract(
        {**result, "search_id": search_id},
        tool_name="inspect_route",
        status="ok" if events or session else "no_match",
        run_projection_truth=run_projection_truth,
        first_package_background_contract={},
    )
    return {
        "schema_version": MCP_RETRIEVAL_OUTPUT_SCHEMA_VERSION,
        "tool_name": "inspect_route",
        "search_id": search_id,
        "status": "ok" if events or session else "no_match",
        "route_trace": route_trace,
        "run_projection_truth": run_projection_truth,
        "run_projection_event_stream_contract": run_projection_event_stream_contract,
        "source_trace": [],
        "payload_integrity": _inspection_payload_integrity(
            tool_name="inspect_route",
            package_field="route_trace",
            package=route_trace,
        ),
        "completeness": {
            "search_id": search_id,
            "package_field": "route_trace",
            "package_present": bool(events or session),
            "event_count": len(events),
            "landing_count": len(_as_list(trace.get("landing_metadata"))),
            "search_map_2d_truth_present": bool(search_map_2d_truth),
            "context_wave_count": len(_as_list(trace.get("context_waves"))),
            "no_match": not bool(events or session),
        },
        "budget": {
            "timing": _as_dict(trace.get("timing")),
            "event_count": len(events),
            "landing_count": len(_as_list(trace.get("landing_metadata"))),
            "search_map_2d_truth_present": bool(search_map_2d_truth),
        },
    }


def build_mcp_memory_object_output(
    *,
    node_id: str,
    cluster: dict[str, Any] | None,
    include_debug: bool = False,
) -> dict[str, Any]:
    cluster_payload = _as_dict(cluster)
    memory_object = {
        "schema_version": MCP_MEMORY_OBJECT_SCHEMA_VERSION,
        "node_id": node_id,
        "focus_node_id": cluster_payload.get("focus_node_id") or node_id,
        "cluster_node_ids": _as_list(cluster_payload.get("cluster_node_ids")),
        "candidate_ids": _as_list(cluster_payload.get("candidate_ids")),
        "document_anchor_candidate_ids": _as_list(cluster_payload.get("document_anchor_candidate_ids")),
        "highway_expansion_ids": _as_list(cluster_payload.get("highway_expansion_ids")),
        "candidate_sources": _as_dict(cluster_payload.get("candidate_sources")),
        "debug_edges": _as_list(cluster_payload.get("debug_edges")) if include_debug else [],
    }
    return {
        "schema_version": MCP_RETRIEVAL_OUTPUT_SCHEMA_VERSION,
        "tool_name": "inspect_memory_object",
        "status": "ok" if cluster_payload else "no_match",
        "memory_object": memory_object,
        "source_trace": [],
        "payload_integrity": _inspection_payload_integrity(
            tool_name="inspect_memory_object",
            package_field="memory_object",
            package=memory_object,
        ),
        "completeness": {
            "node_id": node_id,
            "package_field": "memory_object",
            "package_present": bool(cluster_payload),
            "cluster_node_count": len(memory_object["cluster_node_ids"]),
            "candidate_count": len(memory_object["candidate_ids"]),
            "no_match": not bool(cluster_payload),
        },
        "budget": {
            "cluster_node_count": len(memory_object["cluster_node_ids"]),
            "candidate_count": len(memory_object["candidate_ids"]),
        },
    }
