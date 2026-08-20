from __future__ import annotations

import hashlib
import os
import re
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from config import COARSE_BUCKET_SIZE, FINE_BUCKET_SIZE, ROUTING_FIELDS
from llm import llm_enabled, sleep_model, structured_json
from memory_hygiene import is_answer_eligible
from metamemory import build_metamemory_package
from maintenance_contract import (
    build_apply_policy_rollback_guard,
    build_evolve_structural_proposal_engine,
    build_maintenance_baseline_contract,
    build_maintenance_transaction,
    build_retrieval_trace_learning_gate,
    build_sleep_consolidation_proposal_engine,
    combine_maintenance_proposal_surfaces,
)
from memory_learning_maintenance import build_ingest_learning_review
from memory_policy_revisioning import build_memory_policy_revision_candidate
from projection import color_from_brainhex, position_to_bucket, position_to_topology_brainhex, semantic_similarity
from retrieval import find_local_candidates
from runtime_scope import current_brain_id
from storage import utc_timestamp
from projection import lexical_overlap


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _preview_node_budget_guard(
    *,
    mode: str,
    preview_only: bool,
    max_nodes_considered: int,
) -> dict[str, Any]:
    requested = max(10, _safe_int(max_nodes_considered, 80))
    normalized_mode = str(mode or "sleep_evolve").strip().lower()
    if not preview_only:
        effective = requested
        cap = None
    elif normalized_mode == "sleep":
        cap = max(10, _safe_int(os.environ.get("AGVM_SLEEP_PREVIEW_MAX_NODES"), 160))
        effective = min(requested, cap)
    else:
        cap = max(10, _safe_int(os.environ.get("AGVM_EVOLVE_PREVIEW_MAX_NODES"), 80))
        effective = min(requested, cap)
    return {
        "schema_version": "agvm.maintenance_preview_node_budget_guard.v1",
        "mode": normalized_mode,
        "preview_only": bool(preview_only),
        "requested_max_nodes_considered": requested,
        "effective_max_nodes_considered": effective,
        "bounded": bool(preview_only and effective != requested),
        "cap": cap,
        "policy": (
            "mcp_preview_is_bounded_to_keep_non_mutating_maintenance_inspectable; "
            "larger reviews must run as chunked/deferred maintenance, not inside one MCP request"
        ),
    }


def _node_preview_priority(node: dict[str, Any]) -> float:
    memory_type = str(node.get("memory_type") or "").strip()
    lifecycle = str(node.get("lifecycle_status") or "active").strip()
    priority_by_type = {
        "identity": 8.0,
        "value": 7.0,
        "identity_style": 7.0,
        "relational": 6.0,
        "project": 5.0,
        "document_anchor": 4.0,
        "document_fact": 3.0,
        "document_summary": 3.0,
        "knowledge": 2.0,
        "episodic": 2.0,
    }
    priority = priority_by_type.get(memory_type, 1.0)
    if lifecycle != "active":
        priority -= 1.5
    if bool(node.get("is_document_anchor")):
        priority += 1.0
    priority += min(1.0, _safe_float(node.get("memory_confidence") or node.get("stability_confidence") or 0.0))
    return priority


def _region_summary_for_node(node: dict[str, Any]) -> dict[str, Any]:
    region_id = _region_key_for_node(node)
    position = dict(node.get("final_position") or {})
    return {
        "schema_version": "agvm.maintenance_region_summary.v1",
        "region_id": region_id,
        "bucket": node.get("bucket"),
        "routing_bucket": node.get("routing_bucket"),
        "memory_type": str(node.get("memory_type") or ""),
        "guide_area": str((node.get("provenance") or {}).get("guide_conceptual_area") or ""),
        "has_position": bool(position),
        "position": {
            "x": round(_safe_float(position.get("x")), 5),
            "y": round(_safe_float(position.get("y")), 5),
            "z": round(_safe_float(position.get("z")), 5),
        }
        if position
        else {},
    }


