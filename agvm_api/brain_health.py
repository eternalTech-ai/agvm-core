# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
import re
import time
from typing import Any, Callable

from brain_feedback_ledger import build_brain_feedback_ledger
from config import APP_NAME, APP_VERSION
from geometry_calibration import expected_brain_geometry_profile
from health_ai_diagnosis import build_health_ai_readonly_diagnosis
from storage import utc_timestamp


BRAIN_HEALTH_SCHEMA_VERSION = "agvm.brain_health_report.v1"
MCP_BRAIN_HEALTH_SCHEMA_VERSION = "agvm.mcp_brain_health_tool_output.v1"
BRAIN_SANITY_SNAPSHOT_SCHEMA_VERSION = "agvm.brain_sanity_snapshot.v1"
BRAIN_HEALTH_ALERT_SCHEMA_VERSION = "agvm.brain_health_alert.v1"
EVOLUTION_RECOMMENDATION_SCHEMA_VERSION = "agvm.evolution_recommendation.v1"
BENCHMARK_PREFLIGHT_SCHEMA_VERSION = "agvm.benchmark_preflight_gate.v1"
RETRIEVAL_LEARNING_ROLLUP_SCHEMA_VERSION = "agvm.retrieval_learning_rollup.v1"
VALIDATION_BRAIN_REBUILD_GATE_SCHEMA_VERSION = "agvm.validation_brain_rebuild_gate.v1"
METACOGNITIVE_OBSERVATION_ROLLUP_SCHEMA_VERSION = "agvm.metacognitive_observation_rollup.v1"

_CONTEXT_DEPENDENT_PREFIXES = (
    "it ",
    "this ",
    "that ",
    "these ",
    "those ",
    "he ",
    "she ",
    "they ",
    "his ",
    "her ",
    "their ",
    "esso ",
    "essa ",
    "questo ",
    "questa ",
    "lui ",
    "lei ",
    "loro ",
    "suo ",
    "sua ",
    "the monument ",
    "the company ",
    "the project ",
    "the document ",
    "the release ",
    "the source ",
)

_EVENT_TERMS = (
    "founded",
    "acquired",
    "created",
    "inaugurated",
    "announced",
    "launched",
    "joined",
    "sold",
    "fondato",
    "acquisito",
    "creato",
    "inaugurato",
    "annunciato",
    "lanciato",
    "venduto",
)

_RELATION_TERMS = (
    " is ",
    " was ",
    " founder",
    " founded ",
    " ceo",
    " chief executive",
    " acquired ",
    " linked ",
    " associated ",
    " connected ",
    " develops ",
    " built ",
    " created ",
    " e' ",
    " è ",
    " fond",
    " acquis",
    " colleg",
    " associ",
    " svilupp",
)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "").strip()


def _node_text(node: dict[str, Any]) -> str:
    return " ".join(
        str(node.get(key) or "").strip()
        for key in ("raw_text", "summary", "title")
        if str(node.get(key) or "").strip()
    ).strip()


def _node_position(node: dict[str, Any]) -> dict[str, float] | None:
    for key in ("final_position", "base_position", "position"):
        value = _as_dict(node.get(key))
        if {"x", "y", "z"} <= set(value):
            try:
                return {"x": float(value["x"]), "y": float(value["y"]), "z": float(value["z"])}
            except (TypeError, ValueError):
                return None
    direct = {axis: node.get(axis) for axis in ("x", "y", "z") if axis in node}
    if {"x", "y", "z"} <= set(direct):
        try:
            return {"x": float(direct["x"]), "y": float(direct["y"]), "z": float(direct["z"])}
        except (TypeError, ValueError):
            return None
    return None


def _subject_candidates(text: str, *, limit: int = 8) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,5}\b", text):
        value = " ".join(match.group(0).split())
        if len(value) < 3 or value.lower() in {"source uri", "visible text", "heading", "document", "page title"}:
            continue
        if value not in candidates:
            candidates.append(value)
        if len(candidates) >= limit:
            break
    return candidates


def _score_from_failure_ratio(ratio: float) -> float:
    return round(max(0.0, min(1.0, 1.0 - ratio)), 6)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_elapsed_ms(started_at: Any, ended_at: Any) -> float:
    start_text = str(started_at or "").strip()
    end_text = str(ended_at or "").strip()
    if not start_text or not end_text:
        return 0.0
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds() * 1000.0)
    except ValueError:
        return 0.0


def _iso_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _context_package_agent_markdown_chars(context_package: dict[str, Any]) -> int:
    package = _as_dict(context_package)
    metrics = _as_dict(package.get("metrics"))
    agent_markdown = str(package.get("agent_markdown") or "")
    return len(agent_markdown) or _safe_int(
        metrics.get("agent_markdown_char_count")
        or package.get("agent_markdown_char_count")
        or len(str(package.get("agent_markdown_preview") or ""))
    )


def _context_path_truth(context_package: dict[str, Any]) -> dict[str, Any]:
    package = _as_dict(context_package)
    contract = _as_dict(package.get("contract"))
    return _as_dict(contract.get("path_truth") or package.get("path_truth_contract"))