def _build_maintenance_preview_plan(
    graph: dict[str, Any],
    *,
    mode: str,
    preview_only: bool,
    focus_node_id: str | None,
    preview_budget_guard: dict[str, Any],
    trace_insights: dict[str, Any],
    correction_insights: dict[str, Any],
    ingest_learning_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    nodes_by_id = {str(node.get("id") or ""): node for node in nodes if str(node.get("id") or "").strip()}
    requested = _safe_int(preview_budget_guard.get("requested_max_nodes_considered"), 80)
    effective = _safe_int(preview_budget_guard.get("effective_max_nodes_considered"), 80)
    bounded = bool(preview_budget_guard.get("bounded"))
    if not preview_only:
        preview_depth = "apply_candidate"
    elif effective <= 30:
        preview_depth = "fast_scan"
    elif bounded:
        preview_depth = "chunked_preview"
    else:
        preview_depth = "deep_review"

    candidate_scores: dict[str, float] = {}
    candidate_reasons: dict[str, list[str]] = defaultdict(list)

    def add_candidate(node_id: Any, score: float, reason: str) -> None:
        key = str(node_id or "").strip()
        if not key or key not in nodes_by_id:
            return
        candidate_scores[key] = candidate_scores.get(key, 0.0) + score
        candidate_reasons[key].append(reason)

    if focus_node_id:
        add_candidate(focus_node_id, 100.0, "focus_node")

    for node_id, hits in dict(trace_insights.get("match_hits") or {}).items():
        add_candidate(node_id, 12.0 + min(12.0, _safe_int(hits) * 2.0), "recent_match_hit")
    for node_id, hits in dict(trace_insights.get("candidate_hits") or {}).items():
        add_candidate(node_id, 5.0 + min(8.0, _safe_int(hits)), "recent_candidate_hit")
    for node_id, hits in dict(correction_insights.get("target_hits") or {}).items():
        add_candidate(node_id, 10.0 + min(10.0, _safe_int(hits) * 2.0), "correction_target")
    for node_id, hits in dict(correction_insights.get("evidence_hits") or {}).items():
        add_candidate(node_id, 8.0 + min(8.0, _safe_int(hits) * 1.5), "correction_evidence")

    ingest_scores = dict(dict(ingest_learning_review or {}).get("candidate_scores_by_node_id") or {})
    ingest_reasons = dict(dict(ingest_learning_review or {}).get("candidate_reasons_by_node_id") or {})
    for node_id, score in ingest_scores.items():
        reasons = list(ingest_reasons.get(node_id) or [])
        add_candidate(node_id, 6.0 + min(28.0, _safe_float(score)), reasons[0] if reasons else "ingest_learning_feedback")
        for reason in reasons[1:4]:
            add_candidate(node_id, 0.25, str(reason))

    whole_brain_cursor = dict(dict(ingest_learning_review or {}).get("whole_brain_cursor") or {})
    for node_id in list(whole_brain_cursor.get("selected_node_ids") or []):
        add_candidate(node_id, 0.5, "whole_brain_cursor")

    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in candidate_scores:
            continue
        priority = _node_preview_priority(node)
        if priority >= 5.0:
            add_candidate(node_id, priority, "core_memory_priority")

    if len(candidate_scores) < effective:
        for node in nodes:
            node_id = str(node.get("id") or "")
            if not node_id or node_id in candidate_scores:
                continue
            add_candidate(node_id, _node_preview_priority(node), "budget_fill")
            if len(candidate_scores) >= max(effective, min(len(nodes), effective * 3)):
                break

    ranked_node_ids = sorted(
        candidate_scores,
        key=lambda node_id: (-candidate_scores[node_id], node_id),
    )
    selected_node_ids = ranked_node_ids[:effective]
    selected_regions: dict[str, dict[str, Any]] = {}
    for node_id in selected_node_ids:
        region_summary = _region_summary_for_node(nodes_by_id[node_id])
        region_id = str(region_summary.get("region_id") or "")
        if not region_id:
            continue
        payload = selected_regions.setdefault(
            region_id,
            {
                **region_summary,
                "selected_node_count": 0,
                "signal_reasons": {},
            },
        )
        payload["selected_node_count"] = int(payload.get("selected_node_count") or 0) + 1
        for reason in candidate_reasons.get(node_id, []):
            signal_reasons = dict(payload.get("signal_reasons") or {})
            signal_reasons[reason] = int(signal_reasons.get(reason) or 0) + 1
            payload["signal_reasons"] = signal_reasons

    deferred_count = max(0, len(ranked_node_ids) - len(selected_node_ids))
    chunk_size = max(1, effective)
    return {
        "schema_version": "agvm.maintenance_preview_plan.v1",
        "mode": str(mode or "sleep_evolve"),
        "preview_only": bool(preview_only),
        "preview_depth": preview_depth,
        "requested_max_nodes_considered": requested,
        "effective_max_nodes_considered": effective,
        "bounded": bounded,
        "full_graph_node_count": len(nodes),
        "candidate_node_count": len(ranked_node_ids),
        "selected_node_count": len(selected_node_ids),
        "selected_node_ids": selected_node_ids,
        "selected_region_count": len(selected_regions),
        "selected_region_ids": list(selected_regions.keys())[:80],
        "selected_regions": list(selected_regions.values())[:24],
        "deferred_node_count": deferred_count,
        "deferred_chunk_count": (deferred_count + chunk_size - 1) // chunk_size if deferred_count else 0,
        "recommended_follow_up_chunks": [
            {
                "chunk_index": index + 1,
                "node_id_start": ranked_node_ids[effective + index * chunk_size] if effective + index * chunk_size < len(ranked_node_ids) else None,
                "max_nodes_considered": effective,
                "reason": "continue_deferred_maintenance_preview_region_set",
            }
            for index in range(min(4, (deferred_count + chunk_size - 1) // chunk_size if deferred_count else 0))
        ],
        "whole_brain_cursor": whole_brain_cursor,
        "ingest_learning_review_summary": {
            "review_id": dict(ingest_learning_review or {}).get("review_id"),
            "event_count": _safe_int(dict(ingest_learning_review or {}).get("event_count") or 0),
            "priority_event_count": _safe_int(dict(ingest_learning_review or {}).get("priority_event_count") or 0),
            "candidate_node_count": len(list(dict(ingest_learning_review or {}).get("candidate_node_ids") or [])),
            "event_kind_histogram": dict(dict(ingest_learning_review or {}).get("event_kind_histogram") or {}),
        },
        "skipped_or_deferred_reason": "bounded_preview_budget" if deferred_count else "",
        "ai_context_policy": "compact_region_summaries_and_mission_learning_only; no_full_brain_text",
        "mutation_policy": "non_mutating_preview_plan",
    }


def _nodes_by_selected_ids(graph: dict[str, Any], selected_ids: set[str]) -> list[dict[str, Any]]:
    return [
        dict(node)
        for node in list(graph.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("id") or "") in selected_ids
    ]


def _preview_scope_graph(graph: dict[str, Any], selected_node_ids: set[str], *, max_extra_edges: int = 160) -> dict[str, Any]:
    selected_nodes = _nodes_by_selected_ids(graph, selected_node_ids)
    selected = {str(node.get("id") or "") for node in selected_nodes if str(node.get("id") or "")}
    scoped_edges: list[dict[str, Any]] = []
    for edge in list(graph.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("target_node_id") or "")
        if source in selected and target in selected:
            scoped_edges.append(dict(edge))
            if len(scoped_edges) >= max_extra_edges:
                break
    return {
        **graph,
        "nodes": selected_nodes,
        "edges": scoped_edges,
        "meta": {
            **dict(graph.get("meta") or {}),
            "maintenance_preview_scope_graph": True,
            "selected_node_count": len(selected_nodes),
            "full_graph_node_count": len(list(graph.get("nodes") or [])),
        },
    }


def _safe_ratio(numerator: float, denominator: float) -> float:
    return round(float(numerator) / max(1.0, float(denominator)), 6)


def _parse_iso_age_hours(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600.0)


def _branch_families(branch: dict[str, Any]) -> set[str]:
    families = {
        str(item).strip().lower()
        for item in list(branch.get("origin_families") or [])
        if str(item).strip().lower() in {"heuristic", "ai"}
    }
    if families:
        return families
    family = str(branch.get("planner_family") or "").strip().lower()
    if family in {"heuristic", "ai"}:
        return {family}
    return {"heuristic"}


def _retrieval_session_signal(session: dict[str, Any]) -> dict[str, Any]:
    plan = dict(session.get("plan") or {})
    result = dict(session.get("result") or {})
    plan_runtime = dict(plan.get("planner_runtime") or {})
    planner_runtime = dict(result.get("planner_runtime") or plan_runtime)
    answer = dict(result.get("answer") or {})
    shared_evidence = dict(result.get("shared_evidence") or {})
    master_state = dict(
        result.get("master_state")
        or shared_evidence.get("master_state")
        or planner_runtime.get("master_state")
        or {}
    )
    branches = [
        dict(branch)
        for branch in list(result.get("branches") or plan.get("branches") or [])
        if isinstance(branch, dict)
    ]
    matches = list(result.get("matches") or [])
    heuristic_calibration = dict(planner_runtime.get("heuristic_calibration") or {})
    answerability_state = str(
        result.get("answerability_state")
        or answer.get("answerability_state")
        or session.get("answerability_state")
        or ""
    ).strip()
    stop_reason = str(result.get("stop_reason") or session.get("stop_reason") or "").strip()
    answer_adequacy = dict(answer.get("answer_adequacy") or result.get("answer_adequacy") or {})
    closure_blockers = [
        dict(item)
        for item in list(
            result.get("final_closure_blockers")
            or master_state.get("final_closure_blockers")
            or planner_runtime.get("final_closure_blockers")
            or []
        )
        if isinstance(item, dict)
    ]
    unresolved_destination_count = _safe_int(
        result.get("unresolved_destination_count")
        or master_state.get("unresolved_destination_count")
        or planner_runtime.get("unresolved_destination_count")
        or 0
    )
    destination_reached_ratio = _safe_float(
        planner_runtime.get("destination_reached_ratio")
        or result.get("destination_reached_ratio")
        or 0.0
    )
    branch_evidence_count = sum(len(list(branch.get("evidence_node_ids") or [])) for branch in branches)
    match_evidence_count = len(matches)
    evidence_count = branch_evidence_count + match_evidence_count + len(list(answer.get("evidence_node_ids") or []))
    heuristic_evidence_count = sum(
        len(list(branch.get("evidence_node_ids") or []))
        for branch in branches
        if "heuristic" in _branch_families(branch)
    )
    ai_evidence_count = sum(
        len(list(branch.get("evidence_node_ids") or []))
        for branch in branches
        if "ai" in _branch_families(branch)
    )
    ai_probe_count = _safe_int(planner_runtime.get("llm_added_probe_count") or 0) + _safe_int(planner_runtime.get("llm_probe_count") or 0)
    merge_resolution = dict(planner_runtime.get("merge_resolution_histogram") or {})
    branch_merge_count = (
        _safe_int(planner_runtime.get("branch_reuse_count") or 0)
        + _safe_int(planner_runtime.get("branch_enrich_count") or 0)
        + _safe_int(planner_runtime.get("branch_fork_count") or 0)
        + sum(_safe_int(value) for value in merge_resolution.values())
    )
    final_closure_ready = bool(
        result.get("final_closure_ready")
        if result.get("final_closure_ready") is not None
        else master_state.get("final_closure_ready")
    )
    route_gap_reasons: list[str] = []
    if unresolved_destination_count > 0:
        route_gap_reasons.append("unresolved_destinations")
    if "exhausted" in stop_reason or stop_reason in {"budget_exhausted", "route_budget_exhausted"}:
        route_gap_reasons.append("route_or_budget_exhausted")
    if evidence_count <= 0:
        route_gap_reasons.append("no_evidence")
    if branches and destination_reached_ratio <= 0.0 and answerability_state not in {"grounded", "partial"}:
        route_gap_reasons.append("destination_not_reached")

    final_eval_reasons: list[str] = []
    if answerability_state and answerability_state not in {"grounded", "partial"}:
        final_eval_reasons.append("answerability_not_grounded")
    if answer_adequacy and not bool(answer_adequacy.get("passed")):
        final_eval_reasons.append("answer_adequacy_failed")
    if bool(answer.get("contradiction_present") or result.get("contradiction_present")):
        final_eval_reasons.append("contradiction_present")
    if closure_blockers and not final_closure_ready:
        final_eval_reasons.append("final_closure_blocked")

    query_class = str(
        planner_runtime.get("query_class")
        or plan_runtime.get("query_class")
        or heuristic_calibration.get("query_class")
        or ""
    ).strip()
    return {
        "search_id": str(session.get("search_id") or ""),
        "query_text": str(session.get("query_text") or result.get("query_text") or "")[:180],
        "query_class": query_class,
        "planner_mode": str(result.get("planner_mode") or plan.get("planner_mode") or planner_runtime.get("planner_mode") or ""),
        "answerability_state": answerability_state,
        "stop_reason": stop_reason,
        "branch_count": len(branches),
        "heuristic_evidence": heuristic_evidence_count > 0,
        "ai_evidence": ai_evidence_count > 0 or bool(result.get("ai_material_contribution")) or ai_probe_count > 0,
        "dual_origin": any(bool(branch.get("dual_origin")) or len(_branch_families(branch)) > 1 for branch in branches),
        "branch_merge": branch_merge_count > 0,
        "compiled_prior": bool(
            heuristic_calibration.get("compiled_prior_available")
            or heuristic_calibration.get("compiled_prior_applied")
            or list(heuristic_calibration.get("compiled_prior_scope_keys") or [])
        ),
        "failure_signature": _safe_int(heuristic_calibration.get("failure_signature_count") or 0) > 0
        or _safe_int(heuristic_calibration.get("review_candidate_count") or 0) > 0,
        "calibration_scope_keys": [
            str(item)
            for item in list(heuristic_calibration.get("scope_keys_used") or [])
            if str(item).strip()
        ],
        "route_gap_reasons": route_gap_reasons,
        "final_eval_reasons": final_eval_reasons,
        "gap_reasons": list(dict.fromkeys([*route_gap_reasons, *final_eval_reasons])),
        "unresolved_destination_count": unresolved_destination_count,
        "destination_reached_ratio": round(destination_reached_ratio, 6),
        "evidence_count": evidence_count,
        "answer_adequacy_passed": bool(answer_adequacy.get("passed")) if answer_adequacy else None,
    }


def _build_retrieval_gap_summary(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    signals = [_retrieval_session_signal(session) for session in sessions if dict(session.get("result") or {})]
    reason_histogram: dict[str, int] = defaultdict(int)
    planner_family_histogram: dict[str, int] = defaultdict(int)
    route_gap_examples: list[dict[str, Any]] = []
    final_eval_examples: list[dict[str, Any]] = []
    calibration_scope_keys: list[str] = []
    for signal in signals:
        planner_mode = str(signal.get("planner_mode") or "unknown").strip() or "unknown"
        planner_family_histogram[planner_mode] += 1
        for reason in list(signal.get("gap_reasons") or []):
            reason_histogram[str(reason)] += 1
        calibration_scope_keys.extend(str(item) for item in list(signal.get("calibration_scope_keys") or []) if str(item).strip())
        if signal.get("route_gap_reasons") and len(route_gap_examples) < 5:
            route_gap_examples.append(
                {
                    "search_id": signal["search_id"],
                    "query_text": signal["query_text"],
                    "query_class": signal["query_class"],
                    "answerability_state": signal["answerability_state"],
                    "reasons": list(signal.get("route_gap_reasons") or []),
                    "unresolved_destination_count": signal["unresolved_destination_count"],
                    "destination_reached_ratio": signal["destination_reached_ratio"],
                    "evidence_count": signal["evidence_count"],
                }
            )
        if signal.get("final_eval_reasons") and len(final_eval_examples) < 5:
            final_eval_examples.append(
                {
                    "search_id": signal["search_id"],
                    "query_text": signal["query_text"],
                    "query_class": signal["query_class"],
                    "answerability_state": signal["answerability_state"],
                    "reasons": list(signal.get("final_eval_reasons") or []),
                    "answer_adequacy_passed": signal["answer_adequacy_passed"],
                }
            )
    session_count = len(signals)
    route_gap_count = sum(1 for signal in signals if list(signal.get("route_gap_reasons") or []))
    final_eval_failure_count = sum(1 for signal in signals if list(signal.get("final_eval_reasons") or []))
    gap_session_count = sum(1 for signal in signals if list(signal.get("gap_reasons") or []))
    return {
        "schema_version": "agvm.retrieval_gap_summary.v1",
        "evidence_source": "recent_search_sessions.result_json",
        "session_count": session_count,
        "heuristic_evidence_session_count": sum(1 for signal in signals if bool(signal.get("heuristic_evidence"))),
        "ai_evidence_session_count": sum(1 for signal in signals if bool(signal.get("ai_evidence"))),
        "dual_origin_session_count": sum(1 for signal in signals if bool(signal.get("dual_origin"))),
        "branch_merge_session_count": sum(1 for signal in signals if bool(signal.get("branch_merge"))),
        "compiled_prior_session_count": sum(1 for signal in signals if bool(signal.get("compiled_prior"))),
        "failure_signature_session_count": sum(1 for signal in signals if bool(signal.get("failure_signature"))),
        "route_gap_session_count": route_gap_count,
        "final_eval_failure_session_count": final_eval_failure_count,
        "gap_session_count": gap_session_count,
        "maintenance_retrieval_gap_detection_ratio": _safe_ratio(gap_session_count, session_count),
        "route_gap_detection_ratio": _safe_ratio(route_gap_count, session_count),
        "final_eval_failure_ratio": _safe_ratio(final_eval_failure_count, session_count),
        "gap_reasons": dict(reason_histogram),
        "planner_mode_histogram": dict(planner_family_histogram),
        "calibration_scope_keys": list(dict.fromkeys(calibration_scope_keys))[:16],
        "route_gap_examples": route_gap_examples,
        "final_eval_failure_examples": final_eval_examples,
    }


def _calibration_snapshot_for_maintenance() -> dict[str, Any]:
    try:
        from heuristic_calibration import summarize_calibration_snapshot
        from sqlite_store import fetch_heuristic_calibration_snapshot

        snapshot = fetch_heuristic_calibration_snapshot()
        summary = summarize_calibration_snapshot(snapshot)
    except Exception:
        return {
            "scope_count": 0,
            "event_count": 0,
            "compiled_prior_count": 0,
            "active_compiled_prior_count": 0,
            "failure_signature_count": 0,
            "review_candidate_count": 0,
        }
    return dict(summary)


def _build_retrieval_gap_review(trace_insights: dict[str, Any], *, report_mode: str) -> dict[str, Any]:
    summary = dict(trace_insights.get("retrieval_gap_summary") or {})
    calibration_summary = _calibration_snapshot_for_maintenance()
    event_count = _safe_int(calibration_summary.get("event_count") or 0)
    compiled_prior_count = _safe_int(calibration_summary.get("compiled_prior_count") or 0)
    failure_signature_count = _safe_int(calibration_summary.get("failure_signature_count") or 0)
    review_candidate_count = _safe_int(calibration_summary.get("review_candidate_count") or 0)
    recommendations: list[dict[str, Any]] = []
    if _safe_int(summary.get("route_gap_session_count") or 0) > 0:
        recommendations.append(
            {
                "kind": "inspect_route_gap",
                "priority": 0.9,
                "recommendation": "Review unresolved destinations and route-exhausted sessions before changing graph structure.",
                "evidence_source": "recent_search_sessions.route_gap_examples",
                "evidence_count": _safe_int(summary.get("route_gap_session_count") or 0),
                "review_only": True,
            }
        )
    if _safe_int(summary.get("final_eval_failure_session_count") or 0) > 0:
        recommendations.append(
            {
                "kind": "review_final_eval_failure",
                "priority": 0.88,
                "recommendation": "Inspect answerability, adequacy, contradiction, and closure blockers before promoting memories.",
                "evidence_source": "recent_search_sessions.final_eval_failure_examples",
                "evidence_count": _safe_int(summary.get("final_eval_failure_session_count") or 0),
                "review_only": True,
            }
        )
    if review_candidate_count > 0 or failure_signature_count > 0:
        recommendations.append(
            {
                "kind": "calibrate_bootstrap_prior",
                "priority": 0.84,
                "recommendation": "Use failure signatures as gated bootstrap-prior review candidates, not as automatic AI planner authority.",
                "evidence_source": "heuristic_calibration.failure_signatures",
                "evidence_count": review_candidate_count or failure_signature_count,
                "review_only": True,
            }
        )
    if compiled_prior_count > 0:
        recommendations.append(
            {
                "kind": "monitor_compiled_prior_reuse",
                "priority": 0.76,
                "recommendation": "Keep compiled priors active only while repeated grounded retrieval keeps supporting them.",
                "evidence_source": "heuristic_calibration.compiled_priors",
                "evidence_count": compiled_prior_count,
                "review_only": True,
            }
        )
    return {
        "schema_version": "agvm.maintenance.retrieval_gap_review.v1",
        "mode": report_mode,
        "review_only": True,
        "evidence_source": "recent_search_sessions.result_json",
        "session_count": _safe_int(summary.get("session_count") or 0),
        "gap_session_count": _safe_int(summary.get("gap_session_count") or 0),
        "route_gap_session_count": _safe_int(summary.get("route_gap_session_count") or 0),
        "final_eval_failure_session_count": _safe_int(summary.get("final_eval_failure_session_count") or 0),
        "maintenance_retrieval_gap_detection_ratio": _safe_float(summary.get("maintenance_retrieval_gap_detection_ratio") or 0.0),
        "route_gap_detection_ratio": _safe_float(summary.get("route_gap_detection_ratio") or 0.0),
        "heuristic_evidence_session_count": _safe_int(summary.get("heuristic_evidence_session_count") or 0),
        "ai_evidence_session_count": _safe_int(summary.get("ai_evidence_session_count") or 0),
        "branch_merge_session_count": _safe_int(summary.get("branch_merge_session_count") or 0),
        "compiled_prior_session_count": _safe_int(summary.get("compiled_prior_session_count") or 0),
        "failure_signature_session_count": _safe_int(summary.get("failure_signature_session_count") or 0),
        "gap_reasons": dict(summary.get("gap_reasons") or {}),
        "route_gap_examples": list(summary.get("route_gap_examples") or [])[:5],
        "final_eval_failure_examples": list(summary.get("final_eval_failure_examples") or [])[:5],
        "calibration_authority_boundary": {
            "scope": "bootstrap_priors_only",
            "ai_planner_authority": "observational_evidence_only",
            "failure_signatures_auto_apply": False,
            "structural_evolve_requires_maintenance_mode": True,
        },
        "post_retrieval_calibration": {
            "scope_count": _safe_int(calibration_summary.get("scope_count") or 0),
            "event_count": event_count,
            "compiled_prior_count": compiled_prior_count,
            "active_compiled_prior_count": _safe_int(calibration_summary.get("active_compiled_prior_count") or 0),
            "failure_signature_count": failure_signature_count,
            "review_candidate_count": review_candidate_count,
            "post_retrieval_calibration_gain": round(
                min(event_count, 40) / 40.0
                + min(compiled_prior_count, 8) / 80.0
                + min(review_candidate_count, 8) / 160.0,
                6,
            ),
        },
        "recommendations": recommendations[:6],
    }


def _recent_warm_thread_rows(*, limit: int = 80) -> list[dict[str, Any]]:
    from sqlite_store import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT thread_id, last_search_id, topic_signature_json, warm_packet_json, continuity_state, updated_at
            FROM warm_thread_state
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        try:
            topic_signature = json.loads(str(row["topic_signature_json"] or "{}"))
        except json.JSONDecodeError:
            topic_signature = {}
        try:
            warm_packet = json.loads(str(row["warm_packet_json"] or "{}"))
        except json.JSONDecodeError:
            warm_packet = {}
        parsed_rows.append(
            {
                "thread_id": str(row["thread_id"] or ""),
                "last_search_id": str(row["last_search_id"] or ""),
                "topic_signature": dict(topic_signature or {}),
                "warm_packet": dict(warm_packet or {}),
                "continuity_state": str(row["continuity_state"] or "low_continuity"),
                "updated_at": str(row["updated_at"] or ""),
            }
        )
    return parsed_rows


def _estimate_packet_token_pressure(packet: dict[str, Any]) -> int:
    text_values = [
        *[str(item) for item in list(packet.get("claims") or [])],
        *[str(item.get("text") or "") for item in list(packet.get("supporting_snippets") or []) if isinstance(item, dict)],
        *[str(item.get("query") or item.get("summary") or "") for item in list(packet.get("follow_up_candidates") or []) if isinstance(item, dict)],
    ]
    return int(sum(len(value) for value in text_values) / 4)


def _build_working_memory_depromotion_policy(*, report_mode: str, limit: int = 80) -> dict[str, Any]:
    rows = _recent_warm_thread_rows(limit=limit)
    ttl_hours_by_continuity = {
        "high_continuity": 72.0,
        "medium_continuity": 24.0,
        "low_continuity": 8.0,
    }
    candidates: list[dict[str, Any]] = []
    keep_hot_count = 0
    demote_count = 0
    token_pressure_count = 0
    contradiction_risk_count = 0
    for row in rows:
        packet = dict(row.get("warm_packet") or {})
        read_set = dict(packet.get("read_set") or {})
        continuity_state = str(row.get("continuity_state") or "low_continuity")
        ttl_hours = float(ttl_hours_by_continuity.get(continuity_state, ttl_hours_by_continuity["low_continuity"]))
        age_hours = _parse_iso_age_hours(row.get("updated_at"))
        confidence = _safe_float(packet.get("confidence") or 0.0)
        open_slot_count = len(list(packet.get("open_slots") or []))
        contradiction_count = len(list(packet.get("contradictions") or []))
        read_node_ids = [str(node_id or "") for node_id in list(read_set.get("node_ids") or packet.get("node_ids") or []) if str(node_id or "")]
        read_bucket_keys = [str(bucket_key or "") for bucket_key in list(read_set.get("bucket_keys") or []) if str(bucket_key or "")]
        contradiction_node_ids = [
            str(item.get("node_id") or item.get("target_node_id") or "")
            for item in list(packet.get("contradictions") or [])
            if isinstance(item, dict) and str(item.get("node_id") or item.get("target_node_id") or "")
        ]
        read_node_count = len(read_node_ids)
        read_bucket_count = len(read_bucket_keys)
        estimated_tokens = _estimate_packet_token_pressure(packet)
        reasons: list[str] = []
        if age_hours > ttl_hours:
            reasons.append("ttl_expired")
        if continuity_state == "low_continuity" and age_hours >= 2.0:
            reasons.append("low_reuse_or_divergence")
        if confidence < 0.4:
            reasons.append("low_confidence")
        if contradiction_count > 0:
            reasons.append("contradiction_risk")
            contradiction_risk_count += 1
        if estimated_tokens > 1500 or read_node_count > 20 or read_bucket_count > 20:
            reasons.append("token_pressure")
            token_pressure_count += 1
        if reasons:
            decision = "depromote_to_cold_review"
            demote_count += 1
        elif continuity_state == "high_continuity" and confidence >= 0.55:
            decision = "keep_hot"
            keep_hot_count += 1
        else:
            decision = "keep_warm_review"
        candidates.append(
            {
                "thread_id": str(row.get("thread_id") or ""),
                "last_search_id": str(row.get("last_search_id") or ""),
                "continuity_state": continuity_state,
                "age_hours": round(age_hours, 3),
                "ttl_hours": ttl_hours,
                "confidence": round(confidence, 4),
                "node_ids": read_node_ids[:24],
                "bucket_keys": read_bucket_keys[:24],
                "contradiction_node_ids": contradiction_node_ids[:12],
                "read_node_count": read_node_count,
                "read_bucket_count": read_bucket_count,
                "open_slot_count": open_slot_count,
                "contradiction_count": contradiction_count,
                "estimated_token_load": estimated_tokens,
                "decision": decision,
                "reasons": reasons,
                "review_only": True,
            }
        )
    candidates.sort(key=lambda item: (0 if item["decision"] == "depromote_to_cold_review" else 1, -len(item["reasons"]), -item["age_hours"]))
    return {
        "schema_version": "agvm.working_memory.depromotion_policy.v1",
        "mode": report_mode,
        "review_only": True,
        "evidence_source": "warm_thread_state",
        "warm_state_count": len(rows),
        "ttl_hours_by_continuity": ttl_hours_by_continuity,
        "policy_inputs": ["ttl", "reuse_continuity", "confidence", "contradiction_risk", "token_pressure"],
        "keep_hot_candidate_count": keep_hot_count,
        "depromote_candidate_count": demote_count,
        "token_pressure_candidate_count": token_pressure_count,
        "contradiction_risk_candidate_count": contradiction_risk_count,
        "depromotion_candidate_ratio": _safe_ratio(demote_count, len(rows)),
        "candidates": candidates[:10],
    }


def build_cluster_debug(graph: dict[str, Any], index_payload: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    spatial_index = dict(index_payload.get("spatial_index") or {})
    node = spatial_index.get(str(node_id))
    if not node:
        return None
    candidates, debug = find_local_candidates(node["final_position"], node["routing_brainhex"], index_payload, max_candidates=24)
    candidate_ids = [candidate["id"] for candidate in candidates]
    link_ids = [str(link["target_node_id"]) for link in node.get("links") or []]
    highway_ids = [str(link["target_node_id"]) for link in node.get("highways") or []]
    cluster_node_ids = list(dict.fromkeys([str(node_id)] + candidate_ids + link_ids + highway_ids))
    edges: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        edges.append(
            {
                "source": str(node_id),
                "target": candidate_id,
                "kind": "candidate",
                "sources": debug["candidate_sources"].get(candidate_id, []),
            }
        )
    for link in node.get("links") or []:
        edges.append({"source": str(node_id), "target": str(link["target_node_id"]), "kind": "link", "strength": float(link["strength"])})
    for highway in node.get("highways") or []:
        edges.append(
            {
                "source": str(node_id),
                "target": str(highway["target_node_id"]),
                "kind": "highway",
                "strength": float(highway["strength"]),
            }
        )
    for edge in list(graph.get("edges") or []):
        if edge.get("source_node_id") == node_id or edge.get("target_node_id") == node_id:
            edges.append(
                {
                    "source": str(edge["source_node_id"]),
                    "target": str(edge["target_node_id"]),
                    "kind": "derivation",
                    "strength": float(edge.get("confidence") or 0.0),
                    "sources": [str(edge.get("edge_type") or "derivation")],
                }
            )
            cluster_node_ids.extend([str(edge["source_node_id"]), str(edge["target_node_id"])])
    return {
        "focus_node_id": str(node_id),
        "cluster_node_ids": list(dict.fromkeys(cluster_node_ids)),
        "candidate_ids": candidate_ids,
        "origin_node_id": debug.get("suggested_origin_node_id"),
        "bucket_key": debug.get("bucket_key"),
        "candidate_sources": debug.get("candidate_sources", {}),
        "document_anchor_candidate_ids": debug.get("document_anchor_candidate_ids", []),
        "highway_expansion_ids": debug.get("highway_expansion_ids", []),
        "debug_edges": edges,
    }


def restructure_local_area(graph: dict[str, Any], focus_node_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from retrieval import ensure_index

    index_payload = ensure_index(graph)
    cluster = build_cluster_debug(graph, index_payload, focus_node_id)
    if not cluster:
        raise KeyError(focus_node_id)
    cluster_ids = set(cluster["cluster_node_ids"])
    updated_nodes = []
    graph_nodes = list(graph.get("nodes") or [])
    cluster_positions = [dict(node["final_position"]) for node in graph_nodes if node["id"] in cluster_ids]
    centroid = (
        {
            "x": sum(position["x"] for position in cluster_positions) / len(cluster_positions),
            "y": sum(position["y"] for position in cluster_positions) / len(cluster_positions),
            "z": sum(position["z"] for position in cluster_positions) / len(cluster_positions),
        }
        if cluster_positions
        else None
    )
    for node in graph_nodes:
        payload = dict(node)
        if payload["id"] in cluster_ids and centroid is not None:
            current = dict(payload["final_position"])
            payload["final_position"] = {
                "x": 0.88 * current["x"] + 0.12 * centroid["x"],
                "y": 0.88 * current["y"] + 0.12 * centroid["y"],
                "z": 0.88 * current["z"] + 0.12 * centroid["z"],
            }
            payload["topology_brainhex"] = position_to_topology_brainhex(payload["final_position"])
            payload["topology_color"] = color_from_brainhex(payload["topology_brainhex"])
            payload["bucket"] = position_to_bucket(payload["final_position"])
        updated_nodes.append(payload)
    return {
        **graph,
        "nodes": updated_nodes,
        "meta": {"graph_updated_at": utc_timestamp()},
    }, cluster


def _normalized_summary(node: dict[str, Any]) -> str:
    return " ".join(str(node.get("summary") or node.get("raw_text") or "").strip().lower().split())


def _region_key_for_node(node: dict[str, Any]) -> str:
    position = dict(node.get("final_position") or {})
    if not position:
        return ""
    return str(position_to_bucket(position, bucket_size=COARSE_BUCKET_SIZE).get("key") or "")


def _recent_maintenance_runs(*, limit: int = 6) -> list[dict[str, Any]]:
    from sqlite_store import fetch_recent_maintenance_runs

    return fetch_recent_maintenance_runs(limit=limit, include_report=False)


def _target_memory_type_for_guide_area(guide_area: str) -> str | None:
    normalized = str(guide_area or "").strip().lower()
    mapping = {
        "expression": "identity_style",
        "values": "value",
        "relationships": "relational",
        "history": "episodic",
        "projects": "project",
        "operational": "knowledge",
        "technical": "knowledge",
        "media signals": "document_anchor",
    }
    return mapping.get(normalized)


_BRIDGE_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "come",
    "con",
    "della",
    "delle",
    "degli",
    "from",
    "into",
    "over",
    "public",
    "source",
    "that",
    "the",
    "their",
    "this",
    "with",
}


def _node_bridge_text(node: dict[str, Any]) -> str:
    provenance = dict(node.get("provenance") or {})
    values = [
        node.get("summary"),
        node.get("raw_text"),
        provenance.get("source_label"),
        provenance.get("source_type"),
        provenance.get("guide_conceptual_area"),
        node.get("memory_type"),
    ]
    return " ".join(str(value or "") for value in values).strip()


def _bridge_tokens(node: dict[str, Any]) -> set[str]:
    text = _node_bridge_text(node).lower()
    tokens: set[str] = set()
    current = []
    for char in text:
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if len(token) >= 4 and token not in _BRIDGE_STOPWORDS:
                tokens.add(token)
            current = []
    if current:
        token = "".join(current)
        if len(token) >= 4 and token not in _BRIDGE_STOPWORDS:
            tokens.add(token)
    return tokens


def _bridge_years(node: dict[str, Any]) -> set[str]:
    text = _node_bridge_text(node)
    years: set[str] = set()
    for index in range(0, max(0, len(text) - 3)):
        chunk = text[index : index + 4]
        if chunk.isdigit() and (chunk.startswith("19") or chunk.startswith("20")):
            years.add(chunk)
    return years


def _node_bridge_confidence(node: dict[str, Any]) -> float:
    values = [
        _safe_float(node.get("memory_confidence") or 0.0),
        _safe_float(node.get("evidence_confidence") or 0.0),
        _safe_float(node.get("stability_confidence") or 0.0),
    ]
    values = [value for value in values if value > 0.0]
    if not values:
        return 0.55
    return max(0.0, min(1.0, sum(values) / len(values)))


def _node_is_highway_eligible(node: dict[str, Any]) -> bool:
    if str(node.get("lifecycle_status") or "active") != "active":
        return False
    claim_status = str(node.get("claim_status") or "fact").strip()
    if claim_status in {"instruction", "source_metadata", "test_artifact"}:
        return False
    if node.get("answer_eligible") is False and not bool(node.get("is_document_anchor")):
        return False
    if bool(node.get("is_document_anchor")) and node.get("document_eligible") is False:
        return False
    return _node_bridge_confidence(node) >= 0.58


def _is_document_node(node: dict[str, Any]) -> bool:
    provenance = dict(node.get("provenance") or {})
    return (
        bool(node.get("is_document_anchor"))
        or str(node.get("memory_type") or "") == "document_anchor"
        or str(node.get("node_kind") or "") == "document_anchor"
        or str(provenance.get("source_type") or "") == "document"
    )


def _bridge_area(node: dict[str, Any]) -> str:
    return str((node.get("provenance") or {}).get("guide_conceptual_area") or node.get("guide_area") or "").strip().lower()


def _bridge_memory_type(node: dict[str, Any]) -> str:
    return str(node.get("memory_type") or "").strip().lower()


def _bridge_distance_bucket_changed(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return str((left.get("bucket") or {}).get("key") or "") != str((right.get("bucket") or {}).get("key") or "")


def _has_acquisition_signal(left: dict[str, Any], right: dict[str, Any], shared_terms: set[str]) -> bool:
    text = f"{_node_bridge_text(left)} {_node_bridge_text(right)}".lower()
    acquisition_markers = (
        "acquir",
        "acquis",
        "subsidiary",
        "controllata",
        "announced",
        "merger",
        "merged",
        "rilevat",
    )
    org_context_markers = (
        "azienda",
        "societa",
        "società",
        "company",
        "corporation",
        "corp",
        "group",
        "holding",
        "subsidiary",
        "startup",
        "srl",
        "spa",
        "gmbh",
        "inc",
        "ltd",
        "llc",
        "foundation",
        "foundry",
        "studio",
        "lab",
        "labs",
        "energy",
        "sync",
        "systems",
        "software",
        "platform",
        "electric",
        "technolog",
        "solutions",
    )
    weak_shared_terms = {
        "the",
        "and",
        "for",
        "con",
        "per",
        "del",
        "della",
        "company",
        "corporation",
        "azienda",
        "societa",
        "società",
        "acquired",
        "announced",
    }
    company_like_terms = {
        term
        for term in shared_terms
        if len(term) >= 4 and term not in weak_shared_terms and not term.isdigit()
    }
    return bool(
        company_like_terms
        and any(marker in text for marker in acquisition_markers)
        and any(marker in text for marker in org_context_markers)
    )


def _sparse_bridge_kind(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    semantic: float,
    lexical: float,
    shared_terms: set[str],
    shared_years: set[str],
    trace_support: float,
) -> tuple[str | None, float]:
    left_type = _bridge_memory_type(left)
    right_type = _bridge_memory_type(right)
    left_area = _bridge_area(left)
    right_area = _bridge_area(right)
    left_document = _is_document_node(left)
    right_document = _is_document_node(right)
    if _has_acquisition_signal(left, right, shared_terms):
        return "company_acquisition_bridge", 0.62
    if (left_document and right_type in {"project", "episodic", "knowledge", "identity"}) or (
        right_document and left_type in {"project", "episodic", "knowledge", "identity"}
    ):
        return "project_document_bridge", 0.64
    if (left_type == "identity" and right_type == "project") or (right_type == "identity" and left_type == "project"):
        return "identity_project_bridge", 0.63
    if shared_years and (left_area == "history" or right_area == "history" or left_type == "episodic" or right_type == "episodic"):
        return "temporal_bridge", 0.61
    if trace_support >= 2.0:
        return "retrieval_success_bridge", 0.62
    if semantic >= 0.82 and lexical >= 0.10 and (left_area != right_area or left_type != right_type):
        return "semantic_bridge", 0.72
    return None, 1.0


def _rank_sparse_highway_candidates(
    nodes: list[dict[str, Any]],
    node_signal_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    eligible = [node for node in nodes if _node_is_highway_eligible(node)]
    ranked: list[tuple[float, str, str, dict[str, Any]]] = []
    token_cache = {str(node.get("id") or ""): _bridge_tokens(node) for node in eligible}
    year_cache = {str(node.get("id") or ""): _bridge_years(node) for node in eligible}
    for left_index, left in enumerate(eligible):
        left_id = str(left.get("id") or "")
        if not left_id:
            continue
        for right in eligible[left_index + 1 :]:
            right_id = str(right.get("id") or "")
            if not right_id or right_id == left_id:
                continue
            left_signal = dict(node_signal_map.get(left_id) or {})
            right_signal = dict(node_signal_map.get(right_id) or {})
            semantic = semantic_similarity(
                dict(left.get("routing_semantic_scores") or {}),
                dict(right.get("routing_semantic_scores") or {}),
                ROUTING_FIELDS,
            )
            lexical = lexical_overlap(_node_bridge_text(left), _node_bridge_text(right))
            shared_terms = token_cache.get(left_id, set()) & token_cache.get(right_id, set())
            shared_years = year_cache.get(left_id, set()) & year_cache.get(right_id, set())
            trace_support = (
                _safe_float(left_signal.get("match_hits") or 0.0)
                + _safe_float(right_signal.get("match_hits") or 0.0)
                + _safe_float(left_signal.get("correction_evidence_hits") or 0.0)
                + _safe_float(right_signal.get("correction_evidence_hits") or 0.0)
            )
            kind, threshold = _sparse_bridge_kind(
                left,
                right,
                semantic=semantic,
                lexical=lexical,
                shared_terms=shared_terms,
                shared_years=shared_years,
                trace_support=trace_support,
            )
            if not kind:
                continue
            confidence = (_node_bridge_confidence(left) + _node_bridge_confidence(right)) / 2.0
            distance_bonus = 0.07 if _bridge_distance_bucket_changed(left, right) else 0.0
            shared_term_bonus = min(0.10, len(shared_terms) * 0.018)
            year_bonus = min(0.08, len(shared_years) * 0.04)
            trace_bonus = min(0.10, trace_support * 0.025)
            document_bonus = 0.05 if (_is_document_node(left) or _is_document_node(right)) else 0.0
            score = (
                0.32 * max(0.0, min(1.0, semantic))
                + 0.18 * max(0.0, min(1.0, lexical))
                + 0.28 * confidence
                + distance_bonus
                + shared_term_bonus
                + year_bonus
                + trace_bonus
                + document_bonus
            )
            if score < threshold:
                continue
            reason = (
                f"{kind}; semantic={semantic:.2f}; lexical={lexical:.2f}; "
                f"confidence={confidence:.2f}; shared_terms={len(shared_terms)}; years={','.join(sorted(shared_years)) or 'none'}"
            )
            payload = {
                "source_node_id": left_id,
                "target_node_id": right_id,
                "strength": round(max(threshold, min(0.86, score)), 4),
                "reason": reason,
                "kind": kind,
                "stability": round(confidence, 4),
                "semantic": round(semantic, 4),
                "lexical": round(lexical, 4),
                "trace_support": round(trace_support, 4),
                "shared_terms": sorted(shared_terms)[:8],
                "shared_years": sorted(shared_years),
            }
            ranked.append((float(payload["strength"]), kind, f"{left_id}->{right_id}", payload))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in ranked]


def _append_highway_to_node(node: dict[str, Any], candidate: dict[str, Any], *, reverse: bool = False) -> dict[str, Any] | None:
    source_id = str(candidate.get("target_node_id") if reverse else candidate.get("source_node_id") or "")
    target_id = str(candidate.get("source_node_id") if reverse else candidate.get("target_node_id") or "")
    if not source_id or not target_id or source_id == target_id:
        return None
    highways = [
        dict(highway)
        for highway in list(node.get("highways") or [])
        if str(highway.get("target_node_id") or "").strip()
    ]
    if any(str(highway.get("target_node_id") or "") == target_id for highway in highways):
        return None
    edge = {
        "target_node_id": target_id,
        "strength": float(candidate.get("strength") or 0.0),
        "reason": str(candidate.get("reason") or ""),
        "kind": str(candidate.get("kind") or "semantic_bridge"),
        "stability": float(candidate.get("stability") or candidate.get("strength") or 0.0),
        "learned_by": "maintenance_sparse_highway_calibration",
    }
    node["highways"] = highways + [edge]
    return {
        "source_node_id": source_id,
        "target_node_id": target_id,
        "strength": edge["strength"],
        "reason": edge["reason"],
        "kind": edge["kind"],
        "stability": edge["stability"],
        "derived_from": "sparse_brain_calibration",
    }


def _promote_sparse_brain_highways(
    updated_nodes: list[dict[str, Any]],
    *,
    node_signal_map: dict[str, dict[str, Any]],
    new_highways: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    active_nodes = [node for node in updated_nodes if str(node.get("lifecycle_status") or "active") == "active"]
    existing_pairs = {
        (str(node.get("id") or ""), str(highway.get("target_node_id") or ""))
        for node in active_nodes
        for highway in list(node.get("highways") or [])
        if str(node.get("id") or "") and str(highway.get("target_node_id") or "")
    }
    sparse_brain = len(active_nodes) <= 80 or len(existing_pairs) < max(2, len(active_nodes) // 10)
    if mode not in {"evolve", "sleep_evolve"} or not sparse_brain:
        return {
            "schema_version": "agvm.pr8.sparse_highway_calibration.v1",
            "applied": False,
            "reason": "not_sparse_or_not_evolve_mode",
            "active_node_count": len(active_nodes),
            "existing_highway_count": len(existing_pairs),
            "candidate_count": 0,
            "promoted_count": 0,
            "new_highway_budget": 0,
            "kind_histogram": {},
        }
    target_total = min(16, max(4, len(active_nodes) // 3))
    budget = max(0, target_total - len(existing_pairs) - len(new_highways))
    if budget <= 0:
        return {
            "schema_version": "agvm.pr8.sparse_highway_calibration.v1",
            "applied": False,
            "reason": "highway_budget_already_satisfied",
            "active_node_count": len(active_nodes),
            "existing_highway_count": len(existing_pairs),
            "candidate_count": 0,
            "promoted_count": 0,
            "new_highway_budget": 0,
            "kind_histogram": {},
        }
    node_by_id = {str(node.get("id") or ""): node for node in updated_nodes if str(node.get("id") or "")}
    promoted: list[dict[str, Any]] = []
    kind_histogram: dict[str, int] = defaultdict(int)
    ranked_candidates = _rank_sparse_highway_candidates(active_nodes, node_signal_map)
    for candidate in ranked_candidates:
        if len(promoted) >= budget:
            break
        pair = (str(candidate["source_node_id"]), str(candidate["target_node_id"]))
        reverse_pair = (pair[1], pair[0])
        for reverse in (False, True):
            if len(promoted) >= budget:
                break
            source_id = pair[1] if reverse else pair[0]
            target_id = pair[0] if reverse else pair[1]
            if (source_id, target_id) in existing_pairs:
                continue
            source_node = node_by_id.get(source_id)
            if not source_node:
                continue
            created = _append_highway_to_node(source_node, candidate, reverse=reverse)
            if not created:
                continue
            existing_pairs.add((source_id, target_id))
            promoted.append(created)
            new_highways.append(
                {
                    **created,
                    "maintenance_mode": mode,
                    "trace_support": float(candidate.get("trace_support") or 0.0),
                    "semantic": float(candidate.get("semantic") or 0.0),
                    "lexical": float(candidate.get("lexical") or 0.0),
                    "shared_terms": list(candidate.get("shared_terms") or []),
                    "shared_years": list(candidate.get("shared_years") or []),
                }
            )
            kind_histogram[str(created.get("kind") or "semantic_bridge")] += 1
    return {
        "schema_version": "agvm.pr8.sparse_highway_calibration.v1",
        "applied": bool(promoted),
        "reason": "promoted_sparse_brain_highways" if promoted else "no_candidate_met_threshold",
        "active_node_count": len(active_nodes),
        "existing_highway_count": len(existing_pairs) - len(promoted),
        "candidate_count": len(ranked_candidates),
        "promoted_count": len(promoted),
        "new_highway_budget": budget,
        "kind_histogram": dict(kind_histogram),
        "promoted_examples": promoted[:8],
    }


def _compute_maintenance_quality(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or [])]
    total_nodes = max(1, len(nodes))
    node_map = {str(node.get("id") or ""): node for node in nodes if str(node.get("id") or "")}
    active_nodes = [node for node in nodes if str(node.get("lifecycle_status") or "active") == "active"]
    active_node_total = max(1, len(active_nodes))
    identity_count = 0
    blank_guide_area = 0
    fine_bucket_counts: dict[str, int] = defaultdict(int)
    active_highway_count = 0
    stale_highway_count = 0
    for node in nodes:
        if str(node.get("memory_type") or "") == "identity":
            identity_count += 1
        guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "").strip()
        if not guide_area:
            blank_guide_area += 1
        position = dict(node.get("final_position") or {})
        if position:
            fine_key = str(position_to_bucket(position, bucket_size=FINE_BUCKET_SIZE).get("key") or "")
            if fine_key:
                fine_bucket_counts[fine_key] += 1
        for highway in list(node.get("highways") or []):
            active_highway_count += 1
            target_node = node_map.get(str(highway.get("target_node_id") or ""))
            if target_node and str(target_node.get("lifecycle_status") or "active") != "active":
                stale_highway_count += 1
    crowded_node_count = sum(count for count in fine_bucket_counts.values() if count >= 3)
    crowded_bucket_count = sum(1 for count in fine_bucket_counts.values() if count >= 3)
    distinct_region_count = len({_region_key_for_node(node) for node in nodes if _region_key_for_node(node)})
    return {
        "total_nodes": len(nodes),
        "active_node_ratio": round(len(active_nodes) / total_nodes, 6),
        "identity_memory_ratio": round(identity_count / total_nodes, 6),
        "guide_area_blank_ratio": round(blank_guide_area / total_nodes, 6),
        "crowded_bucket_ratio": round(crowded_node_count / total_nodes, 6),
        "crowded_bucket_count": crowded_bucket_count,
        "bridge_density": round(active_highway_count / active_node_total, 6),
        "stale_highway_ratio": round(stale_highway_count / max(1, active_highway_count), 6),
        "region_coverage_ratio": round(distinct_region_count / total_nodes, 6),
    }


def _compute_quality_delta(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], float]:
    lower_is_better = (
        "identity_memory_ratio",
        "guide_area_blank_ratio",
        "crowded_bucket_ratio",
        "stale_highway_ratio",
    )
    higher_is_better = ("active_node_ratio", "bridge_density", "region_coverage_ratio")
    delta: dict[str, Any] = {}
    weighted_score = 0.0
    weights = {
        "identity_memory_ratio": 0.26,
        "guide_area_blank_ratio": 0.16,
        "crowded_bucket_ratio": 0.22,
        "stale_highway_ratio": 0.12,
        "active_node_ratio": 0.08,
        "bridge_density": 0.08,
        "region_coverage_ratio": 0.08,
    }
    for key in (*lower_is_better, *higher_is_better):
        before_value = float(before.get(key) or 0.0)
        after_value = float(after.get(key) or 0.0)
        metric_delta = round(after_value - before_value, 6)
        delta[key] = metric_delta
        if key in lower_is_better:
            weighted_score += (before_value - after_value) * float(weights.get(key) or 0.0)
        else:
            weighted_score += (after_value - before_value) * float(weights.get(key) or 0.0)
    delta["identity_improved"] = float(after.get("identity_memory_ratio") or 0.0) < float(before.get("identity_memory_ratio") or 0.0)
    delta["geometry_improved"] = float(after.get("crowded_bucket_ratio") or 0.0) < float(before.get("crowded_bucket_ratio") or 0.0)
    delta["bridge_hygiene_improved"] = float(after.get("stale_highway_ratio") or 0.0) < float(before.get("stale_highway_ratio") or 0.0)
    return delta, round(weighted_score, 6)


def _build_follow_up_candidates(
    *,
    trace_insights: dict[str, Any],
    correction_insights: dict[str, Any],
    region_actions: list[dict[str, Any]],
    quality_after: dict[str, Any],
    report_mode: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    top_bucket = next(iter(sorted((trace_insights.get("bucket_hits") or {}).items(), key=lambda item: item[1], reverse=True)), None)
    if top_bucket:
        candidates.append(
            {
                "kind": "trace_hotspot_review",
                "priority": 0.86,
                "query": f"Re-check memories around bucket {top_bucket[0]} after {report_mode}.",
                "reason": f"Recent retrieval traces revisited bucket {top_bucket[0]} {int(top_bucket[1] or 0)} times.",
            }
        )
    target_hits = dict(correction_insights.get("target_hits") or {})
    if target_hits:
        target_node_id, count = max(target_hits.items(), key=lambda item: int(item[1] or 0))
        candidates.append(
            {
                "kind": "correction_validation",
                "priority": 0.84,
                "query": f"Validate the revised memory around node {target_node_id}.",
                "reason": f"Correction history targeted node {target_node_id} {int(count or 0)} times.",
            }
        )
    if region_actions:
        region = dict(sorted(region_actions, key=lambda item: float(item.get("trace_hits") or 0.0), reverse=True)[0])
        candidates.append(
            {
                "kind": "region_stability_check",
                "priority": 0.8,
                "query": f"Inspect region {str(region.get('region_id') or 'unknown')} for residual crowding and bridge quality.",
                "reason": f"Region {str(region.get('region_id') or 'unknown')} still shows trace pressure after maintenance.",
            }
        )
    if float(quality_after.get("identity_memory_ratio") or 0.0) >= 0.4:
        candidates.append(
            {
                "kind": "identity_overconcentration_review",
                "priority": 0.88,
                "query": "Audit identity-heavy areas for project, value, style, and history retyping opportunities.",
                "reason": "Identity memories remain too concentrated after the current maintenance pass.",
            }
        )
    return candidates[:5]


def _build_proactive_opportunities(
    *,
    quality_after: dict[str, Any],
    quality_delta: dict[str, Any],
    pattern_candidates: list[dict[str, Any]],
    bridge_promotions: list[dict[str, Any]],
    bridge_demotions: list[dict[str, Any]],
    retyped_nodes: list[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    opportunities: list[dict[str, Any]] = []
    if pattern_candidates:
        opportunities.append(
            {
                "kind": "pattern_consolidation",
                "priority": 0.84,
                "summary": "Promote repeated retrieval motifs into a reusable pattern support node.",
                "reason": str((pattern_candidates[0] or {}).get("reason") or "Repeated maintenance pattern detected."),
                "derived_from": "evolve" if mode in {"evolve", "sleep_evolve"} else "sleep",
                "maintenance_mode": mode,
            }
        )
    if retyped_nodes:
        opportunities.append(
            {
                "kind": "identity_rebalance",
                "priority": 0.87,
                "summary": "Preserve the successful guide-area retyping policy as a planner prior.",
                "reason": f"{len(retyped_nodes)} nodes were retyped away from identity concentration.",
                "derived_from": "evolve",
                "maintenance_mode": mode,
            }
        )
    if bridge_demotions:
        opportunities.append(
            {
                "kind": "bridge_cleanup_followthrough",
                "priority": 0.74,
                "summary": "Run a follow-up bridge quality sweep around demoted or removed highways.",
                "reason": f"{len(bridge_demotions)} bridge/highway links were demoted or removed.",
                "derived_from": "sleep" if mode == "sleep" else "shared",
                "maintenance_mode": mode,
            }
        )
    if bridge_promotions:
        opportunities.append(
            {
                "kind": "bridge_promotion_validation",
                "priority": 0.71,
                "summary": "Validate retrieval improvement on promoted bridges.",
                "reason": f"{len(bridge_promotions)} bridge/highway links were promoted as useful routes.",
                "derived_from": "sleep" if mode == "sleep" else "shared",
                "maintenance_mode": mode,
            }
        )
    if not bool(quality_delta.get("geometry_improved")) and float(quality_after.get("crowded_bucket_ratio") or 0.0) >= 0.24:
        opportunities.append(
            {
                "kind": "geometry_rebalance",
                "priority": 0.79,
                "summary": "Schedule another evolve pass focused on crowded regions.",
                "reason": "Crowded bucket ratio remains above the preferred threshold after this pass.",
                "derived_from": "evolve",
                "maintenance_mode": mode,
            }
        )
    return opportunities[:5]


def _mode_family_for_report(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized == "sleep":
        return "sleep"
    if normalized == "evolve":
        return "evolve"
    return "shared"


def _tag_maintenance_items(items: list[dict[str, Any]], *, mode: str, default_family: str) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for item in items:
        payload = dict(item)
        payload.setdefault("maintenance_mode", mode)
        payload.setdefault("derived_from", default_family)
        tagged.append(payload)
    return tagged


def _extract_node_ids(items: list[dict[str, Any]], *keys: str) -> set[str]:
    node_ids: set[str] = set()
    for item in items:
        payload = dict(item)
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                node_ids.update(str(entry or "") for entry in value if str(entry or "").strip())
            elif str(value or "").strip():
                node_ids.add(str(value))
    return node_ids


def _build_sleep_profile(
    *,
    mode: str,
    duplicate_candidates: list[dict[str, Any]],
    alias_attachments: list[dict[str, Any]],
    confidence_updates: list[dict[str, Any]],
    bridge_promotions: list[dict[str, Any]],
    bridge_demotions: list[dict[str, Any]],
    archived_node_ids: list[str],
    superseded_node_ids: list[str],
    trace_insights: dict[str, Any],
    correction_insights: dict[str, Any],
    nucleus_refresh: dict[str, Any],
    quality_before: dict[str, Any],
    quality_after: dict[str, Any],
    quality_delta: dict[str, Any],
) -> dict[str, Any]:
    changed_node_ids = {
        *archived_node_ids,
        *superseded_node_ids,
        *_extract_node_ids(confidence_updates, "node_id"),
        *_extract_node_ids(alias_attachments, "source_node_id", "target_node_id"),
        *_extract_node_ids(bridge_promotions, "node_id", "source_node_id", "target_node_id"),
        *_extract_node_ids(bridge_demotions, "node_id", "source_node_id", "target_node_id"),
    }
    return {
        "maintenance_mode": mode,
        "profile_kind": "sleep_review",
        "conservative_review": True,
        "duplicate_review_count": len(duplicate_candidates),
        "alias_cleanup_count": len(alias_attachments),
        "confidence_revision_count": len(confidence_updates),
        "bridge_promotion_count": len(bridge_promotions),
        "bridge_demotion_count": len(bridge_demotions),
        "bridge_adjustment_count": len(bridge_promotions) + len(bridge_demotions),
        "archived_count": len(archived_node_ids),
        "superseded_count": len(superseded_node_ids),
        "nucleus_refresh_recommended": bool(nucleus_refresh.get("recommended")),
        "reviewed_change_node_count": len(changed_node_ids),
        "trace_evidence": {
            "candidate_hotspot_count": len(list(trace_insights.get("candidate_hotspots") or [])),
            "match_hotspot_count": len(list(trace_insights.get("match_hotspots") or [])),
            "bucket_hotspot_count": len(list(trace_insights.get("bucket_hotspots") or [])),
            "correction_mode_count": len(dict(correction_insights.get("mode_counts") or {})),
        },
        "quality_focus": {
            "before_identity_ratio": float(quality_before.get("identity_memory_ratio") or 0.0),
            "after_identity_ratio": float(quality_after.get("identity_memory_ratio") or 0.0),
            "before_bridge_density": float(quality_before.get("bridge_density") or 0.0),
            "after_bridge_density": float(quality_after.get("bridge_density") or 0.0),
            "identity_delta": float(quality_delta.get("identity_memory_ratio") or 0.0),
            "bridge_density_delta": float(quality_delta.get("bridge_density") or 0.0),
        },
        "changed_node_ids": sorted(changed_node_ids),
    }


def _build_evolve_profile(
    *,
    mode: str,
    retyped_nodes: list[dict[str, Any]],
    repositioned_nodes: list[dict[str, Any]],
    region_actions: list[dict[str, Any]],
    created_nodes: list[dict[str, Any]],
    new_highways: list[dict[str, Any]],
    archived_node_ids: list[str],
    superseded_node_ids: list[str],
    quality_before: dict[str, Any],
    quality_after: dict[str, Any],
    quality_delta: dict[str, Any],
) -> dict[str, Any]:
    changed_node_ids = {
        *archived_node_ids,
        *superseded_node_ids,
        *_extract_node_ids(retyped_nodes, "node_id"),
        *_extract_node_ids(repositioned_nodes, "node_id"),
        *_extract_node_ids(created_nodes, "node_id"),
        *_extract_node_ids(new_highways, "source_node_id", "target_node_id"),
    }
    return {
        "maintenance_mode": mode,
        "profile_kind": "evolve_structure",
        "structural_reorganization": True,
        "retyped_count": len(retyped_nodes),
        "repositioned_count": len(repositioned_nodes),
        "region_action_count": len(region_actions),
        "created_node_count": len(created_nodes),
        "new_highway_count": len(new_highways),
        "archived_count": len(archived_node_ids),
        "superseded_count": len(superseded_node_ids),
        "reviewed_change_node_count": len(changed_node_ids),
        "quality_focus": {
            "before_crowded_bucket_ratio": float(quality_before.get("crowded_bucket_ratio") or 0.0),
            "after_crowded_bucket_ratio": float(quality_after.get("crowded_bucket_ratio") or 0.0),
            "before_region_coverage_ratio": float(quality_before.get("region_coverage_ratio") or 0.0),
            "after_region_coverage_ratio": float(quality_after.get("region_coverage_ratio") or 0.0),
            "geometry_delta": float(quality_delta.get("crowded_bucket_ratio") or 0.0),
            "region_coverage_delta": float(quality_delta.get("region_coverage_ratio") or 0.0),
        },
        "changed_node_ids": sorted(changed_node_ids),
    }


def _build_mode_overlap_summary(
    *,
    sleep_profile: dict[str, Any],
    evolve_profile: dict[str, Any],
) -> dict[str, Any]:
    sleep_nodes = {str(node_id) for node_id in list(sleep_profile.get("changed_node_ids") or []) if str(node_id).strip()}
    evolve_nodes = {str(node_id) for node_id in list(evolve_profile.get("changed_node_ids") or []) if str(node_id).strip()}
    union = sleep_nodes | evolve_nodes
    overlap = sleep_nodes & evolve_nodes
    return {
        "sleep_changed_node_count": len(sleep_nodes),
        "evolve_changed_node_count": len(evolve_nodes),
        "overlap_node_count": len(overlap),
        "overlap_ratio": round(len(overlap) / max(1, len(union)), 6),
        "overlap_node_ids": sorted(overlap)[:12],
        "sleep_only_node_ids": sorted(sleep_nodes - evolve_nodes)[:12],
        "evolve_only_node_ids": sorted(evolve_nodes - sleep_nodes)[:12],
    }


def _build_mode_specific_quality_delta(
    *,
    mode: str,
    quality_delta: dict[str, Any],
    overall_quality_delta_score: float,
    sleep_profile: dict[str, Any],
    evolve_profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "current_mode": mode,
        "overall_quality_delta_score": round(float(overall_quality_delta_score or 0.0), 6),
        "sleep_review_focus": {
            "confidence_revision_count": int(sleep_profile.get("confidence_revision_count") or 0),
            "bridge_adjustment_count": int(sleep_profile.get("bridge_adjustment_count") or 0),
            "archived_count": int(sleep_profile.get("archived_count") or 0),
            "superseded_count": int(sleep_profile.get("superseded_count") or 0),
            "identity_delta": float(quality_delta.get("identity_memory_ratio") or 0.0),
        },
        "evolve_structure_focus": {
            "retyped_count": int(evolve_profile.get("retyped_count") or 0),
            "repositioned_count": int(evolve_profile.get("repositioned_count") or 0),
            "new_highway_count": int(evolve_profile.get("new_highway_count") or 0),
            "created_node_count": int(evolve_profile.get("created_node_count") or 0),
            "geometry_delta": float(quality_delta.get("crowded_bucket_ratio") or 0.0),
            "region_coverage_delta": float(quality_delta.get("region_coverage_ratio") or 0.0),
        },
    }


def _summarize_maintenance_history(recent_runs: list[dict[str, Any]], current_report: dict[str, Any]) -> dict[str, Any]:
    all_runs = list(recent_runs) + [
        {
            "mode": str(current_report.get("mode") or ""),
            "applied": bool(current_report.get("applied")),
            "report": current_report,
        }
    ]
    applied_runs = [run for run in all_runs if bool(run.get("applied"))]
    mode_histogram: dict[str, int] = defaultdict(int)
    scores: list[float] = []
    proactive_runs = 0
    for run in all_runs:
        mode_histogram[str(run.get("mode") or "unknown")] += 1
        report = dict(run.get("report") or {})
        scores.append(float(report.get("overall_quality_delta_score") or 0.0))
        if list(report.get("follow_up_candidates") or []) or list(report.get("proactive_opportunities") or []):
            proactive_runs += 1
    if len(scores) >= 2 and scores[-1] > scores[0]:
        trend = "improving"
    elif len(scores) >= 2 and scores[-1] < scores[0]:
        trend = "regressing"
    else:
        trend = "mixed"
    return {
        "recent_run_count": len(all_runs),
        "applied_run_count": len(applied_runs),
        "mode_histogram": dict(mode_histogram),
        "recent_quality_scores": [round(score, 6) for score in scores[-5:]],
        "proactive_run_count": proactive_runs,
        "repeated_evidence_ready": len(applied_runs) >= 2,
        "trend": trend,
    }


def _bounded_nucleus_graph_for_fast_preview(
    graph: dict[str, Any],
    *,
    working_nodes: list[dict[str, Any]],
    max_nodes: int = 96,
    max_text_chars: int = 1200,
) -> dict[str, Any]:
    """Build a small identity-review graph for fast maintenance previews.

    Full identity nucleus reconstruction scans relation cues over the whole
    graph. That is valuable for deep preview/apply, but the fast MCP
    maintenance path only needs bounded support counts and representative
    candidates, so it should not pay full-graph latency.
    """

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def bounded_node(node: dict[str, Any]) -> dict[str, Any]:
        payload = dict(node)
        for field in ("raw_text", "summary"):
            text = str(payload.get(field) or "")
            if len(text) > max_text_chars:
                payload[field] = text[:max_text_chars]
        provenance = dict(payload.get("provenance") or {})
        for field in ("source_label", "source_unit_title"):
            text = str(provenance.get(field) or "")
            if len(text) > 240:
                provenance[field] = text[:240]
        if provenance:
            payload["provenance"] = provenance
        return payload

    def add(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or "").strip()
        if not node_id or node_id in seen:
            return
        seen.add(node_id)
        selected.append(bounded_node(node))

    for node in working_nodes:
        add(dict(node))

    priority_types = {
        "identity": 8,
        "value": 7,
        "identity_style": 7,
        "relational": 6,
        "project": 5,
        "document_anchor": 4,
        "document_fact": 3,
        "document_summary": 3,
        "knowledge": 2,
        "episodic": 2,
    }
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for node in list(graph.get("nodes") or []):
        node_id = str(node.get("id") or "").strip()
        if not node_id or node_id in seen:
            continue
        memory_type = str(node.get("memory_type") or "").strip()
        priority = priority_types.get(memory_type, 0)
        if priority <= 0:
            continue
        score = float(priority) + float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0)
        ranked.append((score, node_id, dict(node)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    for _score, _node_id, node in ranked:
        if len(selected) >= max_nodes:
            break
        add(node)
    return {**graph, "nodes": selected}


def _recent_trace_insights(*, limit: int = 240) -> dict[str, Any]:
    from sqlite_store import connect, fetch_recent_search_sessions

    candidate_hits: dict[str, int] = {}
    match_hits: dict[str, int] = {}
    bucket_hits: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT event_type, payload_json
            FROM search_events
            ORDER BY seq DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    for row in rows:
        event_type = str(row["event_type"] or "")
        payload = json.loads(str(row["payload_json"] or "{}"))
        if event_type == "step_complete":
            bucket_key = str(payload.get("bucket_key") or "")
            if bucket_key:
                bucket_hits[bucket_key] = bucket_hits.get(bucket_key, 0) + 1
            for node_id in list(payload.get("candidate_ids") or []):
                node_key = str(node_id or "")
                if node_key:
                    candidate_hits[node_key] = candidate_hits.get(node_key, 0) + 1
            for match in list(payload.get("matches") or []):
                node_key = str((match or {}).get("node_id") or "")
                if node_key:
                    match_hits[node_key] = match_hits.get(node_key, 0) + 1
        elif event_type == "search_stopped":
            stop_reason = str(payload.get("stop_reason") or "")
            if stop_reason:
                stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1
    recent_sessions = fetch_recent_search_sessions(
        limit=max(1, min(40, int(limit or 40))),
        include_result=False,
        read_only=True,
        busy_timeout_ms=2000,
    )
    retrieval_gap_summary = _build_retrieval_gap_summary(recent_sessions)
    return {
        "candidate_hits": candidate_hits,
        "match_hits": match_hits,
        "bucket_hits": bucket_hits,
        "stop_reasons": stop_reasons,
        "retrieval_gap_summary": retrieval_gap_summary,
    }


def _recent_correction_insights(*, limit: int = 60) -> dict[str, Any]:
    from sqlite_store import fetch_correction_history

    target_hits: dict[str, int] = defaultdict(int)
    target_modes: dict[str, dict[str, int]] = defaultdict(dict)
    evidence_hits: dict[str, int] = defaultdict(int)
    mode_counts: dict[str, int] = defaultdict(int)
    recent_actions: list[dict[str, Any]] = []
    corrections = fetch_correction_history(limit=limit)
    for correction in corrections:
        mode = str(correction.get("correction_mode") or "").strip() or "unknown"
        mode_counts[mode] += 1
        targets = [str(node_id or "") for node_id in list(correction.get("target_node_ids") or []) if str(node_id or "")]
        evidence_nodes = [
            str(node_id or "") for node_id in list(correction.get("used_evidence_node_ids") or []) if str(node_id or "")
        ]
        for node_id in targets:
            target_hits[node_id] += 1
            node_modes = target_modes.setdefault(node_id, {})
            node_modes[mode] = int(node_modes.get(mode, 0)) + 1
        for node_id in evidence_nodes:
            evidence_hits[node_id] += 1
        if len(recent_actions) < 10:
            recent_actions.append(
                {
                    "correction_id": str(correction.get("correction_id") or ""),
                    "mode": mode,
                    "target_node_ids": targets[:6],
                    "used_evidence_node_ids": evidence_nodes[:6],
                    "created_at": str(correction.get("created_at") or ""),
                }
            )
    return {
        "target_hits": dict(target_hits),
        "target_modes": {node_id: dict(modes) for node_id, modes in target_modes.items()},
        "evidence_hits": dict(evidence_hits),
        "mode_counts": dict(mode_counts),
        "recent_actions": recent_actions,
    }


def _build_pattern_node(
    *,
    graph: dict[str, Any],
    raw_text: str,
    summary: str,
    fixed_id: str,
) -> dict[str, Any]:
    from derivation import build_seed
    from retrieval import ensure_index, finalize_node

    seed = build_seed(
        raw_text=raw_text,
        input_mode="auto",
        provenance_mode="maintenance_pattern",
        source_label="maintenance_pattern",
        source_type="system_pattern",
        source_trust="system_metadata",
        claim_status="source_metadata",
        summary_override=summary,
        guide_area_override="Patterns",
        memory_confidence=0.72,
        evidence_confidence=0.7,
        stability_confidence=0.66,
        novelty_override=0.45,
        granularity_override=0.44,
    )
    index_payload = ensure_index(graph)
    return finalize_node(seed, graph, index_payload, fixed_id=fixed_id)


def _normalized_pattern_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


_PATTERN_BUCKET_RE = re.compile(r"\bbucket\s+([-0-9:]+)\b", re.IGNORECASE)


def _top_trace_bucket(trace_insights: dict[str, Any] | None) -> str:
    bucket_hits = dict((trace_insights or {}).get("bucket_hits") or {})
    if not bucket_hits:
        return ""
    top_bucket = next(iter(sorted(bucket_hits.items(), key=lambda item: int(item[1] or 0), reverse=True)), None)
    return str(top_bucket[0] or "") if top_bucket else ""


def _bucket_from_pattern_reason(reason: str) -> str:
    match = _PATTERN_BUCKET_RE.search(str(reason or ""))
    return str(match.group(1) or "") if match else ""


def _pattern_signature(
    candidate: dict[str, Any],
    *,
    trace_insights: dict[str, Any] | None = None,
    mode: str = "",
) -> str:
    kind = _normalized_pattern_text(candidate.get("kind") or "maintenance_pattern")
    reason = _normalized_pattern_text(candidate.get("reason") or "repeated retrieval pattern")
    explicit_signature = _normalized_pattern_text(candidate.get("pattern_signature") or candidate.get("idempotency_signature"))
    if explicit_signature:
        return explicit_signature
    if kind == "trace_alignment_pattern":
        bucket_key = (
            str(candidate.get("bucket_key") or "").strip()
            or _bucket_from_pattern_reason(str(candidate.get("reason") or ""))
            or _top_trace_bucket(trace_insights)
        )
        return f"{kind}::bucket::{bucket_key}" if bucket_key else f"{kind}::global_trace_alignment::{_normalized_pattern_text(mode)}"
    if kind == "duplicate_motif":
        return f"{kind}::near_duplicate_cluster"
    return f"{kind}::{reason}"


def _pattern_node_id(candidate: dict[str, Any], *, trace_insights: dict[str, Any] | None = None, mode: str = "") -> str:
    digest = hashlib.sha256(_pattern_signature(candidate, trace_insights=trace_insights, mode=mode).encode("utf-8")).hexdigest()[:16]
    return f"pattern::{digest}"


def _find_existing_pattern_node(
    graph: dict[str, Any],
    *,
    node_id: str,
    signature: str,
    summary: str,
) -> dict[str, Any] | None:
    normalized_summary = _normalized_pattern_text(summary)
    normalized_signature = _normalized_pattern_text(signature)
    for node in list(graph.get("nodes") or []):
        current_id = str(node.get("id") or "")
        if current_id == node_id:
            return dict(node)
        if not current_id.startswith("pattern::"):
            continue
        provenance = dict(node.get("provenance") or {})
        if str(provenance.get("mode") or "") != "maintenance_pattern":
            continue
        provenance_signature = _normalized_pattern_text(
            provenance.get("maintenance_pattern_signature")
            or provenance.get("pattern_signature")
            or provenance.get("idempotency_signature")
            or provenance.get("maintenance_idempotency_key")
        )
        if normalized_signature and provenance_signature == normalized_signature:
            return dict(node)
        if normalized_summary and _normalized_pattern_text(node.get("summary") or "") == normalized_summary:
            return dict(node)
    return None


def _document_anchor_guard(before_graph: dict[str, Any], after_graph: dict[str, Any]) -> dict[str, Any]:
    before_ids = {
        str(node.get("id") or "")
        for node in list(before_graph.get("nodes") or [])
        if bool(node.get("is_document_anchor")) and str(node.get("id") or "")
    }
    after_ids = {
        str(node.get("id") or "")
        for node in list(after_graph.get("nodes") or [])
        if bool(node.get("is_document_anchor")) and str(node.get("id") or "")
    }
    missing = sorted(before_ids - after_ids)
    return {
        "schema_version": "agvm.pr9.document_anchor_guard.v1",
        "before_count": len(before_ids),
        "after_count": len(after_ids),
        "preserved_count": len(before_ids & after_ids),
        "missing_document_anchor_ids": missing,
        "raw_document_anchor_delete_blocked": not missing,
        "policy": "document_anchors_are_not_deleted_by_maintenance",
    }


def sleep_evolve_graph(
    graph: dict[str, Any],
    *,
    preview_only: bool = True,
    focus_node_id: str | None = None,
    max_nodes_considered: int = 80,
    mode: str = "sleep_evolve",
) -> tuple[dict[str, Any], dict[str, Any]]:
    from derivation import build_identity_nucleus
    from geometry_calibration import build_brain_geometry_calibration_report
    from sqlite_store import rebuild_region_summaries

    preview_budget_guard = _preview_node_budget_guard(
        mode=mode,
        preview_only=preview_only,
        max_nodes_considered=max_nodes_considered,
    )
    max_nodes_considered = int(preview_budget_guard["effective_max_nodes_considered"])
    quality_before = _compute_maintenance_quality(graph)
    trace_insights = _recent_trace_insights(limit=40 if preview_only and max_nodes_considered <= 30 else 120 if preview_only else 240)
    correction_insights = _recent_correction_insights()
    nodes = list(graph.get("nodes") or [])
    ingest_learning_review = build_ingest_learning_review(
        graph,
        mode=mode,
        preview_only=preview_only,
        max_events=180 if preview_only else 400,
        effective_node_limit=max_nodes_considered,
    )
    maintenance_preview_plan = _build_maintenance_preview_plan(
        graph,
        mode=mode,
        preview_only=preview_only,
        focus_node_id=focus_node_id,
        preview_budget_guard=preview_budget_guard,
        trace_insights=trace_insights,
        correction_insights=correction_insights,
        ingest_learning_review=ingest_learning_review,
    )
    preview_budget_guard = {
        **preview_budget_guard,
        "preview_depth": maintenance_preview_plan.get("preview_depth"),
        "selected_node_count": maintenance_preview_plan.get("selected_node_count"),
        "selected_region_count": maintenance_preview_plan.get("selected_region_count"),
        "deferred_node_count": maintenance_preview_plan.get("deferred_node_count"),
        "deferred_chunk_count": maintenance_preview_plan.get("deferred_chunk_count"),
        "recommended_follow_up_chunks": maintenance_preview_plan.get("recommended_follow_up_chunks"),
    }
    fast_preview = bool(preview_only and str(maintenance_preview_plan.get("preview_depth")) == "fast_scan")
    chunked_preview = bool(preview_only and str(maintenance_preview_plan.get("preview_depth")) == "chunked_preview")
    if not preview_only:
        calibration_node_budget = min(2500, max(120, int(max_nodes_considered or 80) * 30))
    elif fast_preview:
        calibration_node_budget = min(240, max(120, int(max_nodes_considered or 80) * 8))
    elif chunked_preview:
        calibration_node_budget = min(
            max(240, _safe_int(os.environ.get("AGVM_CHUNKED_PREVIEW_CALIBRATION_MAX_NODES"), 720)),
            max(160, int(max_nodes_considered or 80) * 6),
        )
    else:
        calibration_node_budget = min(
            max(360, _safe_int(os.environ.get("AGVM_DEEP_PREVIEW_CALIBRATION_MAX_NODES"), 1200)),
            max(240, int(max_nodes_considered or 80) * 12),
        )
    maintenance_preview_plan["calibration_node_budget"] = calibration_node_budget
    brain_geometry_calibration_before = build_brain_geometry_calibration_report(graph, max_nodes=calibration_node_budget)
    if focus_node_id:
        focus_ids = {focus_node_id}
        cluster_nodes = [node for node in nodes if node.get("id") in focus_ids]
        working_nodes = cluster_nodes or nodes
    else:
        selected_ids = set(str(node_id) for node_id in list(maintenance_preview_plan.get("selected_node_ids") or []))
        working_nodes = [node for node in nodes if str(node.get("id") or "") in selected_ids] or nodes
    working_nodes = working_nodes[:max_nodes_considered]
    working_node_ids = {str(node.get("id") or "") for node in working_nodes if str(node.get("id") or "")}

    duplicate_candidates: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    confidence_updates: list[dict[str, Any]] = []
    highway_changes: list[dict[str, Any]] = []
    pattern_candidates: list[dict[str, Any]] = []
    created_nodes: list[dict[str, Any]] = []
    repositioned_nodes: list[dict[str, Any]] = []
    retyped_nodes: list[dict[str, Any]] = []
    new_highways: list[dict[str, Any]] = []
    region_actions: list[dict[str, Any]] = []
    deleted_node_ids: list[str] = []
    archived_node_ids: list[str] = []
    superseded_node_ids: list[str] = []
    region_summary_map = {
        str(summary.get("region_id") or ""): dict(summary)
        for summary in (
            rebuild_region_summaries()
            if mode == "evolve"
            and not preview_only
            else []
        )
        if str(summary.get("region_id") or "")
    }
    retrieval_gap_review = _build_retrieval_gap_review(trace_insights, report_mode=mode)
    working_memory_depromotion_policy = _build_working_memory_depromotion_policy(report_mode=mode)

    keep_ids: set[str] = set(node["id"] for node in nodes)
    redirect_map: dict[str, str] = {}
    seen: list[dict[str, Any]] = []
    for node in working_nodes:
        normalized = _normalized_summary(node)
        target = None
        for candidate in seen:
            overlap = lexical_overlap(normalized, _normalized_summary(candidate))
            if normalized and (_normalized_summary(candidate) == normalized or overlap >= 0.9):
                target = candidate
                duplicate_candidates.append(
                    {
                        "source_node_id": node["id"],
                        "target_node_id": target["id"],
                        "confidence": round(max(0.82, overlap), 4),
                        "reason": "high lexical overlap",
                    }
                )
                break
        if target:
            if _is_document_node(node):
                duplicate_candidates.append(
                    {
                        "source_node_id": node["id"],
                        "target_node_id": target["id"],
                        "confidence": 1.0,
                        "reason": "duplicate_document_anchor_review_only",
                        "blocked_by": "document_anchor_preservation_policy",
                    }
                )
                seen.append(node)
                continue
            redirect_map[node["id"]] = target["id"]
            keep_ids.discard(node["id"])
            deleted_node_ids.append(str(node["id"]))
            merges.append({"source_node_id": node["id"], "target_node_id": target["id"], "reason": "dedupe_duplicate_summary"})
        else:
            seen.append(node)

    working_positions = [dict(node.get("final_position") or {}) for node in working_nodes if node.get("final_position")]
    centroid = (
        {
            "x": sum(float(position.get("x") or 0.0) for position in working_positions) / len(working_positions),
            "y": sum(float(position.get("y") or 0.0) for position in working_positions) / len(working_positions),
            "z": sum(float(position.get("z") or 0.0) for position in working_positions) / len(working_positions),
        }
        if working_positions
        else None
    )

    updated_nodes: list[dict[str, Any]] = []
    node_signal_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        if preview_only and node_id not in working_node_ids:
            updated_nodes.append(dict(node))
            continue
        if node["id"] not in keep_ids:
            continue
        payload = dict(node)
        payload["sleep_revision_count"] = int(payload.get("sleep_revision_count") or 0) + (0 if preview_only else 1)
        payload["last_sleep_review_at"] = utc_timestamp()
        candidate_hits = int(trace_insights["candidate_hits"].get(str(payload["id"]), 0))
        match_hits = int(trace_insights["match_hits"].get(str(payload["id"]), 0))
        correction_target_hits = int(correction_insights["target_hits"].get(str(payload["id"]), 0))
        correction_evidence_hits = int(correction_insights["evidence_hits"].get(str(payload["id"]), 0))
        correction_modes = dict(correction_insights["target_modes"].get(str(payload["id"]), {}) or {})
        region_id = _region_key_for_node(payload)
        region_summary = dict(region_summary_map.get(region_id) or {})
        region_usefulness = dict(region_summary.get("retrieval_usefulness") or {})
        region_trace_hits = int(region_usefulness.get("trace_hits") or 0)
        region_crowding = str(region_summary.get("crowding_severity") or "")
        old_conf = float(payload.get("stability_confidence") or 0.55)
        confidence_delta = 0.08 if payload["id"] not in redirect_map.values() else 0.12
        if match_hits >= 2:
            confidence_delta += 0.05
        elif candidate_hits >= 3:
            confidence_delta += 0.02
        if correction_evidence_hits:
            confidence_delta += min(0.06, 0.02 * correction_evidence_hits)
        if correction_target_hits:
            confidence_delta -= min(0.14, 0.05 * correction_target_hits)
        if region_trace_hits >= 4 and match_hits >= 1:
            confidence_delta += 0.03
        new_conf = min(1.0, max(0.05, old_conf + confidence_delta))
        payload["stability_confidence"] = round(new_conf, 4)
        confidence_updates.append(
            {
                "node_id": payload["id"],
                "field": "stability_confidence",
                "from": round(old_conf, 4),
                "to": round(new_conf, 4),
                "candidate_hits": candidate_hits,
                "match_hits": match_hits,
                "correction_target_hits": correction_target_hits,
                "correction_evidence_hits": correction_evidence_hits,
            }
        )
        lifecycle_status = str(payload.get("lifecycle_status") or "active")
        guide_area = str((payload.get("provenance") or {}).get("guide_conceptual_area") or "").strip()
        target_memory_type = _target_memory_type_for_guide_area(guide_area)
        if (
            mode in {"evolve", "sleep_evolve"}
            and lifecycle_status == "active"
            and str(payload.get("memory_type") or "") == "identity"
            and target_memory_type
            and target_memory_type != "identity"
            and target_memory_type != "document_anchor"
            and (match_hits >= 1 or candidate_hits >= 2 or correction_target_hits >= 1 or correction_evidence_hits >= 1 or region_trace_hits >= 2)
        ):
            original_memory_type = str(payload.get("memory_type") or "identity")
            payload["memory_type"] = target_memory_type
            retyped_nodes.append(
                {
                    "node_id": payload["id"],
                    "from": original_memory_type,
                    "to": target_memory_type,
                    "guide_area": guide_area,
                    "reason": "guide_area_aligned_evolve_retype",
                }
            )
        node_signal_map[str(payload["id"])] = {
            "candidate_hits": candidate_hits,
            "match_hits": match_hits,
            "correction_target_hits": correction_target_hits,
            "correction_evidence_hits": correction_evidence_hits,
            "region_trace_hits": region_trace_hits,
            "region_id": region_id,
            "guide_area": guide_area,
            "memory_type": str(payload.get("memory_type") or ""),
            "lifecycle_status": str(payload.get("lifecycle_status") or lifecycle_status or "active"),
            "is_document_anchor": _is_document_node(payload),
        }
        if correction_modes.get("delete") and not _is_document_node(payload):
            keep_ids.discard(payload["id"])
            deleted_node_ids.append(str(payload["id"]))
            continue
        if correction_modes.get("replace") or correction_modes.get("supersede"):
            if lifecycle_status == "active" and not _is_document_node(payload):
                payload["lifecycle_status"] = "superseded"
                payload["valid_to"] = payload.get("valid_to") or utc_timestamp()
                superseded_node_ids.append(str(payload["id"]))
                lifecycle_status = "superseded"
        elif correction_modes.get("archive") or (
            mode in {"sleep", "sleep_evolve"}
            and candidate_hits >= 5
            and match_hits == 0
            and old_conf < 0.58
            and not _is_document_node(payload)
            and lifecycle_status == "active"
        ):
            payload["lifecycle_status"] = "archived"
            archived_node_ids.append(str(payload["id"]))
            lifecycle_status = "archived"
        cleaned_highways = []
        for highway in payload.get("highways") or []:
            target = redirect_map.get(str(highway.get("target_node_id")), str(highway.get("target_node_id")))
            if target == payload["id"]:
                highway_changes.append({"node_id": payload["id"], "change": "removed_self_highway"})
                continue
            old_strength = float(highway.get("strength") or 0.0)
            next_strength = old_strength
            change_kind = ""
            if lifecycle_status != "active" or (candidate_hits >= 4 and match_hits == 0):
                next_strength = max(0.0, old_strength - 0.14)
                change_kind = "demoted_trace_noise"
            elif match_hits >= 2 or correction_evidence_hits >= 1:
                next_strength = min(1.0, old_strength + 0.08)
                change_kind = "promoted_trace_match"
            if next_strength < 0.18 and (candidate_hits >= 5 or lifecycle_status != "active"):
                highway_changes.append(
                    {
                        "node_id": payload["id"],
                        "target_node_id": target,
                        "change": "removed_low_signal_highway",
                        "from": round(old_strength, 4),
                        "to": 0.0,
                    }
                )
                continue
            if change_kind and abs(next_strength - old_strength) >= 1e-6:
                highway_changes.append(
                    {
                        "node_id": payload["id"],
                        "target_node_id": target,
                        "change": change_kind,
                        "from": round(old_strength, 4),
                        "to": round(next_strength, 4),
                    }
                )
            cleaned = {**highway, "target_node_id": target, "strength": round(next_strength, 4)}
            cleaned_highways.append(cleaned)
        unique_highways: dict[str, dict[str, Any]] = {}
        for cleaned in cleaned_highways:
            target_key = str(cleaned.get("target_node_id") or "")
            if not target_key:
                continue
            existing_highway = unique_highways.get(target_key)
            if existing_highway is None or float(cleaned.get("strength") or 0.0) >= float(existing_highway.get("strength") or 0.0):
                unique_highways[target_key] = cleaned
        payload["highways"] = list(unique_highways.values())
        if mode in {"evolve", "sleep_evolve"} and centroid is not None and (candidate_hits >= 2 or match_hits >= 1 or region_trace_hits >= 4):
            current = dict(payload.get("final_position") or {})
            if current:
                shift = 0.06 + 0.02 * min(candidate_hits, 4)
                shift += 0.02 * min(match_hits, 3)
                if region_crowding == "high":
                    shift += 0.06
                elif region_crowding == "medium":
                    shift += 0.03
                if lifecycle_status != "active":
                    shift += 0.03
                shift = min(0.28, shift)
                next_position = {
                    "x": float(current.get("x") or 0.0) + (float(current.get("x") or 0.0) - float(centroid["x"])) * shift,
                    "y": float(current.get("y") or 0.0) + (float(current.get("y") or 0.0) - float(centroid["y"])) * shift,
                    "z": float(current.get("z") or 0.0) + (float(current.get("z") or 0.0) - float(centroid["z"])) * shift,
                }
                payload["final_position"] = next_position
                payload["topology_brainhex"] = position_to_topology_brainhex(next_position)
                payload["topology_color"] = color_from_brainhex(payload["topology_brainhex"])
                payload["bucket"] = position_to_bucket(next_position)
                repositioned_nodes.append(
                    {
                        "node_id": payload["id"],
                        "reason": "trace_guided_rebalance",
                        "candidate_hits": candidate_hits,
                        "match_hits": match_hits,
                        "region_id": region_id,
                        "crowding_severity": region_crowding,
                    }
                )
        updated_nodes.append(payload)

    route_eligible_node_ids = {str(node.get("id") or "") for node in updated_nodes if is_answer_eligible(node)}
    for payload in updated_nodes:
        payload_id = str(payload.get("id") or "")
        if preview_only and payload_id not in working_node_ids:
            continue
        if payload_id not in route_eligible_node_ids:
            if list(payload.get("highways") or []):
                highway_changes.append(
                    {
                        "node_id": payload_id,
                        "change": "removed_system_metadata_highways",
                        "count": len(list(payload.get("highways") or [])),
                    }
                )
            payload["highways"] = []
            continue
        retained_highways = [
            highway
            for highway in list(payload.get("highways") or [])
            if str(highway.get("target_node_id") or "") in route_eligible_node_ids
        ]
        if len(retained_highways) != len(list(payload.get("highways") or [])):
            highway_changes.append(
                {
                    "node_id": payload_id,
                    "change": "removed_ineligible_highway_targets",
                    "from": len(list(payload.get("highways") or [])),
                    "to": len(retained_highways),
                }
            )
        payload["highways"] = retained_highways

    if mode in {"evolve", "sleep_evolve"}:
        ranked_nodes: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for payload in updated_nodes:
            signal = dict(node_signal_map.get(str(payload.get("id") or "")) or {})
            if (
                not signal
                or signal.get("lifecycle_status") != "active"
                or bool(signal.get("is_document_anchor"))
                or str(payload.get("id") or "") not in route_eligible_node_ids
            ):
                continue
            score = (
                float(signal.get("match_hits") or 0.0) * 3.0
                + float(signal.get("candidate_hits") or 0.0)
                + float(signal.get("correction_evidence_hits") or 0.0) * 2.0
                + float(signal.get("region_trace_hits") or 0.0) * 0.5
            )
            if score <= 0.0:
                continue
            ranked_nodes.append((score, payload, signal))
        ranked_nodes.sort(key=lambda item: item[0], reverse=True)
        new_highway_budget = 2 if mode == "evolve" else 1
        for score, source_payload, source_signal in ranked_nodes[:6]:
            if len(new_highways) >= new_highway_budget:
                break
            existing_targets = {
                str(highway.get("target_node_id") or "")
                for highway in list(source_payload.get("highways") or [])
                if str(highway.get("target_node_id") or "").strip()
            }
            for target_score, target_payload, target_signal in ranked_nodes[:6]:
                if len(new_highways) >= new_highway_budget:
                    break
                if source_payload["id"] == target_payload["id"]:
                    continue
                if str(target_payload["id"]) in existing_targets:
                    continue
                same_region = str(source_signal.get("region_id") or "") == str(target_signal.get("region_id") or "")
                same_memory_type = str(source_signal.get("memory_type") or "") == str(target_signal.get("memory_type") or "")
                same_guide_area = str(source_signal.get("guide_area") or "") == str(target_signal.get("guide_area") or "")
                if same_region and same_memory_type and same_guide_area:
                    continue
                route_support = (
                    float(source_signal.get("match_hits") or 0.0)
                    + float(target_signal.get("match_hits") or 0.0)
                    + float(source_signal.get("correction_evidence_hits") or 0.0)
                    + float(target_signal.get("correction_evidence_hits") or 0.0)
                )
                if route_support < 2.0 and score + target_score < 9.0:
                    continue
                new_strength = round(
                    min(
                        0.74,
                        0.26
                        + 0.04 * route_support
                        + 0.02 * min(
                            float(source_signal.get("candidate_hits") or 0.0)
                            + float(target_signal.get("candidate_hits") or 0.0),
                            6.0,
                        ),
                    ),
                    4,
                )
                source_payload["highways"] = list(source_payload.get("highways") or []) + [
                    {
                        "target_node_id": str(target_payload["id"]),
                        "strength": new_strength,
                        "learned_by": "maintenance_evolve",
                    }
                ]
                new_highways.append(
                    {
                        "source_node_id": str(source_payload["id"]),
                        "target_node_id": str(target_payload["id"]),
                        "strength": new_strength,
                        "reason": "trace_guided_evolve_highway",
                        "trace_support": round(route_support, 4),
                        "derived_from": "evolve",
                        "maintenance_mode": mode,
                    }
                )
                break

    highway_scope_nodes = [node for node in updated_nodes if str(node.get("id") or "") in working_node_ids] if preview_only else updated_nodes
    highway_calibration_profile = _promote_sparse_brain_highways(
        highway_scope_nodes,
        node_signal_map=node_signal_map,
        new_highways=new_highways,
        mode=mode,
    )

    updated_edges: list[dict[str, Any]] = []
    for edge in list(graph.get("edges") or []):
        source = redirect_map.get(str(edge.get("source_node_id")), str(edge.get("source_node_id")))
        target = redirect_map.get(str(edge.get("target_node_id")), str(edge.get("target_node_id")))
        if source == target:
            continue
        updated_edges.append({**edge, "source_node_id": source, "target_node_id": target})

    if len(duplicate_candidates) >= 2:
        pattern_candidates.append(
            {
                "kind": "duplicate_motif",
                "confidence": 0.76,
                "reason": "Repeated near-duplicate memories suggest pattern-level consolidation",
            }
        )
    if trace_insights["match_hits"] or correction_insights["target_hits"]:
        top_bucket = next(
            iter(sorted(trace_insights["bucket_hits"].items(), key=lambda item: item[1], reverse=True)),
            None,
        )
        pattern_candidates.append(
            {
                "kind": "trace_alignment_pattern",
                "confidence": 0.72,
                "reason": (
                    f"Trace hotspots and correction history suggest consolidating guidance around bucket {top_bucket[0]}."
                    if top_bucket
                    else "Trace hotspots and correction history suggest consolidating guidance into a higher-order support node."
                ),
            }
        )

    llm_timeout_seconds = (
        float(os.environ.get("AGVM_FAST_MAINTENANCE_LLM_TIMEOUT_SECONDS") or 4.0)
        if fast_preview
        else float(os.environ.get("AGVM_DEEP_MAINTENANCE_LLM_TIMEOUT_SECONDS") or 20.0)
    )
    llm_review = llm_sleep_review(
        {
            "mode": mode,
            "focus_node_id": focus_node_id,
            "preview_only": preview_only,
            "reviewed_nodes": [
                {
                    "node_id": node["id"],
                    "summary": node.get("summary"),
                    "memory_type": node.get("memory_type"),
                    "memory_confidence": node.get("memory_confidence"),
                    "stability_confidence": node.get("stability_confidence"),
                }
                for node in working_nodes[:40]
            ],
            "duplicate_candidates": duplicate_candidates[:20],
            "heuristic_merges": merges[:20],
            "pattern_candidates": pattern_candidates[:10],
            "trace_insights": {
                "candidate_hotspots": sorted(trace_insights["candidate_hits"].items(), key=lambda item: item[1], reverse=True)[:10],
                "match_hotspots": sorted(trace_insights["match_hits"].items(), key=lambda item: item[1], reverse=True)[:10],
                "bucket_hotspots": sorted(trace_insights["bucket_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
                "retrieval_gap_summary": dict(trace_insights.get("retrieval_gap_summary") or {}),
            },
            "correction_insights": correction_insights,
            "retrieval_gap_review": retrieval_gap_review,
            "working_memory_depromotion_policy": working_memory_depromotion_policy,
        },
        mode=mode,
        timeout_seconds=llm_timeout_seconds,
    )
    llm_review_status = "materialized" if llm_review else "not_materialized"
    alias_attachments: list[dict[str, Any]] = []
    if llm_review:
        alias_payload = list(llm_review.get("alias_attachments") or [])
        alias_attachments = _tag_maintenance_items(alias_payload, mode=mode, default_family="sleep")
        if alias_payload:
            existing = {(item.get("source_node_id"), item.get("target_node_id")) for item in merges}
            for alias in alias_payload:
                pair = (alias.get("source_node_id"), alias.get("target_node_id"))
                if pair not in existing:
                    existing.add(pair)
            # alias attachments are reported but not auto-applied yet
        if llm_review.get("confidence_updates"):
            confidence_updates.extend(list(llm_review.get("confidence_updates") or []))
        if llm_review.get("pattern_candidates"):
            pattern_candidates = list(llm_review.get("pattern_candidates") or []) + pattern_candidates
        if llm_review.get("highway_changes"):
            highway_changes.extend(list(llm_review.get("highway_changes") or []))
        reasoning_summary = str(llm_review.get("reasoning_summary") or "").strip()
    else:
        if mode == "sleep":
            reasoning_summary = "Manual sleep reviewed duplicates, confidence stability, alias cleanup, and bridge hygiene conservatively."
        elif mode == "evolve":
            reasoning_summary = "Manual evolve reviewed structural rebalance, stronger retyping, new route opportunities, and archival decisions."
        else:
            reasoning_summary = "Manual sleep/evolve reviewed both conservative revision and structural reorganization signals."

    if mode in {"evolve", "sleep_evolve"}:
        repositioned_counts_by_region: dict[str, int] = defaultdict(int)
        for item in repositioned_nodes:
            region_key = str(item.get("region_id") or "")
            if region_key:
                repositioned_counts_by_region[region_key] += 1
        for region_id, summary in region_summary_map.items():
            trace_hits = int(((summary.get("retrieval_usefulness") or {}).get("trace_hits") or 0))
            if trace_hits >= 3 or repositioned_counts_by_region.get(region_id):
                region_actions.append(
                    {
                        "region_id": region_id,
                        "trace_hits": trace_hits,
                        "success_ratio": float(((summary.get("retrieval_usefulness") or {}).get("success_ratio") or 0.0)),
                        "fail_ratio": float(((summary.get("retrieval_usefulness") or {}).get("fail_ratio") or 0.0)),
                        "bridge_usefulness": float(((summary.get("retrieval_usefulness") or {}).get("bridge_usefulness") or 0.0)),
                        "common_question_classes": list(((summary.get("retrieval_usefulness") or {}).get("common_question_classes") or [])),
                        "crowding_severity": str(summary.get("crowding_severity") or ""),
                        "instability_flags": list(summary.get("instability_flags") or []),
                        "repositioned_node_count": int(repositioned_counts_by_region.get(region_id, 0)),
                    }
                )

    if pattern_candidates:
        selected_pattern_candidate = dict(pattern_candidates[0] or {})
        pattern_summary = str(selected_pattern_candidate.get("reason") or "Repeated retrieval patterns detected")
        pattern_signature = _pattern_signature(selected_pattern_candidate, trace_insights=trace_insights, mode=mode)
        pattern_node_id = _pattern_node_id(selected_pattern_candidate, trace_insights=trace_insights, mode=mode)
        existing_pattern_node = _find_existing_pattern_node(
            {**graph, "nodes": updated_nodes},
            node_id=pattern_node_id,
            signature=pattern_signature,
            summary=pattern_summary,
        )
        pattern_text = (
            f"Maintenance pattern node: {pattern_summary}. "
            f"Duplicate candidates={len(duplicate_candidates)}, merges={len(merges)}, "
            f"trace candidate hotspots={len(trace_insights['candidate_hits'])}, trace match hotspots={len(trace_insights['match_hits'])}, "
            f"correction targets={len(correction_insights['target_hits'])}."
        )
        created_nodes.append(
            {
                "node_id": str(existing_pattern_node.get("id") if existing_pattern_node else pattern_node_id),
                "idempotency_key": pattern_node_id,
                "pattern_signature": pattern_signature,
                "reason": "pattern_candidate_from_sleep_evolve",
                "preview_only": preview_only,
                "summary": pattern_summary,
                "action": "reuse_existing_pattern_node" if existing_pattern_node else ("would_create_pattern_node" if preview_only else "create_pattern_node"),
                "existing_node_id": str(existing_pattern_node.get("id") or "") if existing_pattern_node else None,
            }
        )

    bridge_promotions = _tag_maintenance_items(
        [change for change in highway_changes if "promot" in str(change.get("change") or "")],
        mode=mode,
        default_family="sleep" if mode == "sleep" else "shared",
    )
    bridge_demotions = _tag_maintenance_items(
        [change for change in highway_changes if "demot" in str(change.get("change") or "") or "removed" in str(change.get("change") or "")],
        mode=mode,
        default_family="sleep" if mode == "sleep" else "shared",
    )
    post_graph = {
        **graph,
        "nodes": updated_nodes,
        "edges": updated_edges,
        "meta": {"graph_updated_at": utc_timestamp()},
    }
    preview_scope_before_graph = _preview_scope_graph(graph, working_node_ids) if preview_only else graph
    preview_scope_candidate_graph = _preview_scope_graph(post_graph, working_node_ids) if preview_only else post_graph
    if preview_only:
        maintenance_preview_plan["scope_graph"] = {
            "schema_version": "agvm.maintenance_preview_scope_graph.v1",
            "before_node_count": len(list(preview_scope_before_graph.get("nodes") or [])),
            "candidate_node_count": len(list(preview_scope_candidate_graph.get("nodes") or [])),
            "full_graph_node_count": len(nodes),
            "full_graph_delta_omitted": True,
            "policy": "preview rollback/delta is scoped to selected regions; full rollback is required only for explicit apply",
        }
    updated_graph = post_graph
    if not preview_only and mode in {"evolve", "sleep_evolve"} and created_nodes:
        created_payload = created_nodes[0]
        if str(created_payload.get("action") or "") == "reuse_existing_pattern_node":
            created_payload["applied_action"] = "reused_existing_pattern_node"
        else:
            try:
                pattern_node = _build_pattern_node(
                    graph=updated_graph,
                    raw_text=pattern_text,
                    summary=str(created_payload.get("summary") or "Maintenance pattern node for repeated retrieval structure"),
                    fixed_id=str(created_payload["node_id"]),
                )
                pattern_node["is_document_anchor"] = False
                pattern_node["answer_eligible"] = False
                pattern_node["profile_eligible"] = False
                pattern_node["document_eligible"] = False
                pattern_node["source_trust"] = "system_metadata"
                pattern_node["claim_status"] = "source_metadata"
                pattern_node["provenance"] = {
                    **dict(pattern_node.get("provenance") or {}),
                    "mode": "maintenance_pattern",
                    "maintenance_pattern_signature": str(created_payload.get("pattern_signature") or ""),
                    "maintenance_idempotency_key": str(created_payload.get("idempotency_key") or ""),
                    "answer_eligible": False,
                }
                updated_graph["nodes"] = list(updated_graph.get("nodes") or []) + [pattern_node]
                created_payload["node_id"] = str(pattern_node["id"])
                created_payload["summary"] = str(pattern_node.get("summary") or "")
                created_payload["applied_action"] = "created_pattern_node"
            except Exception as exc:
                created_payload["error"] = str(exc)
    if not preview_only and mode in {"evolve", "sleep_evolve"}:
        from derivation import normalize_runtime_graph

        updated_graph = normalize_runtime_graph(updated_graph)
    document_anchor_guard = _document_anchor_guard(graph, updated_graph if not preview_only else post_graph)
    quality_after = _compute_maintenance_quality(updated_graph if not preview_only else post_graph)
    quality_delta, overall_quality_delta_score = _compute_quality_delta(quality_before, quality_after)
    follow_up_candidates = _build_follow_up_candidates(
        trace_insights=trace_insights,
        correction_insights=correction_insights,
        region_actions=region_actions,
        quality_after=quality_after,
        report_mode=mode,
    )
    for recommendation in list(retrieval_gap_review.get("recommendations") or [])[:3]:
        follow_up_candidates.append(
            {
                "kind": str(recommendation.get("kind") or "retrieval_gap_review"),
                "priority": float(recommendation.get("priority") or 0.8),
                "query": str(recommendation.get("recommendation") or ""),
                "reason": str(recommendation.get("evidence_source") or "recent_search_sessions"),
                "derived_from": "retrieval_gap_review",
                "evidence_source": str(recommendation.get("evidence_source") or "recent_search_sessions"),
                "review_only": bool(recommendation.get("review_only", True)),
            }
        )
    proactive_opportunities = _build_proactive_opportunities(
        quality_after=quality_after,
        quality_delta=quality_delta,
        pattern_candidates=pattern_candidates,
        bridge_promotions=bridge_promotions,
        bridge_demotions=bridge_demotions,
        retyped_nodes=retyped_nodes,
        mode=mode,
    )
    follow_up_candidates = _tag_maintenance_items(
        follow_up_candidates,
        mode=mode,
        default_family=_mode_family_for_report(mode),
    )
    proactive_opportunities = _tag_maintenance_items(
        proactive_opportunities,
        mode=mode,
        default_family="evolve" if mode in {"evolve", "sleep_evolve"} else "sleep",
    )
    prepared_next_angles = [
        {
            "title": str(item.get("kind") or item.get("summary") or "follow_up"),
            "summary": str(item.get("reason") or item.get("summary") or ""),
            "priority": float(item.get("priority") or 0.0),
            "query": str(item.get("query") or ""),
            "derived_from": str(item.get("derived_from") or _mode_family_for_report(mode)),
            "maintenance_mode": mode,
        }
        for item in [*follow_up_candidates, *proactive_opportunities]
    ][:6]
    maintenance_history_summary = _summarize_maintenance_history(_recent_maintenance_runs(limit=5), {"applied": not preview_only, "mode": mode, "overall_quality_delta_score": overall_quality_delta_score, "follow_up_candidates": follow_up_candidates, "proactive_opportunities": proactive_opportunities})
    nucleus_graph = (
        _bounded_nucleus_graph_for_fast_preview(post_graph, working_nodes=working_nodes)
        if fast_preview
        else (updated_graph if not preview_only else post_graph)
    )
    nucleus = build_identity_nucleus(nucleus_graph)
    sleep_profile = _build_sleep_profile(
        mode=mode,
        duplicate_candidates=duplicate_candidates,
        alias_attachments=alias_attachments,
        confidence_updates=confidence_updates,
        bridge_promotions=bridge_promotions,
        bridge_demotions=bridge_demotions,
        archived_node_ids=archived_node_ids,
        superseded_node_ids=superseded_node_ids,
        trace_insights={
            "candidate_hotspots": sorted(trace_insights["candidate_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
            "match_hotspots": sorted(trace_insights["match_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
            "bucket_hotspots": sorted(trace_insights["bucket_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
            "retrieval_gap_summary": dict(trace_insights.get("retrieval_gap_summary") or {}),
        },
        correction_insights=correction_insights,
        nucleus_refresh={"recommended": mode in {"sleep", "evolve"}},
        quality_before=quality_before,
        quality_after=quality_after,
        quality_delta=quality_delta,
    )
    evolve_profile = _build_evolve_profile(
        mode=mode,
        retyped_nodes=retyped_nodes,
        repositioned_nodes=repositioned_nodes,
        region_actions=region_actions,
        created_nodes=created_nodes,
        new_highways=new_highways,
        archived_node_ids=archived_node_ids,
        superseded_node_ids=superseded_node_ids,
        quality_before=quality_before,
        quality_after=quality_after,
        quality_delta=quality_delta,
    )
    mode_overlap_summary = _build_mode_overlap_summary(sleep_profile=sleep_profile, evolve_profile=evolve_profile)
    mode_specific_quality_delta = _build_mode_specific_quality_delta(
        mode=mode,
        quality_delta=quality_delta,
        overall_quality_delta_score=overall_quality_delta_score,
        sleep_profile=sleep_profile,
        evolve_profile=evolve_profile,
    )
    maintenance_baseline_contract = build_maintenance_baseline_contract(
        mode=mode,
        preview_only=preview_only,
        focus_node_id=focus_node_id,
        max_nodes_considered=max_nodes_considered,
        graph=preview_scope_before_graph if preview_only else graph,
        geometry_report=brain_geometry_calibration_before,
        trace_insights=trace_insights,
        correction_insights=correction_insights,
        retrieval_gap_review=retrieval_gap_review,
        working_memory_depromotion_policy=working_memory_depromotion_policy,
        ingest_learning_review=ingest_learning_review,
        quality_before=quality_before,
    )
    sleep_consolidation = build_sleep_consolidation_proposal_engine(
        mode=mode,
        preview_only=preview_only,
        graph=preview_scope_before_graph if preview_only else graph,
        working_nodes=working_nodes,
        maintenance_baseline_contract=maintenance_baseline_contract,
        duplicate_candidates=duplicate_candidates,
        trace_insights=trace_insights,
        correction_insights=correction_insights,
        retrieval_gap_review=retrieval_gap_review,
        working_memory_depromotion_policy=working_memory_depromotion_policy,
        ingest_learning_review=ingest_learning_review,
    )
    evolve_structural = build_evolve_structural_proposal_engine(
        mode=mode,
        preview_only=preview_only,
        graph=preview_scope_before_graph if preview_only else graph,
        working_nodes=working_nodes,
        maintenance_baseline_contract=maintenance_baseline_contract,
        geometry_report=brain_geometry_calibration_before,
        duplicate_candidates=duplicate_candidates,
        merges=merges,
        pattern_candidates=pattern_candidates,
        repositioned_nodes=repositioned_nodes,
        retyped_nodes=retyped_nodes,
        new_highways=new_highways,
        bridge_promotions=bridge_promotions,
        bridge_demotions=bridge_demotions,
        region_actions=region_actions,
        highway_calibration_profile=highway_calibration_profile,
        evolve_profile=evolve_profile,
        ingest_learning_review=ingest_learning_review,
    )
    retrieval_trace_learning = build_retrieval_trace_learning_gate(
        mode=mode,
        preview_only=preview_only,
        graph=preview_scope_before_graph if preview_only else graph,
        maintenance_baseline_contract=maintenance_baseline_contract,
        trace_insights=trace_insights,
        retrieval_gap_review=retrieval_gap_review,
    )
    maintenance_proposal_surface = combine_maintenance_proposal_surfaces(
        mode=mode,
        preview_only=preview_only,
        maintenance_baseline_contract=maintenance_baseline_contract,
        sleep_consolidation=sleep_consolidation,
        evolve_structural=evolve_structural,
        retrieval_trace_learning=retrieval_trace_learning,
    )
    active_policy_revision: dict[str, Any] = {}
    brain_id = str(current_brain_id() or "default").strip() or "default"
    try:
        from sqlite_store import fetch_active_memory_policy_revision

        active_policy_revision = dict(fetch_active_memory_policy_revision(brain_id=brain_id) or {})
    except Exception:
        active_policy_revision = {}
    memory_policy_revision_preview = build_memory_policy_revision_candidate(
        brain_id=brain_id,
        mode=mode,
        preview_only=preview_only,
        ingest_learning_review=ingest_learning_review,
        retrieval_gap_review=retrieval_gap_review,
        trace_insights=trace_insights,
        deduction_mining=dict(sleep_consolidation.get("deduction_mining") or {}),
        maintenance_proposals=list(maintenance_proposal_surface.get("maintenance_proposals") or []),
        quality_before=quality_before,
        quality_after=quality_after,
        active_policy_revision=active_policy_revision,
    )
    reviewed_candidate_actions = [
        {
            "node_id": str(item.get("node_id") or ""),
            "action": "guide_area_retype",
            "allowed_fields": ["memory_type"],
            "source": "retyped_nodes",
        }
        for item in retyped_nodes
        if str(item.get("node_id") or "")
    ]
    reviewed_candidate_actions.extend(
        {
            "node_id": str(node_id),
            "action": "trace_low_signal_archive",
            "allowed_fields": ["lifecycle_status"],
            "source": "archived_node_ids",
        }
        for node_id in archived_node_ids
        if str(node_id or "")
    )
    reviewed_candidate_actions.extend(
        {
            "node_id": str(node_id),
            "action": "correction_supersede",
            "allowed_fields": ["lifecycle_status"],
            "source": "superseded_node_ids",
        }
        for node_id in superseded_node_ids
        if str(node_id or "")
    )
    apply_policy_guard = build_apply_policy_rollback_guard(
        mode=mode,
        preview_only=preview_only,
        before_graph=preview_scope_before_graph if preview_only else graph,
        candidate_graph=preview_scope_candidate_graph if preview_only else updated_graph,
        maintenance_baseline_contract=maintenance_baseline_contract,
        maintenance_proposals=list(maintenance_proposal_surface.get("maintenance_proposals") or []),
        document_anchor_guard=document_anchor_guard,
        quality_before=quality_before,
        candidate_quality_after=quality_after,
        candidate_quality_delta=quality_delta,
        overall_quality_delta_score=overall_quality_delta_score,
        reviewed_candidate_actions=reviewed_candidate_actions,
    )
    effective_applied = bool(apply_policy_guard.get("applied"))
    effective_graph = updated_graph if effective_applied else graph
    maintenance_transaction = build_maintenance_transaction(
        mode=mode,
        tool_name=f"{mode}_apply" if not preview_only else f"{mode}_preview",
        preview_only=preview_only,
        maintenance_proposals=list(maintenance_proposal_surface.get("maintenance_proposals") or []),
        selected_proposal_ids=[],
        confirm_apply=not preview_only,
        apply_requested=not preview_only,
        apply_policy_guard=apply_policy_guard,
        rollback_snapshot=dict(apply_policy_guard.get("rollback_snapshot") or {}),
        before_after_audit=dict(apply_policy_guard.get("before_after_audit") or {}),
        metamemory_snapshot_payload=dict(maintenance_baseline_contract.get("metamemory_snapshot") or {}),
        maintenance_preview_plan=maintenance_preview_plan,
        sleep_consolidation_profile=dict(sleep_consolidation.get("sleep_consolidation_profile") or {}),
        evolve_structural_profile=dict(evolve_structural.get("evolve_structural_profile") or {}),
        retrieval_trace_learning_gate=dict(retrieval_trace_learning.get("retrieval_trace_learning_gate") or {}),
        calibration_delta=quality_delta,
        archived_node_ids=archived_node_ids,
        superseded_node_ids=superseded_node_ids,
    )
    report = {
        "applied": effective_applied,
        "mode": mode,
        "preview_budget_guard": preview_budget_guard,
        "maintenance_preview_plan": maintenance_preview_plan,
        "maintenance_contract": dict(maintenance_baseline_contract.get("maintenance_contract") or {}),
        "proposal_schema": dict(maintenance_baseline_contract.get("proposal_schema") or {}),
        "metamemory_snapshot": dict(maintenance_baseline_contract.get("metamemory_snapshot") or {}),
        "failure_signatures": dict(maintenance_baseline_contract.get("failure_signatures") or {}),
        "maintenance_proposals": list(maintenance_proposal_surface.get("maintenance_proposals") or []),
        "maintenance_proposal_summary": dict(maintenance_proposal_surface.get("maintenance_proposal_summary") or {}),
        "sleep_consolidation_proposals": list(sleep_consolidation.get("sleep_consolidation_proposals") or []),
        "sleep_consolidation_profile": dict(sleep_consolidation.get("sleep_consolidation_profile") or {}),
        "deduction_candidates": list(sleep_consolidation.get("deduction_candidates") or []),
        "deduction_mining": dict(sleep_consolidation.get("deduction_mining") or {}),
        "evolve_structural_proposals": list(evolve_structural.get("evolve_structural_proposals") or []),
        "elastic_topology_proposals": list(evolve_structural.get("elastic_topology_proposals") or []),
        "evolve_structural_profile": dict(evolve_structural.get("evolve_structural_profile") or {}),
        "retrieval_trace_learning_proposals": list(retrieval_trace_learning.get("retrieval_trace_learning_proposals") or []),
        "retrieval_trace_learning_gate": dict(retrieval_trace_learning.get("retrieval_trace_learning_gate") or {}),
        "ingest_learning_review": ingest_learning_review,
        "memory_policy_revision_preview": memory_policy_revision_preview,
        "memory_policy_revision_candidate": dict(memory_policy_revision_preview.get("memory_policy_revision") or {}),
        "apply_policy_guard": apply_policy_guard,
        "maintenance_transaction": maintenance_transaction,
        "rollback_snapshot": dict(apply_policy_guard.get("rollback_snapshot") or {}),
        "no_corruption_guards": dict(apply_policy_guard.get("no_corruption_guards") or {}),
        "before_after_audit": dict(apply_policy_guard.get("before_after_audit") or {}),
        "reviewed_node_ids": [node["id"] for node in working_nodes],
        "duplicate_candidates": duplicate_candidates,
        "merges": merges,
        "alias_attachments": alias_attachments,
        "confidence_updates": confidence_updates,
        "highway_changes": highway_changes,
        "pattern_candidates": pattern_candidates,
        "created_nodes": created_nodes,
        "repositioned_nodes": repositioned_nodes,
        "retyped_nodes": retyped_nodes,
        "new_highways": new_highways,
        "deleted_node_ids": deleted_node_ids,
        "archived_node_ids": archived_node_ids,
        "superseded_node_ids": superseded_node_ids,
        "region_actions": region_actions,
        "trace_insights": {
            "candidate_hotspots": sorted(trace_insights["candidate_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
            "match_hotspots": sorted(trace_insights["match_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
            "bucket_hotspots": sorted(trace_insights["bucket_hits"].items(), key=lambda item: item[1], reverse=True)[:8],
            "stop_reasons": dict(trace_insights["stop_reasons"]),
            "retrieval_gap_summary": dict(trace_insights.get("retrieval_gap_summary") or {}),
        },
        "retrieval_gap_review": retrieval_gap_review,
        "working_memory_depromotion_policy": working_memory_depromotion_policy,
        "correction_insights": correction_insights,
        "bridge_promotions": bridge_promotions,
        "bridge_demotions": bridge_demotions,
        "follow_up_candidates": follow_up_candidates,
        "prepared_next_angles": prepared_next_angles,
        "proactive_opportunities": proactive_opportunities,
        "maintenance_history_summary": maintenance_history_summary,
        "ai_review_runtime": {
            "enabled": bool(llm_enabled()),
            "status": llm_review_status,
            "timeout_seconds": llm_timeout_seconds,
            "fast_preview": fast_preview,
            "deep_preview_available": True,
        },
        "sleep_profile": sleep_profile,
        "evolve_profile": evolve_profile,
        "mode_overlap_summary": mode_overlap_summary,
        "maintenance_mode_specific_quality_delta": mode_specific_quality_delta,
        "highway_calibration_profile": highway_calibration_profile,
        "document_anchor_guard": document_anchor_guard,
        "quality_before": quality_before,
        "quality_after": quality_after,
        "quality_delta": quality_delta,
        "overall_quality_delta_score": overall_quality_delta_score,
        "nucleus_refresh": {
            "recommended": mode in {"sleep", "evolve"},
            "focus_node_id": focus_node_id,
            "reviewed_count": len(working_nodes),
            "partner_candidate_count": len(list(nucleus.get("partner_candidates") or [])),
            "mentor_candidate_count": len(list(nucleus.get("mentor_candidates") or [])),
            "role_candidate_count": len(list(nucleus.get("role_candidates") or [])),
            "project_candidate_count": len(list(nucleus.get("project_candidates") or [])),
        },
        "untouched_areas": [],
        "reasoning_summary": reasoning_summary,
    }
    if preview_only:
        return graph, report
    return effective_graph, report


def sleep_review_graph(
    graph: dict[str, Any],
    *,
    preview_only: bool = True,
    focus_node_id: str | None = None,
    max_nodes_considered: int = 80,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return sleep_evolve_graph(
        graph,
        preview_only=preview_only,
        focus_node_id=focus_node_id,
        max_nodes_considered=max_nodes_considered,
        mode="sleep",
    )


def evolve_graph(
    graph: dict[str, Any],
    *,
    preview_only: bool = True,
    focus_node_id: str | None = None,
    max_nodes_considered: int = 80,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return sleep_evolve_graph(
        graph,
        preview_only=preview_only,
        focus_node_id=focus_node_id,
        max_nodes_considered=max_nodes_considered,
        mode="evolve",
    )


def llm_sleep_review(review_context: dict[str, Any], *, mode: str = "sleep", timeout_seconds: float = 20.0) -> dict[str, Any] | None:
    if not llm_enabled():
        return None
    mode_copy = str(mode or "sleep").strip().lower()
    if mode_copy == "evolve":
        planner_instruction = (
            "You are the AGVM evolve planner.\n\n"
            f"{build_metamemory_package('sleep')}\n\n"
            "Review the bounded maintenance context and suggest evidence-based structural reorganization: "
            "retyping, archival or supersede candidates, stronger route/highway changes, and pattern formation. "
            "Do not invent unsupported facts. Prefer structural changes only when the evidence justifies them."
        )
    elif mode_copy == "sleep_evolve":
        planner_instruction = (
            "You are the AGVM sleep/evolve planner.\n\n"
            f"{build_metamemory_package('sleep')}\n\n"
            "Review the bounded maintenance context and distinguish conservative review actions from structural reorganization. "
            "Propose alias refinements, confidence updates, bridge hygiene, and only then stronger structural changes when the evidence is strong."
        )
    else:
        planner_instruction = (
            "You are the AGVM sleep planner.\n\n"
            f"{build_metamemory_package('sleep')}\n\n"
            "Review the bounded maintenance context conservatively. Focus on alias refinements, confidence updates, bridge cleanup, "
            "and pattern candidates. Do not invent nodes. Prefer review and revision over strong structural edits."
        )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["alias_attachments", "confidence_updates", "highway_changes", "pattern_candidates", "reasoning_summary"],
        "properties": {
            "alias_attachments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_node_id", "target_node_id", "reason"],
                    "properties": {
                        "source_node_id": {"type": "string"},
                        "target_node_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "confidence_updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["node_id", "field", "from", "to"],
                    "properties": {
                        "node_id": {"type": "string"},
                        "field": {"type": "string"},
                        "from": {"type": "number"},
                        "to": {"type": "number"},
                    },
                },
            },
            "highway_changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["node_id", "change"],
                    "properties": {
                        "node_id": {"type": "string"},
                        "change": {"type": "string"},
                    },
                },
            },
            "pattern_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "confidence", "reason"],
                    "properties": {
                        "kind": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "reasoning_summary": {"type": "string"},
        },
    }
    payload, _error = structured_json(
        model=sleep_model(),
        system_prompt=planner_instruction,
        user_prompt=str(review_context),
        schema_name="agvm_sleep_review",
        schema=schema,
        timeout=max(1.0, float(timeout_seconds or 20.0)),
        role="sleep",
    )
    return payload