def _path_corridor_truth(result: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(result)
    path_corridors = _as_dict(payload.get("path_corridors") or payload.get("path_corridor_package"))
    metrics = _as_dict(path_corridors.get("metrics"))
    if not metrics:
        return {}
    planned_count = _safe_int(metrics.get("planned_path_count") or metrics.get("path_count"))
    path_count = _safe_int(metrics.get("path_count") or metrics.get("planned_path_count"))
    route_event_count = _safe_int(metrics.get("route_event_count"))
    completed_count = _safe_int(metrics.get("completed_path_count"))
    terminal_count = _safe_int(metrics.get("terminal_path_count"))
    pending_count = _safe_int(metrics.get("pending_path_count"))
    changed_count = _safe_int(metrics.get("changed_context_package_path_count"))
    ready = bool(
        path_count > 0
        and route_event_count > 0
        and (pending_count == 0 or terminal_count > 0 or completed_count >= max(1, planned_count))
    )
    if not ready and not any((planned_count, path_count, route_event_count, completed_count, terminal_count, pending_count)):
        return {}
    return {
        "schema_version": "agvm.health.path_corridor_truth.v1",
        "required": path_count > 0,
        "ready": ready,
        "state": "ready" if ready else "pending",
        "planned_path_count": planned_count,
        "path_count": path_count,
        "route_event_count": route_event_count,
        "changed_context_package_path_count": changed_count,
        "completed_path_count": completed_count,
        "terminal_path_count": terminal_count,
        "pending_path_count": pending_count,
        "all_planned_paths_accounted_for": bool(pending_count == 0 and path_count > 0),
        "source": "path_corridors.metrics",
    }


def _merge_path_truth_with_corridors(base_truth: dict[str, Any], corridor_truth: dict[str, Any]) -> dict[str, Any]:
    base = _as_dict(base_truth)
    corridor = _as_dict(corridor_truth)
    if not corridor:
        return base
    if not base:
        return corridor
    base_ready = _path_truth_ready(base)
    corridor_ready = _path_truth_ready(corridor)
    base_route_events = _safe_int(base.get("route_event_count") or base.get("travel") or base.get("route_steps"))
    corridor_route_events = _safe_int(corridor.get("route_event_count"))
    if corridor_ready and (not base_ready or corridor_route_events > base_route_events):
        merged = dict(base)
        merged.update({key: value for key, value in corridor.items() if value is not None})
        merged["required"] = bool(base.get("required") or corridor.get("required"))
        merged["source"] = "path_corridors.metrics_overrides_stale_delivery_truth"
        return merged
    return base


def _result_mcp_delivery_contract(result: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(result)
    return _as_dict(payload.get("mcp_delivery_contract") or payload.get("delivery_contract"))


def _result_context_materialization(result: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_as_dict(result).get("context_package_materialization"))


def _result_context_contract_passed(result: dict[str, Any]) -> bool:
    payload = _as_dict(result)
    delivery = _result_mcp_delivery_contract(payload)
    materialization = _result_context_materialization(payload)
    context_package = _as_dict(payload.get("context_package"))
    package_status = str(context_package.get("status") or "").strip().lower()
    client_state = str(delivery.get("client_payload_state") or "").strip().lower()
    completion_state = str(delivery.get("completion_state") or "").strip().lower()
    return bool(
        materialization.get("contract_passed")
        or materialization.get("terminal_for_mcp_client")
        or (
            client_state in {"usable_context", "final_context", "context_ready", "path_payload_ready"}
            and bool(delivery.get("terminal_for_client"))
        )
        or (package_status in {"contract_satisfied", "ready", "final_ready"} and completion_state in {"finalized", "completed"})
    )


def _document_ref_is_actionable(ref: dict[str, Any]) -> bool:
    payload = _as_dict(ref)
    raw = _as_dict(payload.get("raw_availability"))
    return bool(
        payload.get("retrieve_document_call")
        or payload.get("raw_text_available")
        or payload.get("raw_available")
        or raw.get("raw_text_available")
        or str(raw.get("state") or "").strip().lower() in {"raw_available", "raw_included"}
    )


def _result_document_delivery_satisfied(result: dict[str, Any]) -> bool:
    payload = _as_dict(result)
    delivery = _as_dict(payload.get("document_delivery_contract"))
    refs = [dict(ref) for ref in _as_list(payload.get("document_refs") or payload.get("docs")) if isinstance(ref, dict)]
    actionable_ref_count = _safe_int(delivery.get("actionable_document_ref_count"))
    raw_available_ref_count = _safe_int(delivery.get("raw_available_document_ref_count"))
    raw_included_count = _safe_int(delivery.get("raw_included_document_count"))
    bundle_document_count = _safe_int(delivery.get("document_bundle_document_count"))
    delivery_state = str(delivery.get("state") or delivery.get("document_bundle_state") or "").strip().lower()
    if delivery:
        has_actionable_delivery = bool(
            actionable_ref_count > 0
            or raw_available_ref_count > 0
            or raw_included_count > 0
            or bundle_document_count > 0
            or delivery.get("all_refs_actionable")
        )
        if has_actionable_delivery and delivery_state not in {"missing", "blocked", "failed", "not_ready"}:
            return True
    return any(_document_ref_is_actionable(ref) for ref in refs)


def _result_path_truth(result: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(result)
    delivery_truth = _as_dict(_result_mcp_delivery_contract(payload).get("path_truth"))
    corridor_truth = _path_corridor_truth(payload)
    if delivery_truth:
        return _merge_path_truth_with_corridors(delivery_truth, corridor_truth)
    context_truth = _context_path_truth(_as_dict(payload.get("context_package")))
    if context_truth:
        return _merge_path_truth_with_corridors(context_truth, corridor_truth)
    return corridor_truth


def _path_truth_ready(path_truth: dict[str, Any]) -> bool:
    truth = _as_dict(path_truth)
    if not truth:
        return False
    route_events = _safe_int(truth.get("route_event_count") or truth.get("travel") or truth.get("route_steps"))
    pending_paths = _safe_int(truth.get("pending_path_count") or truth.get("pending") or 0)
    accounted = bool(truth.get("all_planned_paths_accounted_for"))
    return bool(truth.get("ready")) or (route_events > 0 and (pending_paths == 0 or accounted))


def _result_has_ready_agent_payload(result: dict[str, Any]) -> bool:
    payload = _as_dict(result)
    if not payload:
        return False
    tool_status = str(payload.get("status") or "").strip().lower()
    if tool_status in {"failed", "blocked", "no_match"}:
        return False
    runtime_state = _as_dict(payload.get("runtime_state_contract"))
    operator_state = str(runtime_state.get("operator_state") or runtime_state.get("terminal_state") or "").strip().lower()
    if operator_state == "blocked":
        return False
    payload_state = str(runtime_state.get("payload_state") or runtime_state.get("completion_state") or "").strip().lower()
    first_payload = _as_dict(runtime_state.get("first_payload"))
    first_payload_chars = _safe_int(first_payload.get("char_count"))
    delivery = _result_mcp_delivery_contract(payload)
    context_package = _as_dict(payload.get("context_package"))
    context_chars = _context_package_agent_markdown_chars(context_package)
    ai_gate = _as_dict(payload.get("ai_materialization_hard_gate") or payload.get("ai_landing_materialization"))
    delivery_ai = _as_dict(delivery.get("ai"))
    package_ready = bool(ai_gate.get("mcp_context_package_ready")) or context_chars >= 600 or first_payload_chars >= 600
    if not package_ready and payload_state not in {"final_ready", "context_ready", "package_ready", "first_package_ready"}:
        return False

    ai_landing = _as_dict(payload.get("ai_landing_materialization"))
    ai_required = bool(
        ai_gate.get("required")
        or ai_gate.get("ai_required")
        or ai_landing.get("required")
        or delivery_ai.get("required")
    )
    ai_satisfied = bool(
        ai_gate.get("satisfied")
        or ai_gate.get("passed")
        or str(ai_gate.get("validation_state") or "").strip().lower() == "ai_materialization_validated"
        or bool(ai_landing.get("materialized"))
        or bool(delivery_ai.get("materialized"))
    )
    if ai_required and not ai_satisfied:
        return False

    path_truth = _result_path_truth(payload)
    path_required = bool(path_truth.get("required"))
    runtime_paths = _as_dict(runtime_state.get("paths"))
    runtime_path_state = str(runtime_paths.get("state") or "").strip().lower()
    if runtime_path_state == "blocked":
        return False
    runtime_pending = _safe_int(runtime_paths.get("pending"))
    runtime_completed = _safe_int(runtime_paths.get("completed"))
    runtime_stopped = _safe_int(runtime_paths.get("stopped"))
    runtime_planned = _safe_int(runtime_paths.get("planned"))
    runtime_accounted = bool(runtime_planned and runtime_completed + runtime_stopped >= runtime_planned and runtime_pending == 0)
    if path_required and not (_path_truth_ready(path_truth) or runtime_accounted):
        return False
    return True


def _issue_sample(node: dict[str, Any], failures: list[str]) -> dict[str, Any]:
    text = _node_text(node)
    return {
        "node_id": _node_id(node),
        "failures": list(failures),
        "text_preview": text[:180],
        "memory_type": node.get("memory_type"),
        "document_role": node.get("document_role"),
    }


def _analyze_node_atomicity(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    failures_by_node: list[dict[str, Any]] = []
    fragment_count = 0
    pronoun_fragment_count = 0
    temporal_fragment_count = 0
    too_short_count = 0
    for node in nodes:
        text = _node_text(node)
        folded = text.lower().strip()
        failures: list[str] = []
        if len(text) < 24:
            failures.append("node_text_too_short")
            too_short_count += 1
        if folded.startswith(_CONTEXT_DEPENDENT_PREFIXES) or folded.startswith(("- ", "* ", "...")):
            failures.append("context_dependent_or_fragmentary_opening")
            pronoun_fragment_count += 1
        has_event = any(term in folded for term in _EVENT_TERMS)
        if has_event and not _subject_candidates(text):
            failures.append("timeline_or_event_without_explicit_subject")
            temporal_fragment_count += 1
        if failures:
            fragment_count += 1
            if len(failures_by_node) < 24:
                failures_by_node.append(_issue_sample(node, failures))
    total = max(1, len(nodes))
    failure_ratio = fragment_count / total
    return {
        "score": _score_from_failure_ratio(failure_ratio),
        "node_count": len(nodes),
        "fragment_count": fragment_count,
        "fragment_ratio": round(failure_ratio, 6),
        "pronoun_or_ledger_fragment_count": pronoun_fragment_count,
        "temporal_fragment_count": temporal_fragment_count,
        "too_short_count": too_short_count,
        "issue_sample": failures_by_node,
    }


def _analyze_identity_explicitness(nodes: list[dict[str, Any]], identity_nucleus: dict[str, Any]) -> dict[str, Any]:
    core_name = str(identity_nucleus.get("core_name") or "").strip()
    name_candidates = [
        str(value).strip()
        for value in [core_name, *_as_list(identity_nucleus.get("self_name_candidates"))]
        if str(value).strip()
    ]
    folded_names = {value.lower() for value in name_candidates}
    identity_nodes: list[dict[str, Any]] = []
    explicit_relation_nodes: list[dict[str, Any]] = []
    for node in nodes:
        folded = _node_text(node).lower()
        memory_type = str(node.get("memory_type") or "").lower()
        has_name = bool(folded_names and any(name in folded for name in folded_names))
        if has_name or memory_type == "identity":
            identity_nodes.append(node)
        if has_name and any(term in folded for term in _RELATION_TERMS):
            explicit_relation_nodes.append(node)
    score = 0.0
    if core_name:
        score += 0.4
    score += min(len(identity_nodes) / max(1, max(8, int(len(nodes) * 0.01))), 1.0) * 0.3
    score += min(len(explicit_relation_nodes) / max(1, max(4, int(len(nodes) * 0.006))), 1.0) * 0.3
    return {
        "score": round(min(score, 1.0), 6),
        "core_name": core_name,
        "self_name_candidates": list(dict.fromkeys(name_candidates))[:12],
        "identity_node_count": len(identity_nodes),
        "explicit_identity_relation_count": len(explicit_relation_nodes),
        "identity_node_sample": [_node_id(node) for node in identity_nodes[:12]],
    }


def _analyze_source_coverage(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    source_linked = []
    source_label_count: Counter[str] = Counter()
    source_type_count: Counter[str] = Counter()
    source_unit_count = 0
    for node in nodes:
        provenance = _as_dict(node.get("provenance"))
        source_label = str(node.get("source_label") or provenance.get("source_label") or "").strip()
        source_type = str(node.get("source_type") or provenance.get("source_type") or "").strip()
        source_uri = str(node.get("source_uri") or provenance.get("source_uri") or "").strip()
        source_unit_id = str(node.get("source_unit_id") or "").strip()
        if source_unit_id:
            source_unit_count += 1
        if source_label or source_type or source_uri or source_unit_id or str(node.get("source_trust") or "").strip():
            source_linked.append(node)
        if source_label:
            source_label_count[source_label] += 1
        if source_type:
            source_type_count[source_type] += 1
    coverage_ratio = len(source_linked) / max(1, len(nodes))
    source_variety = len(source_label_count)
    score = round(min(coverage_ratio * 0.8 + min(source_variety / 8.0, 1.0) * 0.2, 1.0), 6)
    return {
        "score": score,
        "source_coverage_ratio": round(coverage_ratio, 6),
        "source_linked_node_count": len(source_linked),
        "source_unit_node_count": source_unit_count,
        "source_label_count": source_variety,
        "source_type_histogram": dict(source_type_count),
        "top_source_labels": dict(source_label_count.most_common(12)),
    }


def _link_target_id(link: Any) -> str:
    payload = _as_dict(link)
    return str(payload.get("target_node_id") or payload.get("target_id") or payload.get("id") or "").strip()


def _analyze_link_coherence(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    node_ids = {_node_id(node) for node in nodes if _node_id(node)}
    adjacency: dict[str, set[str]] = defaultdict(set)
    broken_edge_count = 0
    embedded_link_count = 0
    embedded_highway_count = 0
    broken_embedded_link_count = 0
    for edge in edges:
        source = str(edge.get("source_node_id") or edge.get("source") or "").strip()
        target = str(edge.get("target_node_id") or edge.get("target") or "").strip()
        if source in node_ids and target in node_ids:
            adjacency[source].add(target)
            adjacency[target].add(source)
        elif source or target:
            broken_edge_count += 1
    for node in nodes:
        source = _node_id(node)
        for kind, field in (("link", "links"), ("highway", "highways")):
            for link in _as_list(node.get(field)):
                target = _link_target_id(link)
                if not target:
                    continue
                if kind == "link":
                    embedded_link_count += 1
                else:
                    embedded_highway_count += 1
                if source in node_ids and target in node_ids:
                    adjacency[source].add(target)
                    adjacency[target].add(source)
                else:
                    broken_embedded_link_count += 1
    orphan_ids = sorted(node_id for node_id in node_ids if not adjacency.get(node_id))
    orphan_ratio = len(orphan_ids) / max(1, len(node_ids))
    broken_ratio = (broken_edge_count + broken_embedded_link_count) / max(1, len(edges) + embedded_link_count + embedded_highway_count)
    avg_degree = round(sum(len(value) for value in adjacency.values()) / max(1, len(node_ids)), 6)
    score = round(max(0.0, 1.0 - orphan_ratio * 0.75 - broken_ratio * 0.25), 6)
    return {
        "score": score,
        "graph_edge_count": len(edges),
        "embedded_link_count": embedded_link_count,
        "embedded_highway_count": embedded_highway_count,
        "broken_edge_count": broken_edge_count,
        "broken_embedded_link_count": broken_embedded_link_count,
        "orphan_node_count": len(orphan_ids),
        "orphan_ratio": round(orphan_ratio, 6),
        "avg_degree": avg_degree,
        "orphan_node_sample": orphan_ids[:24],
    }


def _analyze_document_retrievability(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    node_ids = {_node_id(node) for node in nodes if _node_id(node)}
    anchors: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    child_by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_anchor_children: list[str] = []
    for node in nodes:
        node_id = _node_id(node)
        role = str(node.get("document_role") or "").strip().lower()
        memory_type = str(node.get("memory_type") or "").strip().lower()
        is_anchor = bool(node.get("is_document_anchor")) or role == "anchor" or memory_type in {"document_anchor", "source_anchor"}
        if is_anchor:
            anchors.append(node)
        if role in {"chunk", "fact", "summary"} or memory_type in {"document_chunk", "document_fact", "document_summary"}:
            children.append(node)
            anchor_id = str(node.get("document_anchor_id") or node.get("derived_from_preview_id") or "").strip()
            if anchor_id and anchor_id in node_ids:
                child_by_anchor[anchor_id].append(node)
            else:
                missing_anchor_children.append(node_id)
    raw_ready = []
    for anchor in anchors:
        anchor_id = _node_id(anchor)
        text = _node_text(anchor)
        if len(text) >= 200 or child_by_anchor.get(anchor_id):
            raw_ready.append(anchor)
    child_anchor_failure_ratio = len(missing_anchor_children) / max(1, len(children))
    anchor_raw_ratio = len(raw_ready) / max(1, len(anchors))
    score = round(min(anchor_raw_ratio * 0.65 + (1.0 - child_anchor_failure_ratio) * 0.35, 1.0), 6)
    if not anchors:
        score = 0.0
    return {
        "score": score,
        "document_anchor_count": len(anchors),
        "document_child_count": len(children),
        "raw_ready_anchor_count": len(raw_ready),
        "anchor_raw_ready_ratio": round(anchor_raw_ratio, 6),
        "child_missing_anchor_count": len(missing_anchor_children),
        "child_anchor_failure_ratio": round(child_anchor_failure_ratio, 6),
        "missing_anchor_child_sample": missing_anchor_children[:24],
    }


def _analyze_radial_distribution(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    bucket_counts: Counter[str] = Counter()
    radial_bands: Counter[str] = Counter()
    semantic_zone_counts: Counter[str] = Counter()
    semantic_zone_in_band: Counter[str] = Counter()
    radii: list[float] = []
    missing_position_count = 0
    for node in nodes:
        profile = dict(expected_brain_geometry_profile(node))
        zone = str(profile.get("zone") or "unknown").strip() or "unknown"
        semantic_zone_counts[zone] += 1
        position = _node_position(node)
        if not position:
            missing_position_count += 1
            continue
        radius = math.sqrt(position["x"] ** 2 + position["y"] ** 2 + position["z"] ** 2)
        radii.append(radius)
        try:
            min_radius = float(profile.get("min_radius"))
            max_radius = float(profile.get("max_radius"))
        except (TypeError, ValueError):
            min_radius = 0.0
            max_radius = 1.0
        if min_radius <= radius <= max_radius:
            semantic_zone_in_band[zone] += 1
        if radius < 0.24:
            radial_bands["core"] += 1
        elif radius < 0.42:
            radial_bands["inner"] += 1
        elif radius < 0.68:
            radial_bands["middle"] += 1
        else:
            radial_bands["outer"] += 1
        bucket = _as_dict(node.get("bucket"))
        bucket_key = str(bucket.get("key") or node.get("coarse_bucket_key") or node.get("fine_bucket_key") or "").strip()
        if not bucket_key:
            bucket_key = f"{round(position['x'], 1)}:{round(position['y'], 1)}:{round(position['z'], 1)}"
        bucket_counts[bucket_key] += 1
    node_count = len(nodes)
    bucket_count = len(bucket_counts)
    radial_spread = (max(radii) - min(radii)) if radii else 0.0
    max_bucket_density = max(bucket_counts.values() or [0])
    overcrowded_threshold = max(80, int(max(1, node_count) * 0.05))
    overcrowded_count = sum(1 for count in bucket_counts.values() if count > overcrowded_threshold)
    missing_ratio = missing_position_count / max(1, node_count)
    bucket_score = min(bucket_count / max(12, min(64, max(12, int(math.sqrt(max(1, node_count)))))), 1.0)
    radial_score = min(radial_spread / 0.35, 1.0) if radii else 0.0
    dominant_semantic_zone_count = max(semantic_zone_counts.values() or [0])
    dominant_semantic_zone_ratio = dominant_semantic_zone_count / max(1, node_count)
    meaningful_zone_count = sum(1 for count in semantic_zone_counts.values() if count >= max(3, int(node_count * 0.005)))
    semantic_diversity_score = min(meaningful_zone_count / 5.0, 1.0)
    if node_count >= 50 and dominant_semantic_zone_ratio > 0.72:
        semantic_diversity_score *= max(0.0, 1.0 - ((dominant_semantic_zone_ratio - 0.72) / 0.28))
    semantic_radial_alignment_score = (
        sum(semantic_zone_in_band.values()) / max(1, len(radii))
        if node_count >= 50 and radii
        else 1.0
    )
    score = round(
        max(
            0.0,
            bucket_score * 0.25
            + radial_score * 0.25
            + semantic_diversity_score * 0.15
            + semantic_radial_alignment_score * 0.25
            + (1.0 - missing_ratio) * 0.1
            - min(overcrowded_count * 0.08, 0.3),
        ),
        6,
    )
    return {
        "score": score,
        "bucket_count": bucket_count,
        "max_bucket_density": max_bucket_density,
        "overcrowded_threshold": overcrowded_threshold,
        "overcrowded_bucket_count": overcrowded_count,
        "radial_spread": round(radial_spread, 6),
        "radial_bands": dict(radial_bands),
        "semantic_zone_counts": dict(semantic_zone_counts),
        "semantic_zone_count": len(semantic_zone_counts),
        "meaningful_semantic_zone_count": meaningful_zone_count,
        "dominant_semantic_zone_ratio": round(dominant_semantic_zone_ratio, 6),
        "semantic_diversity_score": round(semantic_diversity_score, 6),
        "semantic_radial_alignment_score": round(semantic_radial_alignment_score, 6),
        "semantic_zone_in_band_counts": dict(semantic_zone_in_band),
        "missing_position_count": missing_position_count,
        "missing_position_ratio": round(missing_ratio, 6),
    }


def _session_result(session: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(session.get("result"))


def _session_has_document_intent(session: dict[str, Any]) -> bool:
    query_text = str(session.get("query_text") or "").lower()
    response_mode = str(session.get("response_mode") or "").lower()
    request = _session_request(session)
    context_mode = str(request.get("context_package_mode") or "").lower()
    text_policy = str(request.get("document_text_policy") or "").lower()
    return bool(
        any(term in query_text for term in ("document", "documenti", "fonte", "fonti", "pdf", "source", "raw"))
        or response_mode == "document"
        or context_mode in {"document_full", "forensic_trace"}
        or text_policy in {"top_raw", "all_raw"}
    )


def _analyze_recent_retrieval_failures(recent_sessions: list[dict[str, Any]]) -> dict[str, Any]:
    status_histogram: Counter[str] = Counter()
    stop_histogram: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    failure_rows: list[dict[str, Any]] = []
    for session in recent_sessions:
        session_status = str(session.get("status") or "unknown").strip() or "unknown"
        status_histogram[session_status] += 1
        stop = str(session.get("stop_reason") or "unknown").strip() or "unknown"
        stop_histogram[stop] += 1
        result = _session_result(session)
        has_result = bool(result)
        tool_status = str(result.get("status") or "").strip()
        runtime_state = _as_dict(result.get("runtime_state_contract"))
        ai_gate = _as_dict(result.get("ai_materialization_hard_gate") or result.get("ai_landing_materialization"))
        context_package = _as_dict(result.get("context_package"))
        agent_markdown_chars = _context_package_agent_markdown_chars(context_package)
        ready_agent_payload = _result_has_ready_agent_payload(result)
        reasons: list[str] = []
        if session_status == "failed":
            reasons.append("session_failed")
        if has_result and tool_status in {"failed", "blocked", "no_match"}:
            reasons.append(f"tool_status_{tool_status}")
        if has_result and str(runtime_state.get("operator_state") or runtime_state.get("terminal_state") or "").lower() == "blocked":
            reasons.append("runtime_blocked")
        if has_result and str(ai_gate.get("state") or ai_gate.get("status") or "").lower() in {"blocked", "failed", "provider_degraded"}:
            reasons.append("ai_materialization_blocked_or_degraded")
        if has_result and agent_markdown_chars < 600 and tool_status in {"ok", "partial", ""}:
            reasons.append("thin_context_package")
        if stop in {"budget_exhausted", "search_timeout", "timeout"} and not ready_agent_payload:
            reasons.append(f"stop_{stop}")
        for reason in reasons:
            failure_reasons[reason] += 1
        if reasons and len(failure_rows) < 12:
            failure_rows.append(
                {
                    "search_id": session.get("search_id"),
                    "query_text": str(session.get("query_text") or "")[:160],
                    "status": session_status,
                    "tool_status": tool_status or None,
                    "stop_reason": stop,
                    "reasons": reasons,
                }
            )
    failure_count = sum(1 for session in recent_sessions if any(row.get("search_id") == session.get("search_id") for row in failure_rows))
    failure_ratio = sum(failure_reasons.values()) / max(1, len(recent_sessions) * 2)
    return {
        "score": _score_from_failure_ratio(min(failure_ratio, 1.0)),
        "session_count": len(recent_sessions),
        "status_histogram": dict(status_histogram),
        "stop_reason_histogram": dict(stop_histogram),
        "failure_signature_histogram": dict(failure_reasons),
        "failure_session_count": failure_count,
        "failure_sample": failure_rows,
    }


def _session_plan(session: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(session.get("plan"))


def _session_request(session: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(session.get("request"))


def _session_planner_runtime(session: dict[str, Any]) -> dict[str, Any]:
    plan = _session_plan(session)
    result = _session_result(session)
    plan_runtime = _as_dict(plan.get("planner_runtime")) or _as_dict(plan.get("runtime"))
    result_runtime = _as_dict(result.get("planner_runtime")) or _as_dict(result.get("runtime"))
    runtime = dict(plan_runtime)
    for key, value in result_runtime.items():
        if value is not None and value != "":
            runtime[key] = value
    result_semantic = _as_dict(result.get("semantic_contract_runtime"))
    plan_semantic = _as_dict(plan.get("semantic_contract_runtime"))
    semantic = dict(plan_semantic)
    for key, value in result_semantic.items():
        if value is not None and value != "":
            semantic[key] = value
    if semantic:
        runtime["semantic_contract_runtime"] = semantic
        if semantic.get("material") is not None:
            runtime["semantic_contract_material"] = semantic.get("material")
        if semantic.get("status"):
            runtime["semantic_contract_status"] = semantic.get("status")
        if semantic.get("ai_required") is not None:
            runtime["semantic_contract_ai_required"] = semantic.get("ai_required")
    return runtime


def _session_path_truth(session: dict[str, Any]) -> dict[str, Any]:
    result = _session_result(session)
    path_truth = _result_path_truth(result)
    if path_truth:
        return path_truth
    plan = _session_plan(session)
    truth = _as_dict(plan.get("search_map_2d_truth"))
    if truth:
        return truth
    return {}


def _session_ai_material(session: dict[str, Any]) -> tuple[bool, bool, str]:
    runtime = _session_planner_runtime(session)
    result = _session_result(session)
    semantic = _as_dict(runtime.get("semantic_contract_runtime") or _session_plan(session).get("semantic_contract_runtime"))
    delivery = _result_mcp_delivery_contract(result)
    delivery_ai = _as_dict(delivery.get("ai"))
    delivery_spatial = _as_dict(delivery.get("ai_spatial_landing_contract"))
    ai_spatial_runtime = _as_dict(result.get("ai_spatial_landing_contract_runtime"))
    ai_gate = _as_dict(result.get("ai_materialization_hard_gate") or result.get("ai_landing_materialization"))
    ai_required = bool(
        runtime.get("semantic_contract_ai_required")
        or semantic.get("ai_required")
        or semantic.get("enabled")
        or delivery_ai.get("required")
        or delivery_spatial.get("observed")
    )
    semantic_material = bool(
        runtime.get("semantic_contract_material")
        or semantic.get("material")
        or ai_gate.get("passed")
        or ai_gate.get("satisfied")
        or delivery_ai.get("materialized")
    )
    spatial_observed = bool(ai_spatial_runtime or delivery_spatial)
    spatial_material = bool(
        ai_spatial_runtime.get("material")
        or str(ai_spatial_runtime.get("status") or "").strip().lower() in {"completed", "materialized", "cache_hit", "cached_llm"}
        or delivery_spatial.get("materialized")
        or delivery_spatial.get("certifiable")
    )
    material = bool((semantic_material and (spatial_material or not spatial_observed)) or ai_gate.get("satisfied"))
    status = str(
        runtime.get("semantic_contract_status")
        or semantic.get("status")
        or ai_spatial_runtime.get("status")
        or delivery_spatial.get("status")
        or delivery_ai.get("critical_path_state")
        or ai_gate.get("status")
        or ""
    ).strip().lower()
    return ai_required, material, status


def _session_stop(session: dict[str, Any]) -> str:
    return str(session.get("stop_reason") or "").strip().lower()


def _stop_indicates_path_truth_satisfied(stop: str) -> bool:
    if not stop:
        return False
    return "path_truth_satisfied" in stop or "context_and_path_truth_satisfied" in stop


def _stop_indicates_nonblocking_first_payload(stop: str) -> bool:
    if not stop:
        return False
    if stop.startswith("context_package_ready_preserved"):
        return True
    if stop.startswith("mcp_first_package_") and (
        stop.endswith("_cap")
        or "satisfied" in stop
        or "returned" in stop
        or "ready" in stop
    ):
        return True
    if "first_package_ready" in stop and stop.endswith("_cap"):
        return True
    return stop in {
        "first_useful_mcp_package_returned_background_running",
        "context_package_ready_preserved",
        "contract_satisfied",
        "evidence_contract_satisfied",
        "high_confidence_direct_fact",
        "document_synthesis_packet_found",
        "broad_summary_coverage_sufficient",
    }


def _stop_indicates_budget_or_timeout(stop: str) -> bool:
    if not stop:
        return False
    if stop in {"budget_exhausted", "search_timeout", "timeout"}:
        return True
    return "timeout" in stop or "budget_exhausted" in stop


def _runtime_uses_heuristic_as_replacement(runtime: dict[str, Any], *, ai_required: bool, ai_material: bool) -> bool:
    planner_path = str(runtime.get("planner_path") or "").strip().lower()
    planner_kind = str(runtime.get("planner_kind") or "").strip().lower()
    heuristic_provisional = bool(runtime.get("heuristic_provisional"))
    if planner_path in {"heuristic", "heuristic_only", "fallback", "fallback_heuristic"}:
        return True
    if planner_kind in {"heuristic", "heuristic_only", "fallback", "fallback_heuristic"}:
        return True
    if planner_path.startswith("heuristic") and not planner_path.startswith("heuristic_support"):
        return True
    return heuristic_provisional and ai_required and not ai_material


def _session_success_families(session: dict[str, Any]) -> set[str]:
    families: set[str] = set()
    status = str(session.get("status") or "").strip().lower()
    if status not in {"completed", "running"}:
        return families
    result = _session_result(session)
    ready_agent_payload = _result_has_ready_agent_payload(result)
    if not ready_agent_payload:
        return families
    runtime = _session_planner_runtime(session)
    ai_required, ai_material, ai_status = _session_ai_material(session)
    bad_ai_status = ai_status in {"failed", "provider_degraded", "blocked", "timeout"}
    if _result_context_contract_passed(result):
        families.add("context_quality")
    if ai_required and ai_material and not bad_ai_status:
        families.add("ai_spatial")
    if ready_agent_payload and _result_context_contract_passed(result) and (not ai_required or ai_material):
        families.add("prompt_or_metamemory")
    if ai_material and not _runtime_uses_heuristic_as_replacement(runtime, ai_required=ai_required, ai_material=ai_material):
        families.add("heuristic_learning")
    request = _session_request(session)
    path_truth = _session_path_truth(session)
    path_required = bool(path_truth.get("required") or request.get("complete_paths"))
    if path_required and _path_truth_ready(path_truth):
        families.add("route_truth")
    stop = _session_stop(session)
    elapsed_ms = _iso_elapsed_ms(session.get("created_at"), session.get("updated_at"))
    result_surface_ms = _safe_int(result.get("result_surface_ready_ms") or result.get("first_context_ms") or 0)
    if (
        _stop_indicates_nonblocking_first_payload(stop)
        or _stop_indicates_path_truth_satisfied(stop)
        or 0 < result_surface_ms <= 30000
        or (elapsed_ms and elapsed_ms <= 30000)
    ):
        families.add("latency_stream")
    if _session_has_document_intent(session) and _result_document_delivery_satisfied(result):
        families.add("source_document")
    return families


def _session_signal_families(session: dict[str, Any]) -> tuple[set[str], list[str]]:
    families: set[str] = set()
    reasons: list[str] = []
    status = str(session.get("status") or "").strip().lower()
    stop = _session_stop(session)
    stop_path_satisfied = _stop_indicates_path_truth_satisfied(stop)
    stop_first_payload_satisfied = _stop_indicates_nonblocking_first_payload(stop)
    stop_budget_or_timeout = _stop_indicates_budget_or_timeout(stop)
    answerability = str(session.get("answerability_state") or "").strip().lower()
    request = _session_request(session)
    runtime = _session_planner_runtime(session)
    result = _session_result(session)
    tool_status = str(result.get("status") or "").strip().lower()
    elapsed_ms = _iso_elapsed_ms(session.get("created_at"), session.get("updated_at"))
    result_json_length = _safe_int(session.get("result_json_length"))
    ready_agent_payload = _result_has_ready_agent_payload(result)

    if status in {"failed", "blocked"} or tool_status in {"failed", "blocked", "no_match"}:
        families.add("context_quality")
        reasons.append("context_quality:session_or_tool_blocked")
    context_contract_passed = _result_context_contract_passed(result)
    if answerability in {"partial", "missing", "ungrounded", "blocked"} and not (ready_agent_payload and context_contract_passed):
        families.add("context_quality")
        reasons.append("context_quality:answerability_not_grounded")
    if stop_budget_or_timeout and not ready_agent_payload:
        families.add("latency_stream")
        reasons.append(f"latency_stream:stop_{stop}")
    if elapsed_ms > 30000 and not stop_first_payload_satisfied and not ready_agent_payload:
        families.add("latency_stream")
        reasons.append("latency_stream:first_or_final_runtime_over_slo")
    if 0 < result_json_length < 800:
        families.add("context_quality")
        reasons.append("context_quality:thin_result_payload")

    complete_paths = bool(request.get("complete_paths"))
    path_truth = _session_path_truth(session)
    path_required = bool(path_truth.get("required") or complete_paths)
    route_events = _safe_int(path_truth.get("route_event_count") or path_truth.get("travel") or path_truth.get("route_steps"))
    pending_paths = _safe_int(path_truth.get("pending_path_count") or path_truth.get("pending") or 0)
    path_ready = _path_truth_ready(path_truth) or route_events > 0
    if stop_path_satisfied:
        path_ready = True
        pending_paths = 0
    first_payload_still_running = status == "running" and stop_first_payload_satisfied
    path_accounted = bool(path_truth.get("all_planned_paths_accounted_for"))
    if path_required and (not path_ready or (pending_paths > 0 and not path_accounted)) and not first_payload_still_running:
        families.add("route_truth")
        reasons.append("route_truth:path_required_not_fully_traversed")

    ai_required, ai_material, ai_status = _session_ai_material(session)
    if ai_required and not ai_material:
        families.add("ai_spatial")
        reasons.append("ai_spatial:semantic_or_spatial_ai_not_materialized")
    if ai_status in {"failed", "provider_degraded", "blocked", "timeout"} and not ai_material:
        families.add("ai_spatial")
        reasons.append(f"ai_spatial:semantic_ai_{ai_status}")
    if _runtime_uses_heuristic_as_replacement(runtime, ai_required=ai_required, ai_material=ai_material):
        families.add("heuristic_learning")
        reasons.append("heuristic_learning:heuristic_route_replaced_ai_material")

    mission_learning = _as_dict(result.get("mission_learning_rollup") or runtime.get("mission_learning_rollup"))
    mission_family_counts = _as_dict(mission_learning.get("family_counts"))
    mission_reason_counts = _as_dict(mission_learning.get("reason_counts"))
    delivery = _result_mcp_delivery_contract(result)
    client_payload_state = str(delivery.get("client_payload_state") or "").strip().lower()
    completion_state = str(delivery.get("completion_state") or "").strip().lower()
    terminal_for_client = bool(delivery.get("terminal_for_client")) or completion_state in {"finalized", "completed"}
    prompt_gap_contained_by_client_payload = bool(
        ready_agent_payload
        and context_contract_passed
        and terminal_for_client
        and (not ai_required or ai_material)
        and client_payload_state in {"usable_context", "final_context", "context_ready", "path_payload_ready", "no_match"}
    )
    for family, raw_count in mission_family_counts.items():
        if _safe_int(raw_count) <= 0:
            continue
        family_name = str(family or "").strip()
        if not family_name:
            continue
        if family_name == "prompt_or_metamemory" and prompt_gap_contained_by_client_payload:
            continue
        families.add(family_name)
    for reason, raw_count in mission_reason_counts.items():
        if _safe_int(raw_count) <= 0:
            continue
        reason_text = str(reason or "").strip()
        if reason_text:
            if reason_text.startswith("prompt_or_metamemory:") and prompt_gap_contained_by_client_payload:
                continue
            reasons.append(reason_text)

    if _session_has_document_intent(session):
        if not _result_document_delivery_satisfied(result) and (status != "completed" or answerability in {"partial", "missing", "blocked"}):
            families.add("source_document")
            reasons.append("source_document:document_intent_not_fully_satisfied")
    return families, reasons


def _recommendation_for_family(family: str) -> str:
    if family in {"route_truth", "ai_spatial", "heuristic_learning"}:
        return "matrix_calibration_preview"
    if family in {"geometry_or_matrix", "prompt_or_metamemory"}:
        return "matrix_calibration_preview"
    if family in {"context_quality", "link_coherence"}:
        return "evolve_preview"
    if family in {"node_shape_or_link", "provider_or_latency"}:
        return "evolve_preview"
    if family == "hot_cold_policy":
        return "sleep_preview"
    if family in {"source_document", "grow_or_source", "document_delivery"}:
        return "grow_repair"
    if family == "benchmark_policy":
        return "inspect_context_package"
    if family == "latency_stream":
        return "evolve_preview"
    return "evolve_preview"


def _maintenance_preview_modes_for_family(family: str) -> set[str]:
    recommendation = _recommendation_for_family(family)
    if recommendation == "matrix_calibration_preview":
        return {"matrix_calibration"}
    if recommendation == "sleep_preview":
        return {"sleep", "sleep_evolve"}
    if recommendation == "evolve_preview":
        return {"evolve", "sleep_evolve"}
    return set()


def _analyze_retrieval_learning_rollup(
    recent_sessions: list[dict[str, Any]],
    *,
    calibration_snapshot: dict[str, Any] | None = None,
    recent_maintenance_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_family_counts: Counter[str] = Counter()
    success_family_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    evidence_rows: list[dict[str, Any]] = []
    success_rows: list[dict[str, Any]] = []
    newest_failure_index: dict[str, int] = {}
    newest_success_index: dict[str, int] = {}
    newest_failure_epoch: dict[str, float] = {}
    for index, session in enumerate(recent_sessions):
        families, reasons = _session_signal_families(session)
        session_epoch = _iso_epoch(session.get("updated_at") or session.get("created_at"))
        for family in families:
            raw_family_counts[family] += 1
            newest_failure_index.setdefault(family, index)
            newest_failure_epoch.setdefault(family, session_epoch)
        for reason in reasons:
            reason_counts[reason] += 1
        if families and len(evidence_rows) < 16:
            evidence_rows.append(
                {
                    "search_id": session.get("search_id"),
                    "query_text": str(session.get("query_text") or "")[:160],
                    "status": session.get("status"),
                    "answerability_state": session.get("answerability_state"),
                    "stop_reason": session.get("stop_reason"),
                    "families": sorted(families),
                    "reasons": list(dict.fromkeys(reasons))[:10],
                }
            )
        success_families = _session_success_families(session)
        for family in success_families:
            success_family_counts[family] += 1
            newest_success_index.setdefault(family, index)
        if success_families and len(success_rows) < 12:
            success_rows.append(
                {
                    "search_id": session.get("search_id"),
                    "query_text": str(session.get("query_text") or "")[:160],
                    "status": session.get("status"),
                    "answerability_state": session.get("answerability_state"),
                    "stop_reason": session.get("stop_reason"),
                    "families": sorted(success_families),
                }
            )

    calibration = _as_dict(calibration_snapshot)
    failure_signatures = _as_dict(calibration.get("failure_signatures"))
    spatial_priors = _as_dict(calibration.get("spatial_correction_priors"))
    calibration_failure_signature_count = len(failure_signatures)
    calibration_failure_signatures_contributed = False
    if failure_signatures:
        reason_counts["heuristic_learning:historical_failure_signatures_observed"] += calibration_failure_signature_count
        if raw_family_counts.get("heuristic_learning", 0) > 0:
            contribution = min(2, calibration_failure_signature_count)
            raw_family_counts["heuristic_learning"] += contribution
            reason_counts["heuristic_learning:failure_signatures_reproduced_by_recent_sessions"] += contribution
            calibration_failure_signatures_contributed = True
    if spatial_priors:
        pending_count = sum(
            1
            for payload in spatial_priors.values()
            if bool(_as_dict(_as_dict(payload).get("review")).get("required", True))
            or str(_as_dict(payload).get("status") or "") == "review_candidate"
        )
        if pending_count:
            reason_counts["heuristic_learning:spatial_correction_priors_review_pending"] += pending_count

    family_counts: Counter[str] = Counter(raw_family_counts)
    resolved_families: list[str] = []
    resolved_reason_codes: list[str] = []
    for family, count in sorted(raw_family_counts.items()):
        success_index = newest_success_index.get(family)
        failure_index = newest_failure_index.get(family)
        if success_index is None or failure_index is None:
            continue
        if success_index < failure_index and success_family_counts.get(family, 0) > 0:
            family_counts.pop(family, None)
            resolved_families.append(family)
            resolved_reason_codes.append(f"watch:retrieval_learning_{family}_resolved_after_recent_success")

    preview_resolved_families: list[str] = []
    preview_resolution_rows: list[dict[str, Any]] = []
    maintenance_runs = [dict(run) for run in _as_list(recent_maintenance_runs) if isinstance(run, dict)]
    for family in sorted(list(family_counts)):
        failure_epoch = newest_failure_epoch.get(family, 0.0)
        allowed_modes = _maintenance_preview_modes_for_family(family)
        if not allowed_modes or failure_epoch <= 0.0:
            continue
        latest_preview = next(
            (
                run
                for run in maintenance_runs
                if bool(run.get("preview_only"))
                and str(run.get("mode") or "") in allowed_modes
                and _iso_epoch(run.get("created_at")) >= failure_epoch
            ),
            None,
        )
        if not latest_preview:
            continue
        family_counts.pop(family, None)
        preview_resolved_families.append(family)
        resolved_reason_codes.append(f"watch:retrieval_learning_{family}_previewed_after_recent_failure")
        preview_resolution_rows.append(
            {
                "family": family,
                "maintenance_id": latest_preview.get("maintenance_id"),
                "mode": latest_preview.get("mode"),
                "preview_only": bool(latest_preview.get("preview_only")),
                "created_at": latest_preview.get("created_at"),
                "failure_epoch": failure_epoch,
                "policy": "preview_receipt_downgrades_until_preview_gate_but_does_not_claim_problem_fixed",
            }
        )

    repeated_families = sorted(family for family, count in family_counts.items() if count >= 2)
    isolated_families = sorted(family for family, count in family_counts.items() if count == 1)
    severity_by_family: dict[str, str] = {}
    recommendation_hints: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    for family, count in sorted(family_counts.items()):
        if count >= 4:
            severity = "action_recommended"
        elif count >= 2:
            severity = "action_recommended"
        else:
            severity = "watch"
        severity_by_family[family] = severity
        recommendation = _recommendation_for_family(family)
        if severity == "action_recommended":
            reason_code = f"{recommendation}:retrieval_learning_{family}_repeated"
            reason_codes.append(reason_code)
            recommendation_hints.append(
                {
                    "family": family,
                    "count": count,
                    "severity": severity,
                    "recommendation": recommendation,
                    "reason_code": reason_code,
                    "endpoint_hint": _endpoint_hint(recommendation),
                }
            )
        else:
            reason_codes.append(f"watch:retrieval_learning_{family}_isolated")
    reason_codes.extend(resolved_reason_codes)

    signal_count = sum(family_counts.values())
    raw_signal_count = sum(raw_family_counts.values())
    action_family_count = sum(1 for severity in severity_by_family.values() if severity == "action_recommended")
    score = _score_from_failure_ratio(min(1.0, signal_count / max(1, len(recent_sessions) * 4)))
    return {
        "schema_version": RETRIEVAL_LEARNING_ROLLUP_SCHEMA_VERSION,
        "session_count": len(recent_sessions),
        "signal_count": signal_count,
        "raw_signal_count": raw_signal_count,
        "score": score,
        "family_counts": dict(family_counts),
        "raw_family_counts": dict(raw_family_counts),
        "success_family_counts": dict(success_family_counts),
        "reason_counts": dict(reason_counts),
        "repeated_signal_families": repeated_families,
        "isolated_signal_families": isolated_families,
        "resolved_signal_families": resolved_families,
        "preview_resolved_signal_families": preview_resolved_families,
        "severity_by_family": severity_by_family,
        "recommendation_hints": recommendation_hints,
        "reason_codes": reason_codes,
        "action_recommended_family_count": action_family_count,
        "calibration_evidence": {
            "failure_signature_count": calibration_failure_signature_count,
            "failure_signatures_contributed_to_alerts": calibration_failure_signatures_contributed,
            "spatial_correction_pending_count": _safe_int(reason_counts.get("heuristic_learning:spatial_correction_priors_review_pending")),
        },
        "debounce_policy": {
            "single_signal_state": "watch",
            "repeated_signal_threshold": 2,
            "structural_action_requires_repeated_evidence": True,
            "newer_success_can_resolve_stale_repeated_evidence": True,
            "hidden_mutation_allowed": False,
        },
        "evidence_sample": evidence_rows,
        "success_sample": success_rows,
        "preview_resolution_sample": preview_resolution_rows[:12],
    }


def _recommendation_for_metacognitive_kind(kind: str, tool_hint: str) -> str:
    normalized_kind = str(kind or "").strip()
    normalized_tool = str(tool_hint or "").strip()
    if normalized_tool in {"matrix_calibration_preview", "grow_source_preview", "sleep_preview", "evolve_preview"}:
        if normalized_tool == "grow_source_preview":
            return "grow_repair"
        return normalized_tool
    if normalized_kind in {"wrong_landing_region", "overpacked_region"}:
        return "matrix_calibration_preview"
    if normalized_kind in {"node_missing_source_reference", "source_reingest_needed", "node_source_boilerplate"}:
        return "grow_repair"
    if normalized_kind in {"bad_hot_context", "underconnected_region"}:
        return "evolve_preview"
    return "sleep_preview"


def _analyze_metacognitive_observations(recent_memory_learning_events: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = [dict(event) for event in _as_list(recent_memory_learning_events) if isinstance(event, dict)]
    kind_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    recommendation_counts: Counter[str] = Counter()
    affected_node_counts: Counter[str] = Counter()
    evidence_sample: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    severity_weight = {"info": 0.4, "watch": 0.65, "warning": 0.9, "blocking": 1.0}
    weighted_signal = 0.0

    for event in rows:
        if str(event.get("event_kind") or "") != "query_quality_observation_created":
            continue
        payload = _as_dict(event.get("payload"))
        observation = _as_dict(payload.get("metacognitive_observation") or payload.get("observation"))
        if not observation:
            continue
        kind = str(observation.get("observation_kind") or "unknown").strip() or "unknown"
        severity = str(observation.get("severity") or "watch").strip() or "watch"
        recommendation = _recommendation_for_metacognitive_kind(kind, str(observation.get("recommended_tool") or ""))
        kind_counts[kind] += 1
        severity_counts[severity] += 1
        recommendation_counts[recommendation] += 1
        weighted_signal += severity_weight.get(severity, 0.65)
        for node_id in _as_list(observation.get("affected_node_ids")):
            node_text = str(node_id or "").strip()
            if node_text:
                affected_node_counts[node_text] += 1
        if len(evidence_sample) < 16:
            evidence_sample.append(
                {
                    "event_id": event.get("event_id"),
                    "operation_id": event.get("operation_id"),
                    "observation_kind": kind,
                    "severity": severity,
                    "recommended_action": recommendation,
                    "symptom": str(observation.get("symptom") or "")[:220],
                    "affected_node_ids": list(observation.get("affected_node_ids") or [])[:8],
                }
            )

    for kind, count in sorted(kind_counts.items()):
        representative_event = next(
            (
                event
                for event in rows
                if str(_as_dict(_as_dict(event.get("payload")).get("metacognitive_observation")).get("observation_kind") or "") == kind
            ),
            {},
        )
        representative_observation = _as_dict(_as_dict(representative_event.get("payload")).get("metacognitive_observation"))
        recommendation = _recommendation_for_metacognitive_kind(kind, str(representative_observation.get("recommended_tool") or ""))
        if count >= 2:
            reason_codes.append(f"{recommendation}:query_metacognition_{kind}_repeated")
        else:
            reason_codes.append(f"watch:query_metacognition_{kind}_isolated")

    observation_count = sum(kind_counts.values())
    score = _score_from_failure_ratio(min(1.0, weighted_signal / max(1.0, observation_count * 4.0)))
    return {
        "schema_version": METACOGNITIVE_OBSERVATION_ROLLUP_SCHEMA_VERSION,
        "event_count": len(rows),
        "observation_count": observation_count,
        "score": score,
        "kind_counts": dict(kind_counts),
        "severity_counts": dict(severity_counts),
        "recommendation_counts": dict(recommendation_counts),
        "repeated_observation_kinds": sorted(kind for kind, count in kind_counts.items() if count >= 2),
        "affected_node_hotspots": [
            {"node_id": node_id, "observation_count": count}
            for node_id, count in affected_node_counts.most_common(12)
        ],
        "reason_codes": reason_codes,
        "evidence_sample": evidence_sample,
        "maintenance_contract": {
            "consumed_by_sleep": True,
            "consumed_by_evolve": True,
            "consumed_by_matrix_calibration": True,
            "hidden_mutation_allowed": False,
            "structural_apply_requires_preview": True,
        },
    }


def _analyze_metamemory(
    *,
    metamemory: dict[str, Any] | None,
    metamemory_spatial_brief: dict[str, Any] | None = None,
    calibration_snapshot: dict[str, Any] | None,
    recent_maintenance_runs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    calibration = _as_dict(calibration_snapshot)
    compiled_priors = _as_dict(calibration.get("compiled_priors"))
    failure_signatures = _as_dict(calibration.get("failure_signatures"))
    spatial_correction_priors = _as_dict(calibration.get("spatial_correction_priors"))
    spatial_review_count = sum(
        1
        for payload in spatial_correction_priors.values()
        if bool(_as_dict(_as_dict(payload).get("review")).get("required", True))
        or str(_as_dict(payload).get("status") or "") == "review_candidate"
    )
    meta = _as_dict(metamemory)
    spatial_brief = _as_dict(metamemory_spatial_brief)
    spatial_readiness = _as_dict(spatial_brief.get("spatial_readiness_contract"))
    topology_overlay = _as_dict(spatial_brief.get("topology_overlay_summary"))
    base_matrix = _as_dict(spatial_brief.get("base_matrix_summary"))
    runs = [dict(run) for run in _as_list(recent_maintenance_runs) if isinstance(run, dict)]
    mode_histogram = Counter(str(run.get("mode") or "unknown") for run in runs)
    applied_count = sum(1 for run in runs if bool(run.get("applied")))
    preview_count = sum(1 for run in runs if bool(run.get("preview_only")))
    return {
        "score": round(min((len(compiled_priors) + len(failure_signatures) + len(spatial_correction_priors) + len(runs)) / 16.0, 1.0), 6),
        "metamemory_snapshot_present": bool(metamemory),
        "metamemory_schema_version": meta.get("schema_version"),
        "metamemory_revision": meta.get("snapshot_version"),
        "metamemory_hash": meta.get("hash"),
        "metamemory_source_kinds": list(meta.get("source_kinds") or []),
        "metamemory_spatial_brief_exists": bool(meta.get("spatial_brief_exists")),
        "metamemory_spatial_brief_runtime_present": bool(spatial_brief),
        "metamemory_spatial_readiness": spatial_readiness,
        "metamemory_spatial_certifiable": bool(spatial_readiness.get("certifiable")) if spatial_readiness else None,
        "brain_revision": spatial_brief.get("brain_revision"),
        "matrix_revision": spatial_brief.get("matrix_revision") or base_matrix.get("matrix_revision"),
        "topology_revision": spatial_brief.get("topology_revision") or topology_overlay.get("topology_revision"),
        "atlas_revision": spatial_brief.get("atlas_revision") or _as_dict(spatial_brief.get("atlas_summary")).get("atlas_revision"),
        "calibration_revision": spatial_brief.get("calibration_revision"),
        "source_replay_revision": spatial_brief.get("source_replay_revision"),
        "topology_overlay_present": bool(topology_overlay.get("overlay_present")),
        "topology_density_lobe_count": len(_as_list(topology_overlay.get("density_lobes"))),
        "topology_active_highway_count": len(_as_list(topology_overlay.get("active_highways"))),
        "topology_pending_maintenance_proposal_count": len(_as_list(topology_overlay.get("pending_maintenance_proposals"))),
        "metamemory_section_count": int(meta.get("sections") or 0),
        "calibration_event_count": int(calibration.get("event_count") or 0),
        "compiled_prior_count": len(compiled_priors),
        "failure_signature_count": len(failure_signatures),
        "spatial_correction_prior_count": len(spatial_correction_priors),
        "spatial_correction_review_candidate_count": spatial_review_count,
        "recent_maintenance_run_count": len(runs),
        "recent_maintenance_mode_histogram": dict(mode_histogram),
        "recent_maintenance_preview_count": preview_count,
        "recent_maintenance_applied_count": applied_count,
    }


def _primary_recommendation(reason_codes: list[str]) -> str:
    priority = [
        "rebuild_required",
        "grow_repair",
        "matrix_calibration_preview",
        "evolve_preview",
        "sleep_preview",
        "inspect_context_package",
    ]
    for recommendation in priority:
        if any(code.startswith(f"{recommendation}:") or code == recommendation for code in reason_codes):
            return recommendation
    return "none"


def _spatial_prior_resolution_status(
    *,
    metamemory_health: dict[str, Any],
    radial_distribution: dict[str, Any],
    retrieval_learning_rollup: dict[str, Any],
    recent_maintenance_runs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    review_count = _safe_int(metamemory_health.get("spatial_correction_review_candidate_count"))
    runs = [dict(run) for run in _as_list(recent_maintenance_runs) if isinstance(run, dict)]
    latest_matrix_apply = next(
        (
            run
            for run in runs
            if str(run.get("mode") or "") == "matrix_calibration"
            and bool(run.get("applied"))
            and not bool(run.get("preview_only"))
        ),
        None,
    )
    latest_matrix_preview = next(
        (
            run
            for run in runs
            if str(run.get("mode") or "") == "matrix_calibration"
            and bool(run.get("preview_only"))
            and not bool(run.get("applied"))
        ),
        None,
    )
    geometry_green = (
        float(radial_distribution.get("score") or 0.0) >= 0.75
        and float(radial_distribution.get("semantic_diversity_score") or 0.0) >= 0.75
        and float(radial_distribution.get("semantic_radial_alignment_score") or 1.0) >= 0.70
    )
    active_learning_signals = bool(
        _safe_int(retrieval_learning_rollup.get("action_recommended_family_count"))
        or _as_list(retrieval_learning_rollup.get("repeated_signal_families"))
    )
    resolved_by_apply = bool(review_count > 0 and latest_matrix_apply and geometry_green and not active_learning_signals)
    resolved_by_preview = bool(review_count > 0 and latest_matrix_preview and geometry_green and not active_learning_signals)
    resolved = bool(resolved_by_apply or resolved_by_preview)
    return {
        "schema_version": "agvm.spatial_prior_resolution_status.v1",
        "review_candidate_count": review_count,
        "resolved_by_recent_matrix_apply": resolved_by_apply,
        "resolved_by_recent_matrix_preview": resolved_by_preview,
        "resolution_basis": (
            "recent_matrix_calibration_apply_plus_green_current_geometry_and_no_active_retrieval_learning_signals"
            if resolved_by_apply
            else "recent_matrix_calibration_preview_plus_green_current_geometry_and_no_active_retrieval_learning_signals"
            if resolved_by_preview
            else "unresolved_or_not_enough_evidence"
        ),
        "latest_matrix_apply": {
            "maintenance_id": latest_matrix_apply.get("maintenance_id"),
            "created_at": latest_matrix_apply.get("created_at"),
        }
        if latest_matrix_apply
        else {},
        "latest_matrix_preview": {
            "maintenance_id": latest_matrix_preview.get("maintenance_id"),
            "created_at": latest_matrix_preview.get("created_at"),
        }
        if latest_matrix_preview
        else {},
        "current_geometry_green": geometry_green,
        "active_retrieval_learning_signals": active_learning_signals,
        "hidden_mutation_allowed": False,
    }


def _stable_alert_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return f"brain_health_alert::{hashlib.sha256(encoded).hexdigest()[:16]}"


def _reason_family(reason_code: str) -> str:
    text = str(reason_code or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    if "identity" in text:
        return "identity_nucleus"
    if "geometry_or_matrix" in text:
        return "geometry_or_matrix"
    if "prompt_or_metamemory" in text:
        return "prompt_or_metamemory"
    if "node_shape_or_link" in text:
        return "node_shape_or_link"
    if "hot_cold_policy" in text:
        return "hot_cold_policy"
    if "provider_or_latency" in text:
        return "provider_or_latency"
    if "benchmark_policy" in text:
        return "benchmark_policy"
    if "grow_or_source" in text:
        return "grow_or_source"
    if "document_delivery" in text:
        return "document_delivery"
    if "document" in text or "source" in text:
        return "source_document"
    if "link" in text or "orphan" in text:
        return "link_coherence"
    if "route_truth" in text or "path" in text or "corridor" in text:
        return "route_truth"
    if "ai_spatial" in text or "semantic_ai" in text:
        return "ai_spatial"
    if "heuristic" in text or "correction" in text:
        return "heuristic_learning"
    if "source_document" in text or "document" in text or "source" in text:
        return "source_document"
    if "latency" in text or "stream" in text or "timeout" in text:
        return "latency_stream"
    if "radial" in text or "bucket" in text or "semantic_zone" in text or "spatial" in text:
        return "geometry_matrix"
    if "retrieval" in text or "context" in text:
        return "context_quality"
    if "node" in text or "atomicity" in text:
        return "node_shape"
    return "maintenance_lifecycle"


def _reason_severity(reason_code: str, recommendation: str) -> str:
    code = str(reason_code or "")
    if code.startswith("rebuild_required:") or recommendation == "rebuild_required":
        return "rebuild_required"
    if code.startswith("grow_repair:"):
        return "blocking"
    if code.startswith(("matrix_calibration_preview:", "evolve_preview:", "sleep_preview:")):
        return "action_recommended"
    return "watch"


def _endpoint_hint(recommendation: str) -> str | None:
    return {
        "sleep_preview": "/mcp/sleep-preview",
        "evolve_preview": "/mcp/evolve-preview",
        "matrix_calibration_preview": "/mcp/matrix-calibration-preview",
        "grow_repair": "/mcp/grow-source-preview",
        "inspect_context_package": "/mcp/inspect-context-package",
        "rebuild_required": "guarded validation-brain reset/replay plan",
        "none": None,
    }.get(str(recommendation or "none"))


def _product_gate_impact(severity: str) -> str:
    if severity == "rebuild_required":
        return "blocks_product_and_revolutionary_certification"
    if severity == "blocking":
        return "blocks_benchmark_certification_until_preview_or_repair"
    if severity == "action_recommended":
        return "requires_preview_before_serious_benchmark"
    if severity == "watch":
        return "benchmark_allowed_with_warning"
    return "informational"


def _build_health_alerts(
    *,
    brain_id: str | None,
    brain_revision: str,
    reason_codes: list[str],
    recommendation: str,
    generated_at: str,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for code in reason_codes:
        reason_code = str(code or "").strip()
        if not reason_code:
            continue
        local_recommendation = reason_code.split(":", 1)[0] if ":" in reason_code else recommendation
        severity = _reason_severity(reason_code, local_recommendation)
        family = _reason_family(reason_code)
        seed = {
            "brain_id": brain_id,
            "brain_revision": brain_revision,
            "signal_family": family,
            "reason_code": reason_code,
            "recommendation": local_recommendation,
        }
        alerts.append(
            {
                "schema_version": BRAIN_HEALTH_ALERT_SCHEMA_VERSION,
                "alert_id": _stable_alert_id(seed),
                "brain_id": brain_id,
                "brain_revision": brain_revision,
                "severity": severity,
                "signal_family": family,
                "evidence_window": {
                    "window_kind": "point_in_time_plus_recent_sessions",
                    "min_repeated_evidence_required_for_mutation": severity not in {"info", "watch"},
                },
                "reason_codes": [reason_code],
                "recommendation": local_recommendation,
                "endpoint_hint": _endpoint_hint(local_recommendation),
                "debounce_state": "needs_more_evidence" if severity == "watch" else "actionable",
                "expires_at": None,
                "product_gate_impact": _product_gate_impact(severity),
                "rollback_or_preview_required": local_recommendation != "none",
                "created_at": generated_at,
            }
        )
    return alerts


def _build_benchmark_preflight(alerts: list[dict[str, Any]], recommendation: str) -> dict[str, Any]:
    severities = {str(alert.get("severity") or "") for alert in alerts}
    if recommendation == "rebuild_required" or "rebuild_required" in severities:
        verdict = "rebuild_required"
    elif "blocking" in severities or "action_recommended" in severities:
        verdict = "benchmark_blocked_until_preview"
    elif "watch" in severities or recommendation != "none":
        verdict = "benchmark_allowed_with_warnings"
    else:
        verdict = "healthy_for_benchmark"
    return {
        "schema_version": BENCHMARK_PREFLIGHT_SCHEMA_VERSION,
        "verdict": verdict,
        "serious_product_benchmark_allowed": verdict in {"healthy_for_benchmark", "benchmark_allowed_with_warnings"},
        "revolutionary_certification_allowed": verdict == "healthy_for_benchmark",
        "diagnostic_runs_allowed": True,
        "blocking_alert_count": sum(1 for alert in alerts if str(alert.get("severity")) in {"blocking", "rebuild_required"}),
        "action_recommended_count": sum(1 for alert in alerts if str(alert.get("severity")) == "action_recommended"),
        "required_before_phase8c": "resolve_or_preview_blocking_health_alerts",
    }


def _build_evolution_recommendation(
    *,
    recommendation: str,
    alerts: list[dict[str, Any]],
    reason_codes: list[str],
) -> dict[str, Any]:
    ranked = [dict(alert) for alert in alerts]
    return {
        "schema_version": EVOLUTION_RECOMMENDATION_SCHEMA_VERSION,
        "primary_recommendation": recommendation,
        "ranked_alerts": ranked[:12],
        "reason_codes": list(reason_codes),
        "endpoint_hint": _endpoint_hint(recommendation),
        "mutation_policy": {
            "default_policy": "manual_review",
            "hidden_mutation_allowed": False,
            "auto_preview_allowed": True,
            "auto_apply_low_risk_allowed": False,
            "structural_auto_apply_allowed": False,
        },
    }


def _quality_score(checks: dict[str, Any], key: str) -> float:
    try:
        return float(_as_dict(checks.get(key)).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _build_validation_brain_rebuild_gate(
    *,
    brain_id: str | None,
    brain_revision: str,
    node_count: int,
    target_min_nodes: int,
    target_max_nodes: int,
    checks: dict[str, Any],
    recommendation: str,
    reason_codes: list[str],
    metamemory_health: dict[str, Any],
    generated_at: str,
    current_brain_export: dict[str, Any] | None = None,
    source_manifest_snapshot: dict[str, Any] | None = None,
    latest_benchmark_verdict: dict[str, Any] | None = None,
    explicit_reset_approval: bool = False,
) -> dict[str, Any]:
    quality_thresholds = {
        "node_atomicity": 0.96,
        "identity_explicitness": 0.72,
        "source_coverage": 0.70,
        "link_coherence": 0.82,
        "document_retrievability": 0.72,
    }
    failed_quality: list[dict[str, Any]] = []
    for key, threshold in quality_thresholds.items():
        score = _quality_score(checks, key)
        if score < threshold:
            failed_quality.append({"check": key, "score": round(score, 6), "threshold": threshold})

    node_quality_green = not failed_quality
    if node_count < target_min_nodes:
        scale_status = "under_target"
    elif node_count > target_max_nodes:
        scale_status = "over_target"
    else:
        scale_status = "inside_target"

    spatial_readiness = _as_dict(metamemory_health.get("metamemory_spatial_readiness"))
    spatial_readiness_present = bool(spatial_readiness)
    spatial_certifiable = bool(spatial_readiness.get("certifiable")) if spatial_readiness_present else None
    spatial_blocking = spatial_readiness_present and not bool(spatial_certifiable)
    named_validation_brain = bool(brain_id and "validation" in str(brain_id).lower())
    rebuild_recommended = recommendation == "rebuild_required" or any(
        str(code).startswith("rebuild_required:") for code in reason_codes
    )
    grow_repair_required = recommendation == "grow_repair" or any(
        str(code).startswith("grow_repair:") for code in reason_codes
    )
    revision_chain = {
        "brain_revision": brain_revision,
        "metamemory_revision": metamemory_health.get("metamemory_revision"),
        "metamemory_hash": metamemory_health.get("metamemory_hash"),
        "matrix_revision": metamemory_health.get("matrix_revision"),
        "topology_revision": metamemory_health.get("topology_revision"),
        "atlas_revision": metamemory_health.get("atlas_revision"),
        "calibration_revision": metamemory_health.get("calibration_revision"),
        "source_replay_revision": metamemory_health.get("source_replay_revision"),
    }
    revision_chain_present = bool(revision_chain.get("brain_revision")) and any(
        revision_chain.get(key)
        for key in (
            "metamemory_revision",
            "metamemory_hash",
            "matrix_revision",
            "topology_revision",
            "atlas_revision",
            "calibration_revision",
            "source_replay_revision",
        )
    )
    pre_rebuild_exports = {
        "current_brain_export": bool(current_brain_export),
        "source_manifest": bool(source_manifest_snapshot),
        "revision_chain": revision_chain_present,
        "health_report": True,
        "latest_benchmark_verdict": bool(latest_benchmark_verdict),
    }
    required_export_keys = list(pre_rebuild_exports)
    missing_exports = [key for key in required_export_keys if not bool(pre_rebuild_exports.get(key))]

    if rebuild_recommended:
        if missing_exports:
            status = "blocked_until_snapshot_export"
        elif not named_validation_brain:
            status = "blocked_until_named_validation_brain"
        elif not explicit_reset_approval:
            status = "guarded_replay_plan_ready_operator_approval_required"
        else:
            status = "guarded_replay_allowed_for_named_validation_brain"
    elif not node_quality_green:
        status = "blocked_until_grow_repair_or_clean_replay"
    elif grow_repair_required:
        status = "node_quality_review_required_before_product_benchmark"
    elif scale_status == "under_target":
        status = "node_quality_clean_but_under_validation_scale"
    elif scale_status == "over_target":
        status = "node_quality_clean_but_over_target_needs_sleep_review"
    elif spatial_blocking:
        status = "node_quality_green_spatial_readiness_incomplete"
    elif not named_validation_brain:
        status = "node_quality_green_but_not_named_validation_brain"
    else:
        status = "current_validation_brain_node_quality_green"

    guarded_replay_allowed = (
        rebuild_recommended
        and not missing_exports
        and named_validation_brain
        and not failed_quality
        and explicit_reset_approval
    )
    if rebuild_recommended and not missing_exports and named_validation_brain and explicit_reset_approval:
        guarded_replay_allowed = True

    current_brain_usable_for_retrieve_benchmarks = (
        node_quality_green
        and named_validation_brain
        and scale_status == "inside_target"
        and not rebuild_recommended
        and not grow_repair_required
        and not spatial_blocking
    )
    product_proof_allowed_by_this_gate = current_brain_usable_for_retrieve_benchmarks

    blocking_reasons: list[str] = []
    if failed_quality:
        blocking_reasons.append("node_quality_not_green")
    if scale_status != "inside_target":
        blocking_reasons.append(f"validation_scale_{scale_status}")
    if rebuild_recommended:
        blocking_reasons.append("rebuild_required_by_health")
    if grow_repair_required:
        blocking_reasons.append("grow_repair_required_by_health")
    if spatial_blocking:
        blocking_reasons.append("metamemory_spatial_readiness_incomplete")
    if not named_validation_brain:
        blocking_reasons.append("brain_is_not_named_validation_brain")
    if rebuild_recommended and missing_exports:
        blocking_reasons.append("pre_rebuild_snapshot_export_incomplete")

    return {
        "schema_version": VALIDATION_BRAIN_REBUILD_GATE_SCHEMA_VERSION,
        "brain_id": brain_id,
        "generated_at": generated_at,
        "status": status,
        "non_mutating": True,
        "current_brain_usable_for_retrieve_benchmarks": current_brain_usable_for_retrieve_benchmarks,
        "product_proof_allowed_by_this_gate": product_proof_allowed_by_this_gate,
        "guarded_replay_allowed": guarded_replay_allowed,
        "destructive_reset_allowed": guarded_replay_allowed and explicit_reset_approval,
        "delete_existing_brain_allowed": False,
        "baseline_policy": {
            "keep_existing_brain_as_baseline": True,
            "delete_old_validation_brain_requires_separate_explicit_approval": True,
            "direct_database_mutation_is_product_invalid": True,
        },
        "node_quality_gate": {
            "green": node_quality_green,
            "failed_checks": failed_quality,
            "thresholds": dict(quality_thresholds),
            "shape_requirements": [
                "self_contained_text",
                "explicit_subject_relation",
                "source_unit_linkage",
                "parent_child_links",
                "identity_nucleus",
                "actionable_document_refs",
                "no_boilerplate",
                "no_qa_wrapper_inflation",
            ],
        },
        "scale_gate": {
            "status": scale_status,
            "node_count": int(node_count),
            "target_min_nodes": int(target_min_nodes),
            "target_max_nodes": int(target_max_nodes),
            "smaller_clean_brain_preferred_to_fake_scale": True,
        },
        "spatial_readiness_gate": {
            "present": spatial_readiness_present,
            "certifiable": spatial_certifiable,
            "status": spatial_readiness.get("status") if spatial_readiness_present else "not_reported",
        },
        "pre_rebuild_export_gate": {
            "required_when_replay_or_reset_is_needed": True,
            "exports": pre_rebuild_exports,
            "missing": missing_exports,
        },
        "revision_chain": revision_chain,
        "source_replay_policy": {
            "normal_grow_or_guarded_admin_replay_only": True,
            "manual_node_patch_invalidates_product_claim": True,
            "validation_brain_id_must_be_explicit": True,
            "named_validation_brain": named_validation_brain,
        },
        "blocking_reasons": blocking_reasons,
        "next_action": (
            "proceed_to_focused_real_mcp_validation"
            if current_brain_usable_for_retrieve_benchmarks
            else "export_snapshot_and_prepare_guarded_replay"
            if rebuild_recommended and missing_exports
            else "run_guarded_grow_repair_or_clean_replay_before_retrieve_benchmarks"
            if (failed_quality or grow_repair_required or rebuild_recommended)
            else "review_spatial_or_scale_gate_before_product_benchmark"
        ),
    }


def build_brain_health_report(
    graph: dict[str, Any],
    *,
    brain_id: str | None = None,
    identity_nucleus: dict[str, Any] | None = None,
    recent_search_sessions: list[dict[str, Any]] | None = None,
    recent_search_events: list[dict[str, Any]] | None = None,
    recent_feedback_events: list[dict[str, Any]] | None = None,
    recent_corrections: list[dict[str, Any]] | None = None,
    recent_maintenance_runs: list[dict[str, Any]] | None = None,
    recent_memory_learning_events: list[dict[str, Any]] | None = None,
    metamemory: dict[str, Any] | None = None,
    metamemory_spatial_brief: dict[str, Any] | None = None,
    calibration_snapshot: dict[str, Any] | None = None,
    current_brain_export: dict[str, Any] | None = None,
    source_manifest_snapshot: dict[str, Any] | None = None,
    latest_benchmark_verdict: dict[str, Any] | None = None,
    explicit_reset_approval: bool = False,
    target_min_nodes: int = 0,
    target_max_nodes: int = 4000,
    health_ai_diagnoser: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    nodes = [dict(node) for node in _as_list(graph.get("nodes")) if isinstance(node, dict)]
    edges = [dict(edge) for edge in _as_list(graph.get("edges")) if isinstance(edge, dict)]
    identity = _as_dict(identity_nucleus)
    node_atomicity = _analyze_node_atomicity(nodes)
    identity_explicitness = _analyze_identity_explicitness(nodes, identity)
    source_coverage = _analyze_source_coverage(nodes)
    link_coherence = _analyze_link_coherence(nodes, edges)
    document_retrievability = _analyze_document_retrievability(nodes)
    radial_distribution = _analyze_radial_distribution(nodes)
    recent_session_rows = [dict(item) for item in _as_list(recent_search_sessions) if isinstance(item, dict)]
    feedback_ledger = build_brain_feedback_ledger(
        brain_id=brain_id,
        search_sessions=recent_session_rows,
        search_events=recent_search_events,
        feedback_events=recent_feedback_events,
        corrections=recent_corrections,
        learning_events=recent_memory_learning_events,
    )
    recent_failures = _analyze_recent_retrieval_failures(recent_session_rows)
    metamemory_health = _analyze_metamemory(
        metamemory=metamemory,
        metamemory_spatial_brief=metamemory_spatial_brief,
        calibration_snapshot=calibration_snapshot,
        recent_maintenance_runs=recent_maintenance_runs,
    )
    retrieval_learning_rollup = _analyze_retrieval_learning_rollup(
        recent_session_rows,
        calibration_snapshot=calibration_snapshot,
        recent_maintenance_runs=recent_maintenance_runs,
    )
    metacognitive_observations = _analyze_metacognitive_observations(recent_memory_learning_events)
    spatial_prior_resolution = _spatial_prior_resolution_status(
        metamemory_health=metamemory_health,
        radial_distribution=radial_distribution,
        retrieval_learning_rollup=retrieval_learning_rollup,
        recent_maintenance_runs=recent_maintenance_runs,
    )
    metamemory_health["spatial_prior_resolution"] = spatial_prior_resolution
    reason_codes: list[str] = []
    node_count = len(nodes)
    if float(node_atomicity["score"]) < 0.96:
        reason_codes.append("grow_repair:node_atomicity_fragments")
    if float(identity_explicitness["score"]) < 0.72:
        reason_codes.append("grow_repair:identity_nucleus_weak")
    if float(source_coverage["score"]) < 0.70:
        reason_codes.append("grow_repair:source_coverage_weak")
    if float(document_retrievability["score"]) < 0.72:
        reason_codes.append("grow_repair:document_anchor_or_raw_retrieval_weak")
    if float(link_coherence["score"]) < 0.82:
        reason_codes.append("evolve_preview:link_or_orphan_coherence_weak")
    if float(radial_distribution["score"]) < 0.75:
        reason_codes.append("matrix_calibration_preview:radial_or_bucket_distribution_weak")
    if float(radial_distribution.get("semantic_diversity_score") or 0.0) < 0.75 and node_count >= 50:
        reason_codes.append("matrix_calibration_preview:semantic_zone_distribution_weak")
    if float(radial_distribution.get("semantic_radial_alignment_score") or 1.0) < 0.70 and node_count >= 50:
        reason_codes.append("matrix_calibration_preview:semantic_radial_alignment_weak")
    reason_codes.extend(str(code) for code in retrieval_learning_rollup.get("reason_codes") or [] if str(code))
    reason_codes.extend(str(code) for code in metacognitive_observations.get("reason_codes") or [] if str(code))
    if int(metamemory_health.get("spatial_correction_review_candidate_count") or 0) > 0:
        if bool(spatial_prior_resolution.get("resolved_by_recent_matrix_apply")):
            reason_codes.append("watch:spatial_landing_correction_priors_resolved_by_recent_matrix_apply")
        elif bool(spatial_prior_resolution.get("resolved_by_recent_matrix_preview")):
            reason_codes.append("watch:spatial_landing_correction_priors_previewed_with_green_geometry")
        else:
            reason_codes.append("matrix_calibration_preview:spatial_landing_correction_priors_review_pending")
    spatial_readiness = _as_dict(metamemory_health.get("metamemory_spatial_readiness"))
    if metamemory_health.get("metamemory_spatial_brief_runtime_present") and not bool(spatial_readiness.get("certifiable")):
        reason_codes.append("matrix_calibration_preview:metamemory_spatial_brief_incomplete")
    if (
        float(node_atomicity["score"]) < 0.94
        and float(source_coverage["score"]) < 0.65
        and float(document_retrievability["score"]) < 0.65
    ):
        reason_codes.append("rebuild_required:source_replay_cleaner_than_local_repair")
    if (
        float(node_atomicity["score"]) < 0.90
        and float(link_coherence["score"]) < 0.70
        and float(source_coverage["score"]) < 0.60
    ):
        reason_codes.append("rebuild_required:compound_memory_shape_failure")
    recommendation = _primary_recommendation(reason_codes)
    readiness = "healthy" if recommendation == "none" else "needs_attention" if recommendation != "rebuild_required" else "rebuild_recommended"
    checks = {
        "node_atomicity": node_atomicity,
        "identity_explicitness": identity_explicitness,
        "source_coverage": source_coverage,
        "link_coherence": link_coherence,
        "document_retrievability": document_retrievability,
        "radial_distribution": radial_distribution,
        "recent_retrieval_failures": recent_failures,
        "retrieval_learning_rollup": retrieval_learning_rollup,
        "metacognitive_observations": metacognitive_observations,
        "brain_feedback_ledger": dict(feedback_ledger.get("health_rollup") or {}),
        "metamemory": metamemory_health,
    }
    score_keys = [
        "node_atomicity",
        "identity_explicitness",
        "source_coverage",
        "link_coherence",
        "document_retrievability",
        "radial_distribution",
        "recent_retrieval_failures",
        "retrieval_learning_rollup",
        "metacognitive_observations",
    ]
    overall_score = round(sum(float(checks[key].get("score") or 0.0) for key in score_keys) / max(1, len(score_keys)), 6)
    actions = [
        {
            "action": recommendation,
            "mutating": False,
            "requires_preview_apply_rollback": recommendation
            in {"sleep_preview", "evolve_preview", "matrix_calibration_preview", "grow_repair", "rebuild_required"},
            "endpoint_hint": {
                "sleep_preview": "/mcp/sleep-preview",
                "evolve_preview": "/mcp/evolve-preview",
                "matrix_calibration_preview": "/memory/geometry-calibration",
                "grow_repair": "/mcp/grow-source-preview",
                "rebuild_required": "reset validation brain only through guarded reset/replay harness",
                "none": None,
            }.get(recommendation),
        }
    ]
    generated_at = utc_timestamp()
    brain_revision = "|".join(
        [
            f"nodes:{node_count}",
            f"edges:{len(edges)}",
            f"meta:{metamemory_health.get('metamemory_hash') or metamemory_health.get('metamemory_revision') or 'none'}",
            f"cal:{metamemory_health.get('calibration_event_count') or 0}",
        ]
    )
    health_alerts = _build_health_alerts(
        brain_id=brain_id,
        brain_revision=brain_revision,
        reason_codes=reason_codes,
        recommendation=recommendation,
        generated_at=generated_at,
    )
    benchmark_preflight = _build_benchmark_preflight(health_alerts, recommendation)
    evolution_recommendation = _build_evolution_recommendation(
        recommendation=recommendation,
        alerts=health_alerts,
        reason_codes=reason_codes,
    )
    brain_sanity_snapshot = {
        "schema_version": BRAIN_SANITY_SNAPSHOT_SCHEMA_VERSION,
        "brain_id": brain_id,
        "brain_revision": brain_revision,
        "generated_at": generated_at,
        "severity": (
            "rebuild_required"
            if benchmark_preflight["verdict"] == "rebuild_required"
            else "blocking"
            if benchmark_preflight["verdict"] == "benchmark_blocked_until_preview"
            else "watch"
            if benchmark_preflight["verdict"] == "benchmark_allowed_with_warnings"
            else "info"
        ),
        "recommendation": recommendation,
        "reason_codes": list(reason_codes),
        "alert_count": len(health_alerts),
        "product_gate_impact": benchmark_preflight["verdict"],
        "preview_required": benchmark_preflight["verdict"] in {"benchmark_blocked_until_preview", "rebuild_required"},
        "rollback_or_preview_required": recommendation != "none",
        "health_latency_ms": None,
        "non_mutating": True,
    }
    automation_policy = {
        "schema_version": "agvm.automation_policy.v1",
        "policy_mode": "manual_review",
        "hidden_mutation_allowed": False,
        "auto_preview_allowed": True,
        "auto_apply_low_risk_allowed": False,
        "auto_apply_structural_allowed": False,
        "operator_confirmation_required_for_apply": True,
    }
    validation_brain_rebuild_gate = _build_validation_brain_rebuild_gate(
        brain_id=brain_id,
        brain_revision=brain_revision,
        node_count=node_count,
        target_min_nodes=target_min_nodes,
        target_max_nodes=target_max_nodes,
        checks=checks,
        recommendation=recommendation,
        reason_codes=reason_codes,
        metamemory_health=metamemory_health,
        generated_at=generated_at,
        current_brain_export=current_brain_export,
        source_manifest_snapshot=source_manifest_snapshot,
        latest_benchmark_verdict=latest_benchmark_verdict,
        explicit_reset_approval=explicit_reset_approval,
    )
    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    brain_sanity_snapshot["health_latency_ms"] = latency_ms
    report = {
        "schema_version": BRAIN_HEALTH_SCHEMA_VERSION,
        "service": APP_NAME,
        "version": APP_VERSION,
        "brain_id": brain_id,
        "generated_at": generated_at,
        "latency_ms": latency_ms,
        "readiness": readiness,
        "recommendation": recommendation,
        "reason_codes": reason_codes,
        "overall_score": overall_score,
        "summary": {
            "node_count": node_count,
            "edge_count": len(edges),
            "target_node_range": {"min": int(target_min_nodes), "max": int(target_max_nodes)},
            "document_anchor_count": document_retrievability["document_anchor_count"],
            "orphan_ratio": link_coherence["orphan_ratio"],
            "radial_spread": radial_distribution["radial_spread"],
            "recent_session_count": recent_failures["session_count"],
            "retrieval_learning_signal_count": retrieval_learning_rollup["signal_count"],
            "retrieval_learning_repeated_families": list(retrieval_learning_rollup["repeated_signal_families"]),
            "metacognitive_observation_count": metacognitive_observations["observation_count"],
            "metacognitive_repeated_kinds": list(metacognitive_observations["repeated_observation_kinds"]),
            "feedback_signal_count": int(feedback_ledger.get("signal_count") or 0),
            "explicit_feedback_signal_count": int(
                dict(feedback_ledger.get("health_rollup") or {}).get("explicit_signal_count") or 0
            ),
            "health_is_non_mutating": True,
            "validation_rebuild_gate_status": validation_brain_rebuild_gate["status"],
            "validation_node_quality_green": validation_brain_rebuild_gate["node_quality_gate"]["green"],
            "validation_brain_usable_for_retrieve_benchmarks": validation_brain_rebuild_gate[
                "current_brain_usable_for_retrieve_benchmarks"
            ],
        },
        "checks": checks,
        "brain_feedback_ledger": feedback_ledger,
        "actions": actions,
        "brain_sanity_snapshot": brain_sanity_snapshot,
        "health_alerts": health_alerts,
        "alert_summary": {
            "alert_count": len(health_alerts),
            "severity_histogram": dict(Counter(str(alert.get("severity") or "unknown") for alert in health_alerts)),
            "family_histogram": dict(Counter(str(alert.get("signal_family") or "unknown") for alert in health_alerts)),
        },
        "evolution_recommendation": evolution_recommendation,
        "benchmark_preflight": benchmark_preflight,
        "validation_brain_rebuild_gate": validation_brain_rebuild_gate,
        "automation_policy": automation_policy,
        "safety_contract": {
            "non_mutating": True,
            "hidden_mutation_allowed": False,
            "deterministic_health_is_authoritative": True,
            "ai_diagnosis_may_override_health": False,
            "sleep_evolve_apply_requires_explicit_acceptance": True,
            "matrix_updates_require_preview_apply_rollback": True,
            "direct_db_repair_is_product_invalid": True,
        },
        "product_claim": {
            "product_ready_claim_allowed": False,
            "revolutionary_claim_allowed": False,
            "reason": "brain_health_is_a_diagnostic_gate_not_full_mcp_product_benchmark",
        },
    }
    ai_diagnosis = build_health_ai_readonly_diagnosis(
        deterministic_health=report,
        feedback_ledger=feedback_ledger,
        diagnoser=health_ai_diagnoser,
    )
    report["health_ai_diagnosis"] = ai_diagnosis
    report["health_ai_attestation"] = dict(ai_diagnosis.get("attestation") or {})
    return report


def build_mcp_brain_health_output(report: dict[str, Any]) -> dict[str, Any]:
    recommendation = str(report.get("recommendation") or "none")
    status = "ok" if recommendation == "none" else "partial" if recommendation != "rebuild_required" else "blocked"
    return {
        "schema_version": MCP_BRAIN_HEALTH_SCHEMA_VERSION,
        "tool_name": "brain_health",
        "status": status,
        "brain_health_report": dict(report),
        "recommendation": recommendation,
        "reason_codes": list(report.get("reason_codes") or []),
        "health_summary": dict(report.get("summary") or {}),
        "checks": dict(report.get("checks") or {}),
        "actions": list(report.get("actions") or []),
        "retrieval_learning_rollup": dict(dict(report.get("checks") or {}).get("retrieval_learning_rollup") or {}),
        "metacognitive_observation_rollup": dict(dict(report.get("checks") or {}).get("metacognitive_observations") or {}),
        "brain_feedback_ledger": dict(report.get("brain_feedback_ledger") or {}),
        "health_ai_diagnosis": dict(report.get("health_ai_diagnosis") or {}),
        "health_ai_attestation": dict(report.get("health_ai_attestation") or {}),
        "brain_sanity_snapshot": dict(report.get("brain_sanity_snapshot") or {}),
        "health_alerts": list(report.get("health_alerts") or []),
        "alert_summary": dict(report.get("alert_summary") or {}),
        "evolution_recommendation": dict(report.get("evolution_recommendation") or {}),
        "benchmark_preflight": dict(report.get("benchmark_preflight") or {}),
        "validation_brain_rebuild_gate": dict(report.get("validation_brain_rebuild_gate") or {}),
        "automation_policy": dict(report.get("automation_policy") or {}),
        "safety_contract": dict(report.get("safety_contract") or {}),
        "budget": {
            "mutation_allowed": False,
            "heavy_audit_used": False,
            "recent_session_limit": int(dict(report.get("checks") or {}).get("recent_retrieval_failures", {}).get("session_count") or 0),
        },
    }
