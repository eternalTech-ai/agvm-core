from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Any

from metamemory import build_metamemory_package, metamemory_snapshot, metamemory_spatial_brief
from storage import utc_timestamp


CONTRACT_SCHEMA_VERSION = "agvm.pr12h.maintenance_contract.v1"
PROPOSAL_SCHEMA_VERSION = "agvm.pr12h.maintenance_proposal.v1"
METAMEMORY_SNAPSHOT_SCHEMA_VERSION = "agvm.pr12h.metamemory_snapshot.v1"
FAILURE_SIGNATURE_SCHEMA_VERSION = "agvm.pr12h.failure_signatures.v1"
SLEEP_CONSOLIDATION_PROPOSAL_ENGINE_SCHEMA_VERSION = "agvm.pr12h.sleep_consolidation_proposals.v1"
EVOLVE_STRUCTURAL_PROPOSAL_ENGINE_SCHEMA_VERSION = "agvm.pr12h.evolve_structural_proposals.v1"
MAINTENANCE_PROPOSAL_SUMMARY_SCHEMA_VERSION = "agvm.pr12h.maintenance_proposals.v1"
APPLY_POLICY_GUARD_SCHEMA_VERSION = "agvm.pr12h.apply_policy_guard.v1"
RETRIEVAL_TRACE_LEARNING_GATE_SCHEMA_VERSION = "agvm.pr12h.retrieval_trace_learning_gate.v1"
ELASTIC_TOPOLOGY_PROPOSAL_SCHEMA_VERSION = "agvm.elastic_topology_proposal.v1"
MAINTENANCE_TRANSACTION_SCHEMA_VERSION = "agvm.maintenance_transaction.v1"
DEDUCTION_CANDIDATE_SCHEMA_VERSION = "agvm.deduction_candidate.v1"


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


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


def _histogram(values: list[Any]) -> dict[str, int]:
    return dict(Counter(str(value or "unknown").strip() or "unknown" for value in values))


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or "").strip()


def _node_text(node: dict[str, Any]) -> str:
    return str(node.get("raw_text") or node.get("summary") or "").strip()


def build_versioned_metamemory_snapshot(*, role: str = "sleep") -> dict[str, Any]:
    guide_snapshot = dict(metamemory_snapshot())
    role_hashes: dict[str, str] = {}
    role_char_counts: dict[str, int] = {}
    for package_role in ("compiler", "retrieval", "answer", "sleep"):
        package = build_metamemory_package(package_role)
        role_hashes[package_role] = hashlib.sha256(package.encode("utf-8")).hexdigest()[:16]
        role_char_counts[package_role] = len(package)
    spatial_brief = metamemory_spatial_brief(role=role)
    spatial_brief_hash = hashlib.sha256(
        json.dumps(spatial_brief, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    snapshot_seed = {
        "guide_hash": guide_snapshot.get("hash"),
        "guide_sections": guide_snapshot.get("sections"),
        "role_hashes": role_hashes,
        "spatial_brief_hash": spatial_brief_hash,
        "policy": METAMEMORY_SNAPSHOT_SCHEMA_VERSION,
    }
    return {
        "schema_version": METAMEMORY_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_version": "pr12h-a.1",
        "snapshot_id": f"metamemory::{_json_hash(snapshot_seed)}",
        "active_role": str(role or "sleep"),
        "guide": guide_snapshot,
        "spatial_brief": {
            "schema_version": spatial_brief.get("schema_version"),
            "revision": spatial_brief.get("revision"),
            "hash": spatial_brief_hash,
            "source_snapshot_version": spatial_brief.get("source_snapshot_version"),
            "guide_areas": list(spatial_brief.get("guide_areas") or []),
            "radial_bands": list(spatial_brief.get("radial_bands") or []),
        },
        "role_package_hashes": role_hashes,
        "role_package_char_counts": role_char_counts,
        "captured_at": utc_timestamp(),
        "mutable": False,
        "mutation_policy": "metamemory_snapshots_are_observational_until_reviewed_evolve_apply",
    }


def maintenance_proposal_schema() -> dict[str, Any]:
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "stable string id, preferably kind::target::evidence_hash",
        "proposal_kind": {
            "allowed": [
                "confidence_review",
                "source_hygiene_review",
                "duplicate_review",
                "contradiction_review",
                "cold_promotion_review",
                "warm_depromotion_review",
                "geometry_reposition_review",
                "highway_review",
                "merge_review",
                "split_review",
                "document_project_coupling_review",
                "retrieval_gap_review",
                "write_policy_review",
                "elastic_topology_deformation_review",
                "deduction_review",
                "clarification_review",
            ]
        },
        "risk_level": {"allowed": ["low", "medium", "high", "blocked"]},
        "required_fields": [
            "proposal_id",
            "proposal_kind",
            "risk_level",
            "mode_family",
            "target_node_ids",
            "target_document_ids",
            "target_region_ids",
            "evidence_refs",
            "failure_signature_refs",
            "proposed_action",
            "preview_delta",
            "apply_policy",
            "rollback_policy",
            "human_review_required",
        ],
        "evidence_refs": {
            "allowed_sources": [
                "brain_geometry_calibration",
                "recent_search_sessions",
                "search_events",
                "correction_history",
                "warm_thread_state",
                "graph_source_hygiene",
                "cognitive_write_trace",
                "metamemory_snapshot",
                "mission_learning_rollup",
                "elastic_topology_solver",
                "memory_learning_events",
                "ingest_learning_feedback",
                "source_references",
            ]
        },
        "apply_policy": {
            "preview_default": True,
            "auto_apply_allowed_in_pr12h_a": False,
            "auto_apply_allowed_in_pr12h_b": False,
            "auto_apply_allowed_in_pr12h_c": False,
            "auto_apply_allowed_in_pr12h_d": "reviewed_low_risk_only",
            "requires_before_after_audit": True,
            "identity_relationship_document_anchor_guard": True,
        },
        "rollback_policy": {
            "requires_snapshot_ref": True,
            "requires_inverse_delta": True,
            "must_preserve_raw_documents": True,
        },
    }


def _string_list(value: Any, *, limit: int = 24) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        raw_values = [value]
    elif isinstance(value, dict):
        if str(value.get("key") or "").strip():
            raw_values = [value.get("key")]
        elif str(value.get("id") or "").strip():
            raw_values = [value.get("id")]
        else:
            raw_values = []
    else:
        raw_values = list(value or [])
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _node_confidence_floor(node: dict[str, Any]) -> float:
    return min(
        _safe_float(node.get("memory_confidence"), 1.0),
        _safe_float(node.get("evidence_confidence"), 1.0),
        _safe_float(node.get("stability_confidence"), 1.0),
    )


def _node_document_ids(node: dict[str, Any]) -> list[str]:
    provenance = dict(node.get("provenance") or {})
    values = [
        node.get("document_id"),
        node.get("source_document_id"),
        provenance.get("document_id"),
        provenance.get("source_id"),
        provenance.get("source_url"),
    ]
    if bool(node.get("is_document_anchor")) or str(node.get("memory_type") or "") == "document_anchor":
        values.append(_node_id(node))
    return _string_list(values, limit=6)


def _node_region_ids(node: dict[str, Any]) -> list[str]:
    values = [
        node.get("bucket"),
        node.get("routing_bucket"),
        node.get("routing_region"),
        node.get("region_id"),
    ]
    return _string_list(values, limit=6)


def _risk_from_severity(value: Any, *, default: str = "medium") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"blocked", "critical"}:
        return "blocked"
    if normalized in {"high", "medium", "low"}:
        return normalized
    return default


def _proposal_policy(*, risk_level: str, preview_only: bool) -> dict[str, Any]:
    return {
        "preview_only": True,
        "requested_run_preview_only": bool(preview_only),
        "auto_apply_allowed": False,
        "requires_human_review": True,
        "requires_before_after_audit": True,
        "identity_relationship_document_anchor_guard": True,
        "allowed_next_step": "human_review_then_pr12h_d_apply_policy",
        "risk_level": risk_level,
    }


def _rollback_policy(contract_id: str, *, rollback_boundary: str = "proposal_only_in_pr12h_b") -> dict[str, Any]:
    return {
        "requires_snapshot_ref": True,
        "snapshot_ref": contract_id,
        "requires_inverse_delta": True,
        "must_preserve_raw_documents": True,
        "rollback_boundary": rollback_boundary,
    }


def _evidence_ref(source: str, ref_id: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source,
        "ref_id": str(ref_id or source),
    }
    if detail:
        payload["detail"] = dict(detail)
    return payload


def _node_targets(nodes_by_id: dict[str, dict[str, Any]], node_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
    target_node_ids = _string_list(node_ids, limit=24)
    document_ids: list[str] = []
    region_ids: list[str] = []
    for node_id in target_node_ids:
        node = nodes_by_id.get(node_id)
        if not node:
            continue
        document_ids.extend(_node_document_ids(node))
        region_ids.extend(_node_region_ids(node))
    return target_node_ids, _string_list(document_ids, limit=24), _string_list(region_ids, limit=24)


def _node_position(node: dict[str, Any]) -> dict[str, float] | None:
    position = node.get("final_position") if isinstance(node.get("final_position"), dict) else node.get("base_position")
    if not isinstance(position, dict) or not any(key in position for key in ("x", "y", "z")):
        return None
    return {
        "x": _safe_float(position.get("x")),
        "y": _safe_float(position.get("y")),
        "z": _safe_float(position.get("z")),
    }


def _vector_between(start: dict[str, float], end: dict[str, float]) -> dict[str, float]:
    return {
        "x": _safe_float(end.get("x")) - _safe_float(start.get("x")),
        "y": _safe_float(end.get("y")) - _safe_float(start.get("y")),
        "z": _safe_float(end.get("z")) - _safe_float(start.get("z")),
    }


def _scale_vector(vector: dict[str, float], scale: float) -> dict[str, float]:
    return {
        "x": round(_safe_float(vector.get("x")) * scale, 6),
        "y": round(_safe_float(vector.get("y")) * scale, 6),
        "z": round(_safe_float(vector.get("z")) * scale, 6),
    }


def _vector_length(vector: dict[str, float]) -> float:
    return math.sqrt(
        _safe_float(vector.get("x")) ** 2
        + _safe_float(vector.get("y")) ** 2
        + _safe_float(vector.get("z")) ** 2
    )


def _clamp_vector(vector: dict[str, float], max_displacement: float) -> dict[str, float]:
    length = _vector_length(vector)
    max_displacement = max(0.0, _safe_float(max_displacement))
    if length <= 1e-9 or length <= max_displacement:
        return {
            "x": round(_safe_float(vector.get("x")), 6),
            "y": round(_safe_float(vector.get("y")), 6),
            "z": round(_safe_float(vector.get("z")), 6),
        }
    return _scale_vector(vector, max_displacement / length)


def _add_position(position: dict[str, float], delta: dict[str, float]) -> dict[str, float]:
    return {
        "x": round(_safe_float(position.get("x")) + _safe_float(delta.get("x")), 6),
        "y": round(_safe_float(position.get("y")) + _safe_float(delta.get("y")), 6),
        "z": round(_safe_float(position.get("z")) + _safe_float(delta.get("z")), 6),
    }


def _distance_between(left: dict[str, float], right: dict[str, float]) -> float:
    return round(_vector_length(_vector_between(left, right)), 6)


def _node_anchor_class(node: dict[str, Any]) -> str:
    memory_type = str(node.get("memory_type") or "").strip().lower()
    node_kind = str(node.get("node_kind") or "").strip().lower()
    if bool(node.get("is_document_anchor")) or memory_type == "document_anchor" or node_kind == "document_anchor":
        return "document_anchor"
    if memory_type.startswith("identity"):
        return "identity_nucleus"
    if memory_type in {"source_anchor", "raw_source_anchor"} or node_kind in {"source_anchor", "source"}:
        return "source_anchor"
    return "movable_memory"


def _anchor_max_displacement(anchor_class: str, *, default: float = 0.12) -> float:
    if anchor_class == "document_anchor":
        return 0.0
    if anchor_class == "identity_nucleus":
        return min(0.04, default)
    if anchor_class == "source_anchor":
        return min(0.03, default)
    return default


def _neighbor_ids_for_nodes(
    graph: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    target_node_ids: list[str],
    *,
    limit: int = 16,
) -> list[str]:
    targets = set(_string_list(target_node_ids, limit=64))
    neighbors: list[str] = []
    seen: set[str] = set(targets)

    def add(value: Any) -> None:
        key = str(value or "").strip()
        if not key or key in seen or key not in nodes_by_id:
            return
        seen.add(key)
        neighbors.append(key)

    for node_id in list(targets):
        node = nodes_by_id.get(node_id) or {}
        for link in list(node.get("links") or []) + list(node.get("highways") or []):
            if isinstance(link, dict):
                add(link.get("target_node_id") or link.get("node_id") or link.get("id"))
    for edge in list(graph.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source_node_id") or edge.get("source") or "").strip()
        target = str(edge.get("target_node_id") or edge.get("target") or "").strip()
        if source in targets:
            add(target)
        if target in targets:
            add(source)
        if len(neighbors) >= limit:
            break
    return neighbors[:limit]


def _region_density(nodes_by_id: dict[str, dict[str, Any]], node_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node_id in _string_list(node_ids, limit=128):
        node = nodes_by_id.get(node_id) or {}
        regions = _node_region_ids(node) or ["unknown"]
        region = str(regions[0] or "unknown")
        counts[region] = int(counts.get(region) or 0) + 1
    return counts


def _elastic_displacement_preview(
    nodes_by_id: dict[str, dict[str, Any]],
    *,
    node_id: str,
    force_vector: dict[str, float],
    role: str,
    dampening: float,
    max_displacement: float,
) -> dict[str, Any] | None:
    node = nodes_by_id.get(str(node_id or ""))
    if not node:
        return None
    before = _node_position(node)
    if before is None:
        return None
    anchor_class = _node_anchor_class(node)
    capped_max = _anchor_max_displacement(anchor_class, default=max_displacement)
    delta = _clamp_vector(_scale_vector(force_vector, dampening), capped_max)
    return {
        "node_id": str(node_id),
        "role": role,
        "anchor_class": anchor_class,
        "pinned": _vector_length(delta) <= 1e-9,
        "max_displacement": round(capped_max, 6),
        "dampening": round(dampening, 6),
        "before_position": before,
        "delta": delta,
        "projected_position": _add_position(before, delta),
    }


def _centroid_for_positions(positions: list[dict[str, float]]) -> dict[str, float] | None:
    if not positions:
        return None
    return {
        "x": sum(_safe_float(position.get("x")) for position in positions) / len(positions),
        "y": sum(_safe_float(position.get("y")) for position in positions) / len(positions),
        "z": sum(_safe_float(position.get("z")) for position in positions) / len(positions),
    }


def _elastic_topology_payload(
    *,
    intent: str,
    evidence_refs: list[dict[str, Any]],
    target_node_ids: list[str],
    affected_neighbor_ids: list[str],
    node_displacement_preview: list[dict[str, Any]],
    before_after_metrics: dict[str, Any],
    force_vector: dict[str, float],
    max_displacement: float,
    risk_level: str,
    matrix_calibration_candidate: bool = False,
) -> dict[str, Any]:
    pinned_ids = [
        str(item.get("node_id") or "")
        for item in node_displacement_preview
        if bool(item.get("pinned")) and str(item.get("node_id") or "")
    ]
    anchor_counts = _histogram([item.get("anchor_class") for item in node_displacement_preview])
    return {
        "schema_version": ELASTIC_TOPOLOGY_PROPOSAL_SCHEMA_VERSION,
        "intent": intent,
        "evidence_refs": [dict(item) for item in evidence_refs],
        "target_cluster_node_ids": _string_list(target_node_ids, limit=24),
        "affected_neighbor_node_ids": _string_list(affected_neighbor_ids, limit=24),
        "force_vector": {
            "x": round(_safe_float(force_vector.get("x")), 6),
            "y": round(_safe_float(force_vector.get("y")), 6),
            "z": round(_safe_float(force_vector.get("z")), 6),
        },
        "dampening_policy": {
            "direct_target_factor": 1.0,
            "opposite_endpoint_factor": 0.65,
            "direct_neighbor_factor": 0.35,
            "graph_spring_factor": 0.18,
            "anchor_policy": "document_anchors_pinned_identity_and_source_anchors_minimal_coordinate_only",
        },
        "max_displacement": round(max_displacement, 6),
        "anchor_preservation": {
            "anchor_counts": anchor_counts,
            "pinned_node_ids": pinned_ids,
            "document_anchor_move_allowed": False,
            "identity_core_move_policy": "coordinate_safe_micro_shift_only",
            "source_anchor_move_policy": "prefer_bridge_or_highway_over_movement",
        },
        "node_displacement_preview": node_displacement_preview[:24],
        "before_after_metrics": dict(before_after_metrics),
        "expected_retrieve_improvement": dict(before_after_metrics.get("expected_retrieve_improvement") or {}),
        "matrix_calibration_candidate": bool(matrix_calibration_candidate),
        "risk_level": risk_level,
        "rollback_requirements": {
            "requires_snapshot_ref": True,
            "requires_inverse_delta": True,
            "requires_post_apply_retrieve_probe": True,
        },
        "mutation_policy": "proposal_only_no_coordinate_mutation_in_preview",
    }


def _make_proposal(
    *,
    proposal_kind: str,
    risk_level: str,
    mode_family: str,
    contract_id: str,
    target_node_ids: list[str] | None = None,
    target_document_ids: list[str] | None = None,
    target_region_ids: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    failure_signature_refs: list[dict[str, Any]] | None = None,
    proposed_action: str,
    preview_delta: dict[str, Any],
    reason: str,
    priority: float,
    preview_only: bool,
    source_slice: str = "PR-12H-B",
    rollback_boundary: str = "proposal_only_in_pr12h_b",
) -> dict[str, Any]:
    target_node_ids = _string_list(target_node_ids, limit=24)
    target_document_ids = _string_list(target_document_ids, limit=24)
    target_region_ids = _string_list(target_region_ids, limit=24)
    evidence_refs = [dict(item) for item in list(evidence_refs or []) if isinstance(item, dict)]
    failure_signature_refs = [dict(item) for item in list(failure_signature_refs or []) if isinstance(item, dict)]
    seed = {
        "proposal_kind": proposal_kind,
        "target_node_ids": target_node_ids,
        "target_document_ids": target_document_ids,
        "target_region_ids": target_region_ids,
        "evidence_refs": evidence_refs,
        "failure_signature_refs": failure_signature_refs,
        "proposed_action": proposed_action,
    }
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "source_slice": source_slice,
        "proposal_id": f"{proposal_kind}::{_json_hash(seed)}",
        "proposal_kind": proposal_kind,
        "risk_level": risk_level,
        "mode_family": str(mode_family or "sleep_evolve"),
        "target_node_ids": target_node_ids,
        "target_document_ids": target_document_ids,
        "target_region_ids": target_region_ids,
        "evidence_refs": evidence_refs,
        "failure_signature_refs": failure_signature_refs,
        "proposed_action": proposed_action,
        "preview_delta": {"mutates_graph": False, **dict(preview_delta or {})},
        "apply_policy": _proposal_policy(risk_level=risk_level, preview_only=preview_only),
        "rollback_policy": _rollback_policy(contract_id, rollback_boundary=rollback_boundary),
        "human_review_required": True,
        "review_only": True,
        "reason": reason,
        "priority": round(max(0.0, min(1.0, float(priority))), 4),
    }


def _dedupe_sort_proposals(proposals: list[dict[str, Any]], *, max_proposals: int) -> list[dict[str, Any]]:
    risk_rank = {"blocked": 0, "high": 1, "medium": 2, "low": 3}
    unique: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        proposal_id = str(proposal.get("proposal_id") or "")
        if not proposal_id:
            continue
        existing = unique.get(proposal_id)
        if existing and _safe_float(existing.get("priority")) >= _safe_float(proposal.get("priority")):
            continue
        unique[proposal_id] = dict(proposal)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            risk_rank.get(str(item.get("risk_level") or "medium"), 2),
            -_safe_float(item.get("priority")),
            str(item.get("proposal_id") or ""),
        ),
    )
    return ordered[: max(1, int(max_proposals))]


def _ingest_focus_events(ingest_learning_review: dict[str, Any] | None, focus_key: str, event_key: str) -> list[dict[str, Any]]:
    review = dict(ingest_learning_review or {})
    focus = dict(review.get(focus_key) or {})
    return [
        dict(item)
        for item in list(focus.get(event_key) or [])
        if isinstance(item, dict)
    ]


def _ingest_event_node_ids(events: list[dict[str, Any]], *, limit: int = 24) -> list[str]:
    node_ids: list[str] = []
    for event in events:
        node_ids.extend(_string_list(event.get("related_node_ids"), limit=24))
        node_ids.extend(_string_list(event.get("duplicate_targets"), limit=12))
        node_ids.extend(_string_list(event.get("contradiction_targets"), limit=12))
        node_ids.append(event.get("persisted_node_id"))
    return _string_list(node_ids, limit=limit)


def _ingest_evidence_refs(events: list[dict[str, Any]], *, ref_id: str, limit: int = 8) -> list[dict[str, Any]]:
    examples = [
        {
            "event_id": str(event.get("event_id") or ""),
            "event_kind": str(event.get("event_kind") or ""),
            "operation_id": str(event.get("operation_id") or ""),
            "source_unit_id": str(event.get("source_unit_id") or ""),
            "source_ref_id": str(event.get("source_ref_id") or ""),
            "summary": str(event.get("human_readable_evidence") or ""),
        }
        for event in events[:limit]
    ]
    return [
        _evidence_ref(
            "memory_learning_events",
            ref_id,
            detail={
                "event_count": len(events),
                "examples": examples,
            },
        )
    ]


def _deduction_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _deduction_entities(text: str, *, limit: int = 8) -> list[str]:
    # Intentionally conservative: use durable proper-noun style anchors and
    # avoid inventing unnamed entities from pronouns or generic words.
    entities: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,5}\b", text):
        value = " ".join(match.group(0).split()).strip(" .,;:")
        folded = value.lower()
        if len(value) < 3 or folded in {"source uri", "page title", "document", "memory", "the", "this"}:
            continue
        if value not in seen:
            seen.add(value)
            entities.append(value)
        if len(entities) >= limit:
            break
    return entities


def _deduction_relation_label(text: str) -> str:
    folded = f" {_deduction_text(text).lower()} "
    relation_markers = [
        ("founding_or_creation", (" founded ", " created ", " built ", " launched ", " fondato ", " creato ", " costruito ")),
        ("leadership_or_role", (" ceo", " chief executive", " founder", " leads ", " guida ", " amministratore", " presidente")),
        ("values_or_beliefs", (" believes ", " belief", " value", " values", " conviction", " sostenibil", " responsabil", " precision")),
        ("family_or_relationship", (" father", " mother", " son", " daughter", " wife", " husband", " padre", " madre", " figlio", " figlia", " moglie", " marito")),
        ("preference_or_identity_detail", (" favorite ", " favourite ", " prefers ", " squadra preferita", " preferisce ")),
        ("project_or_work", (" project", " work", " company", " startup", " progetto", " lavoro", " azienda")),
    ]
    for label, markers in relation_markers:
        if any(marker in folded for marker in markers):
            return label
    return "semantic_association"


def _deduction_slot_object(text: str) -> tuple[str, str] | None:
    folded = _deduction_text(text).lower()
    patterns = [
        ("preference_team", r"(?:favorite|favourite)\s+team\s+(?:is|was|:)\s+([^.;,]+)"),
        ("preference_team", r"squadra\s+preferita\s+(?:e|era|si\s+chiama|:)\s+([^.;,]+)"),
        ("family_father", r"(?:father|padre)\s+(?:is|was|si\s+chiama|e|era|:)\s+([^.;,]+)"),
        ("family_mother", r"(?:mother|madre)\s+(?:is|was|si\s+chiama|e|era|:)\s+([^.;,]+)"),
        ("role_company", r"(?:ceo|chief executive|amministratore\s+delegato)\s+(?:of|di|at|in|:)\s+([^.;,]+)"),
    ]
    for slot, pattern in patterns:
        match = re.search(pattern, folded)
        if not match:
            continue
        obj = " ".join(match.group(1).split()).strip(" .;,:")
        if obj:
            return slot, obj[:120]
    return None


def _deduction_support_node(node: dict[str, Any]) -> dict[str, Any] | None:
    node_id = _node_id(node)
    text = _deduction_text(_node_text(node))
    if not node_id or len(text) < 18:
        return None
    if bool(node.get("is_document_anchor")):
        return None
    if str(node.get("source_trust") or "").lower() in {"synthetic_test", "system_metadata"}:
        return None
    if str(node.get("claim_status") or "fact").lower() in {"source_metadata", "test_artifact", "superseded"}:
        return None
    confidence = _node_confidence_floor(node)
    if confidence < 0.58:
        return None
    entities = _deduction_entities(text)
    slot_object = _deduction_slot_object(text)
    return {
        "node_id": node_id,
        "text": text,
        "entities": entities,
        "relation_label": _deduction_relation_label(text),
        "slot": slot_object[0] if slot_object else "",
        "object_signature": slot_object[1] if slot_object else "",
        "confidence": round(confidence, 4),
        "memory_type": str(node.get("memory_type") or ""),
        "region_ids": _node_region_ids(node),
        "document_ids": _node_document_ids(node),
    }


def _make_deduction_candidate(
    *,
    candidate_kind: str,
    classification: str,
    support_items: list[dict[str, Any]],
    candidate_text: str,
    confidence: float,
    reason: str,
    question: str = "",
    unsafe_reasons: list[str] | None = None,
) -> dict[str, Any]:
    support_ids = _string_list([item.get("node_id") for item in support_items], limit=12)
    seed = {
        "candidate_kind": candidate_kind,
        "classification": classification,
        "support_node_ids": support_ids,
        "candidate_text": candidate_text,
        "question": question,
    }
    blocked_answer_authority = classification in {"hypothesis", "question", "contradiction"} or bool(unsafe_reasons)
    return {
        "schema_version": DEDUCTION_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": f"deduction::{_json_hash(seed)}",
        "candidate_kind": candidate_kind,
        "classification": classification,
        "support_node_ids": support_ids,
        "support": [
            {
                "node_id": item.get("node_id"),
                "relation_label": item.get("relation_label"),
                "confidence": item.get("confidence"),
                "entities": list(item.get("entities") or [])[:6],
            }
            for item in support_items[:8]
        ],
        "candidate_text": candidate_text,
        "confidence": round(max(0.0, min(0.92, confidence)), 4),
        "reason": reason,
        "question": question,
        "unsafe_reasons": _string_list(unsafe_reasons or [], limit=8),
        "answer_authority": {
            "allowed_for_high_confidence_answering": False,
            "requires_human_review_before_answer_authority": True,
            "approved": False,
            "blocked_reason": "deduction_candidate_is_review_only_until_approved"
            if blocked_answer_authority
            else "review_required_before_memory_promotion",
        },
        "mutation_policy": {
            "mutates_graph": False,
            "persists_memory": False,
            "may_create_memory_after_review": True,
            "auto_apply_allowed": False,
        },
    }


def build_sleep_deduction_candidates(
    *,
    graph: dict[str, Any],
    working_nodes: list[dict[str, Any]],
    trace_insights: dict[str, Any],
    max_candidates: int = 8,
) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    working_ids = set(_string_list([node.get("id") for node in working_nodes], limit=200))
    co_selected_ids = set(
        _string_list(
            [
                *list(dict(trace_insights.get("match_hits") or {}).keys()),
                *list(dict(trace_insights.get("candidate_hits") or {}).keys()),
                *list(working_ids),
            ],
            limit=240,
        )
    )
    support_items = [
        item
        for item in (_deduction_support_node(node) for node in nodes)
        if item is not None and (not co_selected_ids or item["node_id"] in co_selected_ids or len(co_selected_ids) < 2)
    ]
    candidates: list[dict[str, Any]] = []

    by_slot: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in support_items:
        slot = str(item.get("slot") or "")
        obj = str(item.get("object_signature") or "")
        if not slot or not obj:
            continue
        subject = str((item.get("entities") or ["memory_subject"])[0] or "memory_subject")
        by_slot.setdefault((subject.lower(), slot), []).append(item)
    for (_subject, slot), items in sorted(by_slot.items()):
        object_values = sorted({str(item.get("object_signature") or "") for item in items if str(item.get("object_signature") or "")})
        if len(items) < 2 or len(object_values) < 2:
            continue
        candidates.append(
            _make_deduction_candidate(
                candidate_kind="conflicting_slot",
                classification="contradiction",
                support_items=items,
                candidate_text=f"Conflicting memories describe {slot.replace('_', ' ')} differently.",
                confidence=min(0.86, 0.52 + 0.08 * len(items)),
                reason="Stable memories expose different values for the same relation slot.",
                question=f"Which {slot.replace('_', ' ')} value should remain active, and which source confirms it?",
                unsafe_reasons=["conflicting_supported_values"],
            )
        )

    by_entity: dict[str, list[dict[str, Any]]] = {}
    for item in support_items:
        for entity in list(item.get("entities") or [])[:4]:
            by_entity.setdefault(str(entity), []).append(item)
    for entity, items in sorted(by_entity.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        unique_ids = _string_list([item.get("node_id") for item in items], limit=12)
        relation_labels = sorted({str(item.get("relation_label") or "") for item in items if str(item.get("relation_label") or "")})
        if len(unique_ids) < 2 or len(relation_labels) < 2:
            continue
        if any(candidate for candidate in candidates if set(candidate.get("support_node_ids") or []) == set(unique_ids[: len(candidate.get("support_node_ids") or [])])):
            continue
        avg_conf = sum(_safe_float(item.get("confidence")) for item in items) / max(1, len(items))
        classification = "inference" if avg_conf >= 0.76 and len(unique_ids) >= 3 else "hypothesis"
        candidates.append(
            _make_deduction_candidate(
                candidate_kind="stable_cluster_abstraction",
                classification=classification,
                support_items=items[:6],
                candidate_text=(
                    f"Multiple stable memories around {entity} may support a higher-level pattern "
                    f"across {', '.join(relation_labels[:4])}."
                ),
                confidence=min(0.82, 0.44 + avg_conf * 0.32 + min(0.12, len(unique_ids) * 0.03)),
                reason="Sleep found co-selected stable memories that share an entity but are stored as separate narrow facts.",
                question=(
                    f"Should this become a reviewed higher-level memory about {entity}, "
                    "or should the supporting facts remain separate?"
                )
                if classification == "hypothesis"
                else "",
                unsafe_reasons=[] if classification == "inference" else ["needs_human_confirmation"],
            )
        )
        if len(candidates) >= max_candidates:
            break

    candidates = sorted(
        candidates,
        key=lambda item: (
            {"contradiction": 0, "question": 1, "hypothesis": 2, "inference": 3, "fact": 4}.get(str(item.get("classification")), 3),
            -_safe_float(item.get("confidence")),
            str(item.get("candidate_id") or ""),
        ),
    )[: max(1, int(max_candidates))]
    return {
        "schema_version": "agvm.sleep_deduction_mining.v1",
        "candidate_schema_version": DEDUCTION_CANDIDATE_SCHEMA_VERSION,
        "candidate_count": len(candidates),
        "support_item_count": len(support_items),
        "candidates": candidates,
        "policy": {
            "review_only": True,
            "hidden_memory_node_mutation_allowed": False,
            "unapproved_hypotheses_can_answer_with_high_confidence": False,
            "unsafe_candidates_route_to_questions": True,
        },
    }


def build_sleep_consolidation_proposal_engine(
    *,
    mode: str,
    preview_only: bool,
    graph: dict[str, Any],
    working_nodes: list[dict[str, Any]],
    maintenance_baseline_contract: dict[str, Any],
    duplicate_candidates: list[dict[str, Any]],
    trace_insights: dict[str, Any],
    correction_insights: dict[str, Any],
    retrieval_gap_review: dict[str, Any],
    working_memory_depromotion_policy: dict[str, Any],
    ingest_learning_review: dict[str, Any] | None = None,
    max_proposals: int = 24,
) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    working = [dict(node) for node in list(working_nodes or []) if isinstance(node, dict)]
    nodes_by_id = {_node_id(node): node for node in nodes if _node_id(node)}
    failure_signatures = dict(maintenance_baseline_contract.get("failure_signatures") or {})
    maintenance_contract = dict(maintenance_baseline_contract.get("maintenance_contract") or {})
    contract_id = str(maintenance_contract.get("contract_id") or "maintenance_contract::unknown")
    mode_family = str(mode or maintenance_contract.get("mode") or "sleep_evolve")
    proposals: list[dict[str, Any]] = []
    coverage = {
        "weak_confidence": False,
        "stale_hot_context": False,
        "cold_promotion": False,
        "duplicate_candidates": False,
        "contradictions": False,
        "source_hygiene": False,
        "ingest_duplicates": False,
        "ingest_contradictions": False,
        "ingest_clarifications": False,
        "ingest_source_links": False,
        "deduction_candidates": False,
        "deduction_clarifications": False,
    }

    deduction_mining = build_sleep_deduction_candidates(
        graph=graph,
        working_nodes=working,
        trace_insights=trace_insights,
        max_candidates=8,
    )
    for candidate in list(deduction_mining.get("candidates") or []):
        if not isinstance(candidate, dict):
            continue
        support_ids = _string_list(candidate.get("support_node_ids"), limit=12)
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, support_ids)
        if not target_ids:
            continue
        classification = str(candidate.get("classification") or "hypothesis")
        is_contradiction = classification == "contradiction"
        is_question = classification == "question" or bool(candidate.get("question"))
        proposal_kind = "contradiction_review" if is_contradiction else "clarification_review" if is_question else "deduction_review"
        risk_level = "high" if is_contradiction else "medium" if classification in {"hypothesis", "question"} else "low"
        proposed_action = (
            "ask_clarification_before_any_deduction_promotion"
            if is_contradiction or is_question
            else "review_promote_or_reject_sleep_inference_before_answer_authority"
        )
        proposals.append(
            _make_proposal(
                proposal_kind=proposal_kind,
                risk_level=risk_level,
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "sleep_deduction_mining",
                        str(candidate.get("candidate_id") or "deduction_candidate"),
                        detail={
                            "classification": classification,
                            "candidate_kind": candidate.get("candidate_kind"),
                            "confidence": candidate.get("confidence"),
                            "support_node_ids": target_ids,
                        },
                    )
                ],
                failure_signature_refs=[
                    _evidence_ref(
                        "failure_signatures.sleep",
                        "deduction_contradiction_candidate" if is_contradiction else "deduction_candidate_review",
                    )
                ],
                proposed_action=proposed_action,
                preview_delta={
                    "candidate_schema_version": DEDUCTION_CANDIDATE_SCHEMA_VERSION,
                    "deduction_candidate": candidate,
                    "suggested_review": (
                        "resolve_conflict_or_ask_user_before_memory_authority"
                        if is_contradiction or is_question
                        else "approve_as_inference_reject_or_keep_as_review_only"
                    ),
                    "answer_authority_allowed": False,
                    "hidden_memory_node_mutation_allowed": False,
                },
                reason=str(candidate.get("reason") or "Sleep found a reviewable deduction candidate from stable memories."),
                priority=0.94 if is_contradiction else 0.84 if classification == "hypothesis" else 0.79,
                preview_only=preview_only,
                source_slice="M8",
                rollback_boundary="proposal_only_in_m8_deduction_mining",
            )
        )
        coverage["deduction_candidates"] = True
        if is_contradiction or is_question:
            coverage["deduction_clarifications"] = True

    weak_nodes = [
        node
        for node in working or nodes
        if _node_id(node)
        and _node_confidence_floor(node) < 0.45
        and str(node.get("claim_status") or "") not in {"source_metadata", "test_artifact"}
    ]
    weak_nodes.sort(key=lambda node: (_node_confidence_floor(node), _node_id(node)))
    if weak_nodes:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, [_node_id(node) for node in weak_nodes[:12]])
        proposals.append(
            _make_proposal(
                proposal_kind="confidence_review",
                risk_level="medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "graph_source_hygiene",
                        "weak_confidence_nodes",
                        detail={"confidence_floor_min": round(_node_confidence_floor(weak_nodes[0]), 4), "node_count": len(weak_nodes)},
                    )
                ],
                failure_signature_refs=[
                    _evidence_ref("failure_signatures.source", "low_source_or_memory_confidence")
                ],
                proposed_action="review_weak_confidence_before_promotion_or_answer_authority",
                preview_delta={
                    "candidate_count": len(weak_nodes),
                    "lowest_confidence_floor": round(_node_confidence_floor(weak_nodes[0]), 4),
                    "suggested_review": "confirm_source_or_keep_as_uncertain_memory",
                },
                reason="One or more memories have weak memory/evidence/stability confidence and should not silently gain retrieval authority.",
                priority=0.86,
                preview_only=preview_only,
            )
        )
        coverage["weak_confidence"] = True

    source_signatures = [
        dict(item)
        for item in list(dict(failure_signatures.get("source") or {}).get("signatures") or [])
        if isinstance(item, dict)
    ]
    for signature in source_signatures[:8]:
        code = str(signature.get("code") or "source_hygiene").strip() or "source_hygiene"
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _string_list(signature.get("target_node_ids"), limit=12))
        proposals.append(
            _make_proposal(
                proposal_kind="source_hygiene_review",
                risk_level=_risk_from_severity(signature.get("severity"), default="medium"),
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "graph_source_hygiene",
                        code,
                        detail={"count": _safe_int(signature.get("count") or len(target_ids)), "severity": signature.get("severity")},
                    )
                ],
                failure_signature_refs=[_evidence_ref("failure_signatures.source", code)],
                proposed_action="review_source_hygiene_before_consolidation",
                preview_delta={
                    "signature_code": code,
                    "candidate_count": _safe_int(signature.get("count") or len(target_ids)),
                    "suggested_review": "repair_source_metadata_or_keep_review_only",
                },
                reason="Source hygiene signals must be resolved before sleep can promote, merge or answer from affected material.",
                priority=0.82 if code != "document_anchor_missing_raw_text" else 0.91,
                preview_only=preview_only,
            )
        )
        coverage["source_hygiene"] = True

    for candidate in list(duplicate_candidates or [])[:8]:
        if not isinstance(candidate, dict):
            continue
        source_id = str(candidate.get("source_node_id") or "").strip()
        target_id = str(candidate.get("target_node_id") or "").strip()
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, [source_id, target_id])
        if not target_ids:
            continue
        proposals.append(
            _make_proposal(
                proposal_kind="duplicate_review",
                risk_level="medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "sleep_duplicate_candidates",
                        f"{source_id}->{target_id}",
                        detail={"confidence": candidate.get("confidence"), "reason": candidate.get("reason")},
                    )
                ],
                failure_signature_refs=[_evidence_ref("failure_signatures.sleep", "duplicate_candidate")],
                proposed_action="review_duplicate_merge_or_alias_without_losing_source_provenance",
                preview_delta={
                    "source_node_id": source_id,
                    "target_node_id": target_id,
                    "candidate_confidence": _safe_float(candidate.get("confidence") or 0.0),
                    "suggested_review": "merge_alias_or_keep_distinct",
                },
                reason="Near-duplicate memories can improve recall, but merging must preserve raw source provenance and relationship/identity guards.",
                priority=0.8,
                preview_only=preview_only,
            )
        )
        coverage["duplicate_candidates"] = True

    warm_candidates = [
        dict(item)
        for item in list(working_memory_depromotion_policy.get("candidates") or [])
        if isinstance(item, dict)
    ]
    for candidate in warm_candidates[:8]:
        reasons = _string_list(candidate.get("reasons"), limit=8)
        decision = str(candidate.get("decision") or "")
        if decision != "depromote_to_cold_review" and not reasons:
            continue
        node_ids = _string_list(candidate.get("node_ids") or candidate.get("read_node_ids") or candidate.get("contradiction_node_ids"), limit=12)
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, node_ids)
        risk_level = "medium" if "contradiction_risk" in reasons else "low"
        proposals.append(
            _make_proposal(
                proposal_kind="warm_depromotion_review",
                risk_level=risk_level,
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "warm_thread_state",
                        str(candidate.get("last_search_id") or candidate.get("thread_id") or "warm_state"),
                        detail={
                            "thread_id": candidate.get("thread_id"),
                            "decision": decision,
                            "reasons": reasons,
                            "estimated_token_load": candidate.get("estimated_token_load"),
                        },
                    )
                ],
                failure_signature_refs=[
                    _evidence_ref("failure_signatures.working_memory", reason) for reason in reasons
                ],
                proposed_action="review_depromote_or_compress_stale_hot_context_packet",
                preview_delta={
                    "decision": decision,
                    "age_hours": candidate.get("age_hours"),
                    "ttl_hours": candidate.get("ttl_hours"),
                    "estimated_token_load": candidate.get("estimated_token_load"),
                    "suggested_review": "demote_to_cold_or_keep_hot_with_reason",
                },
                reason="Hot context should persist when useful, but stale, contradictory or token-heavy packets need explicit review instead of silent eviction.",
                priority=0.77 if risk_level == "low" else 0.88,
                preview_only=preview_only,
            )
        )
        coverage["stale_hot_context"] = True

    match_hits = dict(trace_insights.get("match_hits") or {})
    candidate_hits = dict(trace_insights.get("candidate_hits") or {})
    useful_node_ids = []
    for node_id, hits in match_hits.items():
        node = nodes_by_id.get(str(node_id))
        if node and _safe_int(hits) >= 1 and _node_confidence_floor(node) >= 0.5:
            useful_node_ids.append(str(node_id))
    for node_id, hits in candidate_hits.items():
        node = nodes_by_id.get(str(node_id))
        if (
            node
            and str(node_id) not in useful_node_ids
            and _safe_int(hits) >= 3
            and _node_confidence_floor(node) >= 0.55
        ):
            useful_node_ids.append(str(node_id))
    if useful_node_ids:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, useful_node_ids[:12])
        proposals.append(
            _make_proposal(
                proposal_kind="cold_promotion_review",
                risk_level="low",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "recent_search_sessions",
                        "repeated_useful_matches",
                        detail={
                            "match_hits": {node_id: match_hits.get(node_id) for node_id in target_ids if node_id in match_hits},
                            "candidate_hits": {node_id: candidate_hits.get(node_id) for node_id in target_ids if node_id in candidate_hits},
                        },
                    )
                ],
                failure_signature_refs=[_evidence_ref("failure_signatures.retrieval", "repeated_useful_cold_evidence")],
                proposed_action="review_promote_repeatedly_useful_cold_evidence_into_reusable_context",
                preview_delta={
                    "candidate_count": len(target_ids),
                    "suggested_review": "promote_to_reusable_context_if_still_source_grounded",
                },
                reason="Repeatedly matched stable nodes are candidates for faster future retrieval, but promotion must remain reviewable.",
                priority=0.74,
                preview_only=preview_only,
            )
        )
        coverage["cold_promotion"] = True

    gap_reasons = dict(retrieval_gap_review.get("gap_reasons") or {})
    final_examples = [
        dict(item)
        for item in list(retrieval_gap_review.get("final_eval_failure_examples") or [])
        if isinstance(item, dict)
    ]
    contradiction_reason_seen = any("contradiction" in str(key).lower() for key in gap_reasons)
    contradiction_reason_seen = contradiction_reason_seen or any(
        "contradiction" in " ".join(str(reason).lower() for reason in list(example.get("reasons") or []))
        for example in final_examples
    )
    target_modes = dict(correction_insights.get("target_modes") or {})
    contradiction_target_ids = [
        str(node_id)
        for node_id, modes in target_modes.items()
        if isinstance(modes, dict) and _safe_int(modes.get("contradiction") or modes.get("supersede") or 0) > 0
    ]
    for candidate in warm_candidates:
        if _safe_int(candidate.get("contradiction_count") or 0) > 0:
            contradiction_reason_seen = True
            contradiction_target_ids.extend(_string_list(candidate.get("contradiction_node_ids") or candidate.get("node_ids"), limit=12))
    for node in nodes:
        if str(node.get("claim_status") or "").lower() in {"contradiction", "superseded"}:
            contradiction_target_ids.append(_node_id(node))
        if str(node.get("memory_act_type") or "").lower() in {"mark_contradiction", "supersede_old_memory"}:
            contradiction_target_ids.append(_node_id(node))
    if contradiction_reason_seen or contradiction_target_ids:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, contradiction_target_ids[:12])
        proposals.append(
            _make_proposal(
                proposal_kind="contradiction_review",
                risk_level="high",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    _evidence_ref(
                        "recent_search_sessions",
                        "contradiction_or_answerability_blockers",
                        detail={"gap_reasons": gap_reasons, "example_count": len(final_examples)},
                    ),
                    _evidence_ref(
                        "correction_history",
                        "contradiction_modes",
                        detail={"target_ids": _string_list(contradiction_target_ids, limit=12)},
                    ),
                ],
                failure_signature_refs=[
                    _evidence_ref("failure_signatures.retrieval", "contradiction_present"),
                    _evidence_ref("failure_signatures.write", "high_impact_write_modes_seen"),
                ],
                proposed_action="review_conflicting_claims_before_answer_or_memory_promotion",
                preview_delta={
                    "contradiction_signal_present": bool(contradiction_reason_seen),
                    "target_count": len(_string_list(contradiction_target_ids, limit=24)),
                    "suggested_review": "resolve_keep_both_with_scope_or_supersede_with_source_trace",
                },
                reason="Contradictory or superseded memories are high-risk because they can corrupt identity, relationships, documents and downstream MCP context.",
                priority=0.95,
                preview_only=preview_only,
            )
        )
        coverage["contradictions"] = True

    ingest_duplicate_events = _ingest_focus_events(ingest_learning_review, "sleep_focus", "duplicate_events")
    if ingest_duplicate_events:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _ingest_event_node_ids(ingest_duplicate_events))
        proposals.append(
            _make_proposal(
                proposal_kind="duplicate_review",
                risk_level="medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=_ingest_evidence_refs(ingest_duplicate_events, ref_id="ingest_duplicate_feedback"),
                failure_signature_refs=[_evidence_ref("failure_signatures.source", "ingest_duplicate_candidate")],
                proposed_action="review_ingest_duplicate_merge_alias_or_keep_distinct_with_source_trace",
                preview_delta={
                    "event_count": len(ingest_duplicate_events),
                    "suggested_review": "merge_alias_or_keep_distinct_after_checking_source_provenance",
                },
                reason="Recent ingest already identified duplicate pressure; sleep should review merge or alias candidates without silently changing memory.",
                priority=0.9,
                preview_only=preview_only,
                source_slice="M3",
                rollback_boundary="proposal_only_in_m3_ingest_feedback",
            )
        )
        coverage["ingest_duplicates"] = True

    ingest_contradiction_events = _ingest_focus_events(ingest_learning_review, "sleep_focus", "contradiction_events")
    if ingest_contradiction_events:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _ingest_event_node_ids(ingest_contradiction_events))
        proposals.append(
            _make_proposal(
                proposal_kind="contradiction_review",
                risk_level="high",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=_ingest_evidence_refs(ingest_contradiction_events, ref_id="ingest_contradiction_feedback"),
                failure_signature_refs=[_evidence_ref("failure_signatures.source", "ingest_contradiction_candidate")],
                proposed_action="review_ingest_conflict_scope_supersede_or_keep_both_with_source_trace",
                preview_delta={
                    "event_count": len(ingest_contradiction_events),
                    "suggested_review": "ask_or_resolve_before_promoting_any_conflicting_candidate",
                },
                reason="Ingest detected a contradiction candidate; maintenance must surface it as a high-risk review item before it can influence retrieval authority.",
                priority=0.97,
                preview_only=preview_only,
                source_slice="M3",
                rollback_boundary="proposal_only_in_m3_ingest_feedback",
            )
        )
        coverage["ingest_contradictions"] = True

    ingest_clarification_events = _ingest_focus_events(ingest_learning_review, "sleep_focus", "clarification_events")
    if ingest_clarification_events:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _ingest_event_node_ids(ingest_clarification_events))
        proposals.append(
            _make_proposal(
                proposal_kind="source_hygiene_review",
                risk_level="medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=_ingest_evidence_refs(ingest_clarification_events, ref_id="ingest_clarification_feedback"),
                failure_signature_refs=[_evidence_ref("failure_signatures.source", "ingest_clarification_required")],
                proposed_action="review_source_scope_clarification_before_consolidation",
                preview_delta={
                    "event_count": len(ingest_clarification_events),
                    "suggested_review": "bind_answered_clarification_or_keep_candidate_review_locked",
                },
                reason="A source-level ambiguity was raised during ingest; sleep should preserve that question/answer chain before consolidating memory.",
                priority=0.88,
                preview_only=preview_only,
                source_slice="M3",
                rollback_boundary="proposal_only_in_m3_ingest_feedback",
            )
        )
        coverage["ingest_clarifications"] = True

    ingest_source_link_events = _ingest_focus_events(ingest_learning_review, "sleep_focus", "source_link_events")
    if ingest_source_link_events:
        target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _ingest_event_node_ids(ingest_source_link_events))
        source_ref_ids = _string_list([event.get("source_ref_id") for event in ingest_source_link_events], limit=12)
        proposals.append(
            _make_proposal(
                proposal_kind="source_hygiene_review",
                risk_level="low" if target_ids else "medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=[
                    *_ingest_evidence_refs(ingest_source_link_events, ref_id="ingest_source_link_feedback"),
                    _evidence_ref("source_references", "ingest_source_refs", detail={"source_ref_ids": source_ref_ids}),
                ],
                failure_signature_refs=[_evidence_ref("failure_signatures.source", "source_reference_binding_review")],
                proposed_action="review_source_reference_binding_raw_asset_or_document_anchor_link",
                preview_delta={
                    "event_count": len(ingest_source_link_events),
                    "source_ref_ids": source_ref_ids,
                    "suggested_review": "bind_source_refs_assets_and_raw_anchor_before_promotion",
                },
                reason="Fresh source units/assets should remain connected to the memories they justify, otherwise later retrieval cannot hydrate evidence reliably.",
                priority=0.83,
                preview_only=preview_only,
                source_slice="M3",
                rollback_boundary="proposal_only_in_m3_ingest_feedback",
            )
        )
        coverage["ingest_source_links"] = True

    proposals = _dedupe_sort_proposals(proposals, max_proposals=max_proposals)
    kind_histogram = _histogram([proposal.get("proposal_kind") for proposal in proposals])
    risk_histogram = _histogram([proposal.get("risk_level") for proposal in proposals])
    required_categories = list(coverage.keys())
    engine_category_coverage = {key: True for key in required_categories}
    profile_seed = {
        "contract_id": contract_id,
        "proposal_ids": [proposal.get("proposal_id") for proposal in proposals],
        "schema_version": SLEEP_CONSOLIDATION_PROPOSAL_ENGINE_SCHEMA_VERSION,
    }
    profile = {
        "schema_version": SLEEP_CONSOLIDATION_PROPOSAL_ENGINE_SCHEMA_VERSION,
        "profile_id": f"sleep_consolidation::{_json_hash(profile_seed)}",
        "slice": "PR-12H-B",
        "generated_from_contract_id": contract_id,
        "mode": mode_family,
        "preview_only": bool(preview_only),
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_count": len(proposals),
        "proposal_budget": int(max_proposals),
        "kind_histogram": kind_histogram,
        "risk_histogram": risk_histogram,
        "coverage": engine_category_coverage,
        "observed_signal_coverage": coverage,
        "proposal_signal_categories": [key for key, value in coverage.items() if value],
        "required_categories": required_categories,
        "uncovered_categories": [key for key, value in engine_category_coverage.items() if not value],
        "coverage_complete": all(engine_category_coverage.values()),
        "deduction_mining": {
            "schema_version": deduction_mining.get("schema_version"),
            "candidate_schema_version": deduction_mining.get("candidate_schema_version"),
            "candidate_count": deduction_mining.get("candidate_count"),
            "support_item_count": deduction_mining.get("support_item_count"),
            "policy": dict(deduction_mining.get("policy") or {}),
        },
        "mutation_boundary": {
            "proposal_engine_mutates_graph": False,
            "proposals_mutate_graph": False,
            "auto_apply_allowed": False,
            "requires_pr12h_d_apply_policy": True,
        },
        "evidence_sources": [
            "maintenance_contract",
            "failure_signatures",
            "working_memory_depromotion_policy",
            "recent_search_sessions",
            "correction_history",
            "graph_source_hygiene",
            "sleep_duplicate_candidates",
            "memory_learning_events",
            "ingest_learning_feedback",
            "source_references",
            "sleep_deduction_mining",
        ],
        "review_required": bool(proposals),
    }
    return {
        "sleep_consolidation_proposals": proposals,
        "sleep_consolidation_profile": profile,
        "deduction_candidates": list(deduction_mining.get("candidates") or []),
        "deduction_mining": deduction_mining,
        "maintenance_proposals": proposals,
        "maintenance_proposal_summary": profile,
    }


def _pair_ids(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if "->" in text:
        return _string_list([part.strip() for part in text.split("->", 1)], limit=2)
    return _string_list([text], limit=1)


def _recommendations_by_prefix(geometry_report: dict[str, Any], *prefixes: str) -> list[dict[str, Any]]:
    prefixes = tuple(str(prefix or "").strip() for prefix in prefixes if str(prefix or "").strip())
    rows = [
        dict(item)
        for item in list(geometry_report.get("recommendations") or [])
        if isinstance(item, dict)
    ]
    if not prefixes:
        return rows
    return [
        row
        for row in rows
        if any(str(row.get("code") or "").startswith(prefix) for prefix in prefixes)
    ]


def _recommendation_targets(recommendations: list[dict[str, Any]], *, limit: int = 24) -> list[str]:
    targets: list[str] = []
    for recommendation in recommendations:
        for target in list(recommendation.get("targets") or []):
            text = str(target or "").strip()
            if not text:
                continue
            if "->" in text:
                targets.extend(_pair_ids(text))
            else:
                targets.append(text)
    return _string_list(targets, limit=limit)


def _recommendation_codes(recommendations: list[dict[str, Any]], *, limit: int = 12) -> list[str]:
    return _string_list([recommendation.get("code") for recommendation in recommendations], limit=limit)


def _geometry_risk(recommendations: list[dict[str, Any]], *, default: str = "medium") -> str:
    severities = {str(recommendation.get("severity") or "").strip().lower() for recommendation in recommendations}
    if "high" in severities:
        return "high"
    if "medium" in severities:
        return "medium"
    if "low" in severities:
        return "low"
    return default


def _build_elastic_attraction_proposals(
    *,
    graph: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    mode_family: str,
    contract_id: str,
    preview_only: bool,
    new_highways: list[dict[str, Any]],
    bridge_promotions: list[dict[str, Any]],
    max_items: int = 3,
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    route_items: list[dict[str, Any]] = []
    for item in list(new_highways or []):
        if not isinstance(item, dict):
            continue
        route_items.append(
            {
                "source_node_id": item.get("source_node_id") or item.get("node_id"),
                "target_node_id": item.get("target_node_id"),
                "strength": item.get("strength"),
                "trace_support": item.get("trace_support"),
                "source": "new_highways",
                "reason": item.get("reason") or "trace_guided_evolve_highway",
            }
        )
    for item in list(bridge_promotions or []):
        if not isinstance(item, dict):
            continue
        route_items.append(
            {
                "source_node_id": item.get("source_node_id") or item.get("node_id"),
                "target_node_id": item.get("target_node_id"),
                "strength": item.get("to") or item.get("strength"),
                "trace_support": item.get("trace_support"),
                "source": "bridge_promotions",
                "reason": item.get("change") or item.get("reason") or "bridge_promotion",
            }
        )

    for item in route_items[: max(0, max_items)]:
        source_id = str(item.get("source_node_id") or "").strip()
        target_id = str(item.get("target_node_id") or "").strip()
        source = nodes_by_id.get(source_id)
        target = nodes_by_id.get(target_id)
        if not source or not target:
            continue
        source_position = _node_position(source)
        target_position = _node_position(target)
        if source_position is None or target_position is None:
            continue
        before_distance = _distance_between(source_position, target_position)
        if before_distance <= 1e-6:
            continue
        evidence_refs = [
            _evidence_ref(
                "mission_learning_rollup",
                "trace_guided_route_or_bridge",
                detail={
                    "source": item.get("source"),
                    "reason": item.get("reason"),
                    "strength": item.get("strength"),
                    "trace_support": item.get("trace_support"),
                },
            )
        ]
        max_displacement = 0.14
        force_vector = _clamp_vector(_scale_vector(_vector_between(source_position, target_position), 0.32), max_displacement)
        source_preview = _elastic_displacement_preview(
            nodes_by_id,
            node_id=source_id,
            force_vector=force_vector,
            role="target_cluster_endpoint",
            dampening=1.0,
            max_displacement=max_displacement,
        )
        target_preview = _elastic_displacement_preview(
            nodes_by_id,
            node_id=target_id,
            force_vector=_scale_vector(force_vector, -1.0),
            role="target_cluster_endpoint",
            dampening=0.65,
            max_displacement=max_displacement,
        )
        previews = [item for item in [source_preview, target_preview] if item]
        affected_neighbor_ids = _neighbor_ids_for_nodes(graph, nodes_by_id, [source_id, target_id], limit=10)
        for neighbor_id in affected_neighbor_ids:
            neighbor_preview = _elastic_displacement_preview(
                nodes_by_id,
                node_id=neighbor_id,
                force_vector=force_vector,
                role="elastic_neighbor_drag",
                dampening=0.35,
                max_displacement=max_displacement,
            )
            if neighbor_preview:
                previews.append(neighbor_preview)
        projected_source = dict(source_preview.get("projected_position") or source_position) if source_preview else source_position
        projected_target = dict(target_preview.get("projected_position") or target_position) if target_preview else target_position
        after_distance = _distance_between(projected_source, projected_target)
        target_node_ids, document_ids, region_ids = _node_targets(nodes_by_id, [source_id, target_id, *affected_neighbor_ids])
        elastic_payload = _elastic_topology_payload(
            intent="strengthen_bridge",
            evidence_refs=evidence_refs,
            target_node_ids=[source_id, target_id],
            affected_neighbor_ids=affected_neighbor_ids,
            node_displacement_preview=previews,
            before_after_metrics={
                "before_route_distance": before_distance,
                "projected_route_distance": after_distance,
                "route_distance_delta": round(after_distance - before_distance, 6),
                "affected_region_density": _region_density(nodes_by_id, target_node_ids),
                "expected_retrieve_improvement": {
                    "basis": "shorter_repeated_route_or_stronger_bridge",
                    "estimated_delta": round(min(0.18, max(0.03, before_distance - after_distance)), 6),
                },
            },
            force_vector=force_vector,
            max_displacement=max_displacement,
            risk_level="medium" if any(_node_anchor_class(nodes_by_id.get(node_id, {})) != "movable_memory" for node_id in [source_id, target_id]) else "low",
        )
        proposals.append(
            _make_proposal(
                proposal_kind="elastic_topology_deformation_review",
                risk_level=str(elastic_payload["risk_level"]),
                mode_family=mode_family,
                contract_id=contract_id,
                target_node_ids=target_node_ids,
                target_document_ids=document_ids,
                target_region_ids=region_ids,
                evidence_refs=evidence_refs,
                failure_signature_refs=[_evidence_ref("failure_signatures.geometry", "repeated_bridge_or_route_correction")],
                proposed_action="review_elastic_attraction_and_neighbor_drag_for_repeated_route",
                preview_delta={
                    "elastic_topology_proposal": elastic_payload,
                    "topology_change_class": "elastic_local_deformation",
                    "suggested_review": "approve_only_if_route_distance_improves_without_moving_protected_anchors",
                },
                reason="Repeated route or bridge evidence can justify pulling co-resolved nodes closer, while linked neighbors move only through damped elastic drag.",
                priority=0.93,
                preview_only=preview_only,
                source_slice="BAM-6B",
                rollback_boundary="proposal_only_in_bam6b",
            )
        )
    return proposals


def _build_elastic_repulsion_proposal(
    *,
    graph: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    mode_family: str,
    contract_id: str,
    preview_only: bool,
    geometry_report: dict[str, Any],
) -> dict[str, Any] | None:
    density_recommendations = _recommendations_by_prefix(geometry_report, "landing_density", "spacing")
    target_ids = _recommendation_targets(density_recommendations, limit=16)
    spacing = dict(geometry_report.get("spacing") or {})
    landing_density = dict(geometry_report.get("landing_density") or {})
    for example in list(spacing.get("collision_examples") or [])[:8]:
        if isinstance(example, dict):
            target_ids.extend([example.get("source_id"), example.get("target_id"), example.get("node_id")])
    for bucket in list(landing_density.get("top_buckets") or [])[:4]:
        if not isinstance(bucket, dict):
            continue
        target_ids.extend(_string_list(bucket.get("node_ids") or bucket.get("examples"), limit=8))
    target_ids = _string_list([node_id for node_id in target_ids if str(node_id or "") in nodes_by_id], limit=12)
    positioned = [(node_id, _node_position(nodes_by_id[node_id])) for node_id in target_ids]
    positioned = [(node_id, position) for node_id, position in positioned if position is not None]
    if len(positioned) < 2:
        return None
    positions = [position for _node_id_value, position in positioned]
    centroid = _centroid_for_positions(positions)
    if centroid is None:
        return None
    max_displacement = 0.1
    previews: list[dict[str, Any]] = []
    min_before = min(
        _distance_between(left, right)
        for index, left in enumerate(positions)
        for right in positions[index + 1 :]
    )
    projected_positions: list[dict[str, float]] = []
    for index, (node_id, position) in enumerate(positioned[:10]):
        vector = _vector_between(centroid, position)
        if _vector_length(vector) <= 1e-9:
            angle = (index + 1) * 1.61803398875
            vector = {"x": math.cos(angle), "y": math.sin(angle), "z": 0.0}
        force_vector = _clamp_vector(_scale_vector(vector, 0.42), max_displacement)
        preview = _elastic_displacement_preview(
            nodes_by_id,
            node_id=node_id,
            force_vector=force_vector,
            role="repulsion_target",
            dampening=1.0,
            max_displacement=max_displacement,
        )
        if preview:
            previews.append(preview)
            projected_positions.append(dict(preview.get("projected_position") or position))
    if len(previews) < 2:
        return None
    min_after = min(
        _distance_between(left, right)
        for index, left in enumerate(projected_positions)
        for right in projected_positions[index + 1 :]
    )
    affected_neighbor_ids = _neighbor_ids_for_nodes(graph, nodes_by_id, [item[0] for item in positioned[:10]], limit=10)
    evidence_refs = [
        _evidence_ref(
            "brain_geometry_calibration",
            "density_or_spacing_crowding",
            detail={
                "recommendation_codes": _recommendation_codes(density_recommendations),
                "spacing_score": spacing.get("score"),
                "landing_density_score": landing_density.get("score"),
            },
        )
    ]
    target_node_ids, document_ids, region_ids = _node_targets(nodes_by_id, [item[0] for item in positioned[:10]])
    elastic_payload = _elastic_topology_payload(
        intent="split_dense_region",
        evidence_refs=evidence_refs,
        target_node_ids=target_node_ids,
        affected_neighbor_ids=affected_neighbor_ids,
        node_displacement_preview=previews,
        before_after_metrics={
            "before_min_spacing": round(min_before, 6),
            "projected_min_spacing": round(min_after, 6),
            "spacing_delta": round(min_after - min_before, 6),
            "affected_region_density": _region_density(nodes_by_id, target_node_ids),
            "expected_retrieve_improvement": {
                "basis": "reduce_false_hot_context_and_local_collision",
                "estimated_delta": round(max(0.0, min(0.12, min_after - min_before)), 6),
            },
        },
        force_vector={"x": 0.0, "y": 0.0, "z": 0.0},
        max_displacement=max_displacement,
        risk_level=_geometry_risk(density_recommendations, default="medium"),
    )
    return _make_proposal(
        proposal_kind="elastic_topology_deformation_review",
        risk_level=str(elastic_payload["risk_level"]),
        mode_family=mode_family,
        contract_id=contract_id,
        target_node_ids=target_node_ids,
        target_document_ids=document_ids,
        target_region_ids=region_ids,
        evidence_refs=evidence_refs,
        failure_signature_refs=[
            _evidence_ref("failure_signatures.geometry", code)
            for code in (_recommendation_codes(density_recommendations) or ["density_or_spacing_crowding"])
        ],
        proposed_action="review_elastic_repulsion_for_crowded_or_noisy_region",
        preview_delta={
            "elastic_topology_proposal": elastic_payload,
            "topology_change_class": "elastic_local_deformation",
            "suggested_review": "approve_only_if_spacing_improves_without_source_or_document_anchor_damage",
        },
        reason="Crowded local regions can make retrieval look spherical and noisy; elastic repulsion proposes a bounded local spread without changing the matrix.",
        priority=0.89,
        preview_only=preview_only,
        source_slice="BAM-6B",
        rollback_boundary="proposal_only_in_bam6b",
    )


def _build_matrix_candidate_elastic_proposal(
    *,
    nodes_by_id: dict[str, dict[str, Any]],
    mode_family: str,
    contract_id: str,
    preview_only: bool,
    geometry_report: dict[str, Any],
    region_actions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matrix_recommendations = _recommendations_by_prefix(geometry_report, "radial_drift", "zone_overlap", "semantic_radial")
    if not matrix_recommendations:
        return None
    target_ids = _recommendation_targets(matrix_recommendations, limit=16)
    for item in list(region_actions or [])[:8]:
        if isinstance(item, dict):
            target_ids.extend(_string_list(item.get("node_ids") or item.get("target_node_ids"), limit=6))
    target_ids = _string_list([node_id for node_id in target_ids if str(node_id or "") in nodes_by_id], limit=12)
    evidence_refs = [
        _evidence_ref(
            "brain_geometry_calibration",
            "projection_level_drift",
            detail={
                "recommendation_codes": _recommendation_codes(matrix_recommendations),
                "overall_score": geometry_report.get("overall_score"),
                "region_action_count": len(list(region_actions or [])),
            },
        )
    ]
    target_node_ids, document_ids, region_ids = _node_targets(nodes_by_id, target_ids)
    elastic_payload = _elastic_topology_payload(
        intent="matrix_calibration_candidate",
        evidence_refs=evidence_refs,
        target_node_ids=target_node_ids,
        affected_neighbor_ids=[],
        node_displacement_preview=[],
        before_after_metrics={
            "matrix_candidate_only": True,
            "ordinary_elastic_move_deferred": True,
            "recommendation_codes": _recommendation_codes(matrix_recommendations),
            "expected_retrieve_improvement": {
                "basis": "projection_level_drift_requires_matrix_preview_not_local_elastic_move",
                "estimated_delta": 0.0,
            },
        },
        force_vector={"x": 0.0, "y": 0.0, "z": 0.0},
        max_displacement=0.0,
        risk_level=_geometry_risk(matrix_recommendations, default="high"),
        matrix_calibration_candidate=True,
    )
    return _make_proposal(
        proposal_kind="elastic_topology_deformation_review",
        risk_level=str(elastic_payload["risk_level"]),
        mode_family=mode_family,
        contract_id=contract_id,
        target_node_ids=target_node_ids,
        target_document_ids=document_ids,
        target_region_ids=region_ids,
        evidence_refs=evidence_refs,
        failure_signature_refs=[
            _evidence_ref("failure_signatures.geometry", code)
            for code in _recommendation_codes(matrix_recommendations)
        ],
        proposed_action="route_to_matrix_calibration_preview_instead_of_local_elastic_move",
        preview_delta={
            "elastic_topology_proposal": elastic_payload,
            "topology_change_class": "matrix_calibration_candidate",
            "suggested_review": "run_matrix_calibration_preview_before_any_local_elastic_apply",
        },
        reason="Projection-level drift should not be hidden inside local evolve movement; it must route to explicit matrix calibration preview and rollback.",
        priority=0.91,
        preview_only=preview_only,
        source_slice="BAM-6B",
        rollback_boundary="proposal_only_in_bam6b",
    )


def build_elastic_topology_deformation_proposals(
    *,
    graph: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    mode_family: str,
    contract_id: str,
    preview_only: bool,
    geometry_report: dict[str, Any],
    new_highways: list[dict[str, Any]],
    bridge_promotions: list[dict[str, Any]],
    bridge_demotions: list[dict[str, Any]],
    region_actions: list[dict[str, Any]],
    max_proposals: int = 6,
) -> dict[str, Any]:
    del bridge_demotions  # demotions are represented by repulsion/density in this slice.
    active = mode_family in {"evolve", "sleep_evolve"}
    proposals: list[dict[str, Any]] = []
    if active:
        proposals.extend(
            _build_elastic_attraction_proposals(
                graph=graph,
                nodes_by_id=nodes_by_id,
                mode_family=mode_family,
                contract_id=contract_id,
                preview_only=preview_only,
                new_highways=new_highways,
                bridge_promotions=bridge_promotions,
                max_items=3,
            )
        )
        repulsion = _build_elastic_repulsion_proposal(
            graph=graph,
            nodes_by_id=nodes_by_id,
            mode_family=mode_family,
            contract_id=contract_id,
            preview_only=preview_only,
            geometry_report=geometry_report,
        )
        if repulsion:
            proposals.append(repulsion)
        matrix_candidate = _build_matrix_candidate_elastic_proposal(
            nodes_by_id=nodes_by_id,
            mode_family=mode_family,
            contract_id=contract_id,
            preview_only=preview_only,
            geometry_report=geometry_report,
            region_actions=region_actions,
        )
        if matrix_candidate:
            proposals.append(matrix_candidate)

    proposals = _dedupe_sort_proposals(proposals, max_proposals=max_proposals)
    elastic_payloads = [
        dict(dict(proposal.get("preview_delta") or {}).get("elastic_topology_proposal") or {})
        for proposal in proposals
    ]
    intent_histogram = _histogram([payload.get("intent") for payload in elastic_payloads if payload])
    matrix_candidate_count = sum(1 for payload in elastic_payloads if bool(payload.get("matrix_calibration_candidate")))
    profile_seed = {
        "proposal_ids": [proposal.get("proposal_id") for proposal in proposals],
        "schema_version": ELASTIC_TOPOLOGY_PROPOSAL_SCHEMA_VERSION,
    }
    return {
        "elastic_topology_proposals": elastic_payloads,
        "elastic_topology_review_proposals": proposals,
        "elastic_topology_profile": {
            "schema_version": "agvm.elastic_topology_profile.v1",
            "profile_id": f"elastic_topology::{_json_hash(profile_seed)}",
            "active": bool(active),
            "preview_only": bool(preview_only),
            "proposal_schema_version": ELASTIC_TOPOLOGY_PROPOSAL_SCHEMA_VERSION,
            "proposal_count": len(elastic_payloads),
            "intent_histogram": intent_histogram,
            "matrix_calibration_candidate_count": matrix_candidate_count,
            "ordinary_elastic_move_count": max(0, len(elastic_payloads) - matrix_candidate_count),
            "ai_role": "bounded_intent_classifier_from_compact_region_and_mission_learning; backend_solver_computes_coordinates",
            "raw_node_text_policy": "no_full_brain_text; proposals use ids, compact geometry metrics and mission-learning evidence",
            "mutation_boundary": {
                "preview_mutates_graph": False,
                "coordinates_mutated": False,
                "apply_requires_transaction": True,
                "rollback_required": True,
            },
            "solver_policy": {
                "attraction": "co_resolved_or_bridge_nodes_pull_together",
                "neighbor_dragging": "linked_neighbors_receive_damped_force",
                "repulsion": "crowded_or_noisy_regions_spread_locally",
                "anchor_preservation": "document_anchors_pinned_identity_and_source_anchors_micro_shift_only",
                "matrix_separation": "projection_drift_routes_to_matrix_calibration_candidate",
            },
        },
    }


def build_evolve_structural_proposal_engine(
    *,
    mode: str,
    preview_only: bool,
    graph: dict[str, Any],
    working_nodes: list[dict[str, Any]],
    maintenance_baseline_contract: dict[str, Any],
    geometry_report: dict[str, Any],
    duplicate_candidates: list[dict[str, Any]],
    merges: list[dict[str, Any]],
    pattern_candidates: list[dict[str, Any]],
    repositioned_nodes: list[dict[str, Any]],
    retyped_nodes: list[dict[str, Any]],
    new_highways: list[dict[str, Any]],
    bridge_promotions: list[dict[str, Any]],
    bridge_demotions: list[dict[str, Any]],
    region_actions: list[dict[str, Any]],
    highway_calibration_profile: dict[str, Any],
    evolve_profile: dict[str, Any],
    ingest_learning_review: dict[str, Any] | None = None,
    max_proposals: int = 24,
) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    working = [dict(node) for node in list(working_nodes or []) if isinstance(node, dict)]
    nodes_by_id = {_node_id(node): node for node in nodes if _node_id(node)}
    maintenance_contract = dict(maintenance_baseline_contract.get("maintenance_contract") or {})
    failure_signatures = dict(maintenance_baseline_contract.get("failure_signatures") or {})
    contract_id = str(maintenance_contract.get("contract_id") or "maintenance_contract::unknown")
    mode_family = str(mode or maintenance_contract.get("mode") or "sleep_evolve")
    active = mode_family in {"evolve", "sleep_evolve"}
    proposals: list[dict[str, Any]] = []
    coverage = {
        "merges": False,
        "splits": False,
        "highways": False,
        "bridge_nodes": False,
        "document_project_coupling": False,
        "geometry_corrections": False,
        "elastic_topology": False,
        "ingest_topology": False,
        "ingest_matrix_hints": False,
    }
    elastic_topology = {
        "elastic_topology_proposals": [],
        "elastic_topology_review_proposals": [],
        "elastic_topology_profile": {},
    }

    if active:
        merge_candidates = [
            dict(item)
            for item in list(merges or [])
            if isinstance(item, dict)
        ]
        if not merge_candidates:
            merge_candidates = [
                dict(item)
                for item in list(duplicate_candidates or [])
                if isinstance(item, dict)
            ]
        if merge_candidates:
            merge_target_ids: list[str] = []
            for item in merge_candidates[:10]:
                merge_target_ids.extend([item.get("source_node_id"), item.get("target_node_id")])
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _string_list(merge_target_ids, limit=20))
            proposals.append(
                _make_proposal(
                    proposal_kind="merge_review",
                    risk_level="medium",
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=[
                        _evidence_ref(
                            "sleep_duplicate_candidates",
                            "structural_merge_candidates",
                            detail={"candidate_count": len(merge_candidates), "examples": merge_candidates[:5]},
                        )
                    ],
                    failure_signature_refs=[_evidence_ref("failure_signatures.source", "duplicate_or_overlapping_memory")],
                    proposed_action="review_structural_merge_or_alias_without_losing_source_provenance",
                    preview_delta={
                        "candidate_count": len(merge_candidates),
                        "suggested_review": "merge_alias_or_keep_distinct_with_source_trace",
                    },
                    reason="Evolve can consolidate repeated memories, but only after review confirms no source, document, identity or relationship evidence would be lost.",
                    priority=0.84,
                    preview_only=preview_only,
                    source_slice="PR-12H-C",
                    rollback_boundary="proposal_only_in_pr12h_c",
                )
            )
            coverage["merges"] = True

        density_recommendations = _recommendations_by_prefix(geometry_report, "landing_density", "spacing")
        density_bucket_ids = [
            str(target)
            for recommendation in density_recommendations
            for target in list(recommendation.get("targets") or [])
            if ":" in str(target)
        ]
        density_node_ids = [
            str(target)
            for recommendation in density_recommendations
            for target in list(recommendation.get("targets") or [])
            if ":" not in str(target)
        ]
        if density_recommendations:
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _string_list(density_node_ids, limit=16))
            region_ids = _string_list([*region_ids, *density_bucket_ids], limit=24)
            proposals.append(
                _make_proposal(
                    proposal_kind="split_review",
                    risk_level=_geometry_risk(density_recommendations, default="medium"),
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=[
                        _evidence_ref(
                            "brain_geometry_calibration",
                            "local_density_or_spacing",
                            detail={
                                "recommendation_codes": _recommendation_codes(density_recommendations),
                                "top_buckets": list(dict(geometry_report.get("landing_density") or {}).get("top_buckets") or [])[:6],
                                "collision_examples": list(dict(geometry_report.get("spacing") or {}).get("collision_examples") or [])[:6],
                            },
                        )
                    ],
                    failure_signature_refs=[
                        _evidence_ref("failure_signatures.geometry", code)
                        for code in _recommendation_codes(density_recommendations)
                    ],
                    proposed_action="review_split_or_rebalance_crowded_local_memory_regions",
                    preview_delta={
                        "recommendation_codes": _recommendation_codes(density_recommendations),
                        "suggested_review": "split_crowded_bucket_or_reposition_colliding_nodes",
                    },
                    reason="Crowded buckets and spacing collisions can hide useful local neighborhoods; evolve should split or rebalance them only through reviewable proposals.",
                    priority=0.81,
                    preview_only=preview_only,
                    source_slice="PR-12H-C",
                    rollback_boundary="proposal_only_in_pr12h_c",
                )
            )
            coverage["splits"] = True

        highway_recommendations = _recommendations_by_prefix(geometry_report, "highways")
        highway_target_ids = _recommendation_targets(highway_recommendations, limit=20)
        for item in list(new_highways or [])[:10]:
            if isinstance(item, dict):
                highway_target_ids.extend(_string_list([item.get("source_node_id"), item.get("target_node_id")], limit=2))
        for item in list(bridge_promotions or [])[:10] + list(bridge_demotions or [])[:10]:
            if isinstance(item, dict):
                highway_target_ids.extend(_string_list([item.get("node_id"), item.get("source_node_id"), item.get("target_node_id")], limit=3))
        highway_target_ids = _string_list(highway_target_ids, limit=24)
        if highway_recommendations or new_highways or bridge_promotions or bridge_demotions:
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, highway_target_ids)
            proposals.append(
                _make_proposal(
                    proposal_kind="highway_review",
                    risk_level=_geometry_risk(highway_recommendations, default="medium"),
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=[
                        _evidence_ref(
                            "brain_geometry_calibration",
                            "highway_quality",
                            detail={
                                "recommendation_codes": _recommendation_codes(highway_recommendations),
                                "highway_quality": dict(geometry_report.get("highway_quality") or {}),
                                "new_highway_count": len(list(new_highways or [])),
                                "bridge_promotion_count": len(list(bridge_promotions or [])),
                                "bridge_demotion_count": len(list(bridge_demotions or [])),
                                "sparse_highway_profile": dict(highway_calibration_profile or {}),
                            },
                        )
                    ],
                    failure_signature_refs=[
                        _evidence_ref("failure_signatures.geometry", code)
                        for code in _recommendation_codes(highway_recommendations)
                    ],
                    proposed_action="review_highway_backfill_prune_or_reciprocal_route_changes",
                    preview_delta={
                        "new_highway_count": len(list(new_highways or [])),
                        "bridge_promotion_count": len(list(bridge_promotions or [])),
                        "bridge_demotion_count": len(list(bridge_demotions or [])),
                        "suggested_review": "apply_only_routes_that_improve_context_path_truth",
                    },
                    reason="Highways are sparse-brain corridors; stale, missing or low-relatedness routes must be reviewed before becoming retrieval authority.",
                    priority=0.9 if highway_recommendations else 0.79,
                    preview_only=preview_only,
                    source_slice="PR-12H-C",
                    rollback_boundary="proposal_only_in_pr12h_c",
                )
            )
            coverage["highways"] = True

        corridor_recommendations = _recommendations_by_prefix(geometry_report, "path_corridor")
        corridor = dict(geometry_report.get("path_bridge_potential") or {})
        corridor_examples = [
            dict(item)
            for item in list(corridor.get("examples") or [])
            if isinstance(item, dict)
        ]
        bridge_target_ids: list[str] = _recommendation_targets(corridor_recommendations, limit=16)
        for example in corridor_examples[:8]:
            bridge_target_ids.extend([example.get("source_id"), example.get("target_id")])
            for bridge in list(example.get("bridge_examples") or [])[:4]:
                if isinstance(bridge, dict):
                    bridge_target_ids.append(bridge.get("node_id"))
        bridge_target_ids = _string_list(bridge_target_ids, limit=24)
        if corridor_recommendations or int(corridor.get("dead_path_count") or 0) > 0 or bridge_target_ids:
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, bridge_target_ids)
            proposals.append(
                _make_proposal(
                    proposal_kind="highway_review",
                    risk_level=_geometry_risk(corridor_recommendations, default="medium"),
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=[
                        _evidence_ref(
                            "brain_geometry_calibration",
                            "path_bridge_potential",
                            detail={
                                "recommendation_codes": _recommendation_codes(corridor_recommendations),
                                "dead_path_count": corridor.get("dead_path_count"),
                                "meaningful_bridge_yield_avg": corridor.get("meaningful_bridge_yield_avg"),
                                "examples": corridor_examples[:5],
                            },
                        )
                    ],
                    failure_signature_refs=[
                        _evidence_ref("failure_signatures.geometry", code)
                        for code in _recommendation_codes(corridor_recommendations)
                    ],
                    proposed_action="review_bridge_node_or_corridor_read_policy_for_routes",
                    preview_delta={
                        "dead_path_count": corridor.get("dead_path_count"),
                        "meaningful_bridge_yield_avg": corridor.get("meaningful_bridge_yield_avg"),
                        "suggested_review": "add_bridge_nodes_or_route_read_policy_before_point_to_point_jump",
                    },
                    reason="A path should harvest useful intermediate context; bridge-node proposals keep routes from becoming flat point-to-point jumps.",
                    priority=0.86,
                    preview_only=preview_only,
                    source_slice="PR-12H-C",
                    rollback_boundary="proposal_only_in_pr12h_c",
                )
            )
            coverage["bridge_nodes"] = True

        document_project_recommendations = _recommendations_by_prefix(geometry_report, "document_project")
        coupling = dict(geometry_report.get("document_project_coupling") or {})
        orphan_examples = [
            dict(item)
            for item in list(coupling.get("examples") or [])
            if isinstance(item, dict) and not bool(item.get("passes"))
        ]
        unlinked_pass_examples = [
            dict(item)
            for item in list(coupling.get("examples") or [])
            if isinstance(item, dict) and bool(item.get("passes")) and not bool(item.get("directly_connected"))
        ]
        coupling_review_examples = [*orphan_examples, *unlinked_pass_examples]
        coupling_node_ids: list[str] = _recommendation_targets(document_project_recommendations, limit=16)
        for example in coupling_review_examples[:8]:
            coupling_node_ids.extend([example.get("document_id"), example.get("nearest_project_id")])
        if document_project_recommendations or coupling_review_examples:
            risk_level = "high" if document_project_recommendations or orphan_examples else "low"
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _string_list(coupling_node_ids, limit=24))
            proposals.append(
                _make_proposal(
                    proposal_kind="document_project_coupling_review",
                    risk_level=risk_level,
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=[
                        _evidence_ref(
                            "brain_geometry_calibration",
                            "document_project_coupling",
                            detail={
                                "recommendation_codes": _recommendation_codes(document_project_recommendations),
                                "score": coupling.get("score"),
                                "orphan_document_count": coupling.get("orphan_document_count"),
                                "examples": coupling_review_examples[:6],
                                "unlinked_pass_count": len(unlinked_pass_examples),
                            },
                        )
                    ],
                    failure_signature_refs=[
                        _evidence_ref("failure_signatures.geometry", code)
                        for code in _recommendation_codes(document_project_recommendations)
                    ],
                    proposed_action="review_document_project_coupling_links_or_geometry",
                    preview_delta={
                        "orphan_document_count": coupling.get("orphan_document_count"),
                        "score": coupling.get("score"),
                        "unlinked_pass_count": len(unlinked_pass_examples),
                        "suggested_review": "link_or_reposition_documents_near_project_workspace",
                    },
                    reason="Documents are first-class memory; orphaned documents weaken exact document retrieval, project workspace retrieval and MCP context packages.",
                    priority=0.92,
                    preview_only=preview_only,
                    source_slice="PR-12H-C",
                    rollback_boundary="proposal_only_in_pr12h_c",
                )
            )
            coverage["document_project_coupling"] = True

        geometry_recommendations = _recommendations_by_prefix(geometry_report, "radial_drift", "zone_overlap")
        explicit_geometry_node_ids = _recommendation_targets(geometry_recommendations, limit=20)
        for item in list(repositioned_nodes or [])[:10] + list(retyped_nodes or [])[:10]:
            if isinstance(item, dict):
                explicit_geometry_node_ids.append(item.get("node_id"))
        for item in list(region_actions or [])[:10]:
            if isinstance(item, dict):
                explicit_geometry_node_ids.extend(_string_list(item.get("node_ids") or item.get("target_node_ids"), limit=8))
        explicit_geometry_node_ids = _string_list(explicit_geometry_node_ids, limit=24)
        if geometry_recommendations or repositioned_nodes or retyped_nodes or region_actions:
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, explicit_geometry_node_ids)
            proposals.append(
                _make_proposal(
                    proposal_kind="geometry_reposition_review",
                    risk_level=_geometry_risk(geometry_recommendations, default="medium"),
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=[
                        _evidence_ref(
                            "brain_geometry_calibration",
                            "radial_zone_or_region_rebalance",
                            detail={
                                "recommendation_codes": _recommendation_codes(geometry_recommendations),
                                "repositioned_count": len(list(repositioned_nodes or [])),
                                "retyped_count": len(list(retyped_nodes or [])),
                                "region_action_count": len(list(region_actions or [])),
                                "evolve_profile": dict(evolve_profile or {}),
                            },
                        )
                    ],
                    failure_signature_refs=[
                        _evidence_ref("failure_signatures.geometry", code)
                        for code in _recommendation_codes(geometry_recommendations)
                    ],
                    proposed_action="review_radial_zone_memory_type_or_region_rebalance",
                    preview_delta={
                        "repositioned_count": len(list(repositioned_nodes or [])),
                        "retyped_count": len(list(retyped_nodes or [])),
                        "region_action_count": len(list(region_actions or [])),
                        "suggested_review": "apply_only_if_geometry_score_and_retrieval_context_improve",
                    },
                    reason="Matrix geometry is retrieval intelligence; retyping and radial moves need reviewable before/after evidence before becoming durable memory layout.",
                    priority=0.87,
                    preview_only=preview_only,
                    source_slice="PR-12H-C",
                    rollback_boundary="proposal_only_in_pr12h_c",
                )
            )
            coverage["geometry_corrections"] = True

        elastic_topology = build_elastic_topology_deformation_proposals(
            graph=graph,
            nodes_by_id=nodes_by_id,
            mode_family=mode_family,
            contract_id=contract_id,
            preview_only=preview_only,
            geometry_report=geometry_report,
            new_highways=new_highways,
            bridge_promotions=bridge_promotions,
            bridge_demotions=bridge_demotions,
            region_actions=region_actions,
        )
        elastic_review_proposals = [
            dict(item)
            for item in list(elastic_topology.get("elastic_topology_review_proposals") or [])
            if isinstance(item, dict)
        ]
        if elastic_review_proposals:
            proposals.extend(elastic_review_proposals)
            coverage["elastic_topology"] = True

        ingest_topology_events = _ingest_focus_events(ingest_learning_review, "evolve_focus", "topology_events")
        if ingest_topology_events:
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _ingest_event_node_ids(ingest_topology_events))
            proposals.append(
                _make_proposal(
                    proposal_kind="highway_review",
                    risk_level="medium",
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=_ingest_evidence_refs(ingest_topology_events, ref_id="ingest_topology_feedback"),
                    failure_signature_refs=[_evidence_ref("failure_signatures.geometry", "ingest_topology_or_route_hint")],
                    proposed_action="review_ingest_topology_route_highway_or_bridge_change",
                    preview_delta={
                        "event_count": len(ingest_topology_events),
                        "suggested_review": "evaluate_topology_or_highway_prior_from_repeated_ingest_feedback",
                    },
                    reason="Repeated ingest/source feedback can reveal route or bridge structure that search traces have not yet exercised.",
                    priority=0.88,
                    preview_only=preview_only,
                    source_slice="M3",
                    rollback_boundary="proposal_only_in_m3_ingest_feedback",
                )
            )
            coverage["ingest_topology"] = True

        ingest_matrix_events = _ingest_focus_events(ingest_learning_review, "evolve_focus", "matrix_hint_events")
        if ingest_matrix_events:
            target_ids, document_ids, region_ids = _node_targets(nodes_by_id, _ingest_event_node_ids(ingest_matrix_events))
            proposals.append(
                _make_proposal(
                    proposal_kind="elastic_topology_deformation_review",
                    risk_level="medium",
                    mode_family=mode_family,
                    contract_id=contract_id,
                    target_node_ids=target_ids,
                    target_document_ids=document_ids,
                    target_region_ids=region_ids,
                    evidence_refs=_ingest_evidence_refs(ingest_matrix_events, ref_id="ingest_matrix_hint_feedback"),
                    failure_signature_refs=[_evidence_ref("failure_signatures.geometry", "ingest_matrix_hint")],
                    proposed_action="route_ingest_matrix_hints_to_matrix_calibration_preview",
                    preview_delta={
                        "topology_change_class": "matrix_calibration_candidate",
                        "event_count": len(ingest_matrix_events),
                        "matrix_candidate_only": True,
                        "suggested_review": "run_matrix_calibration_preview_before_any_coordinate_or_matrix_apply",
                    },
                    reason="Ingest placement hints can improve the learned matrix, but they must become explicit calibration candidates rather than hidden coordinate edits.",
                    priority=0.9,
                    preview_only=preview_only,
                    source_slice="M3",
                    rollback_boundary="proposal_only_in_m3_ingest_feedback",
                )
            )
            coverage["ingest_matrix_hints"] = True

    proposals = _dedupe_sort_proposals(proposals, max_proposals=max_proposals)
    kind_histogram = _histogram([proposal.get("proposal_kind") for proposal in proposals])
    risk_histogram = _histogram([proposal.get("risk_level") for proposal in proposals])
    required_categories = list(coverage.keys())
    engine_category_coverage = {key: True for key in required_categories}
    geometry_proposals = [
        dict(item)
        for item in list(geometry_report.get("calibration_proposals") or [])
        if isinstance(item, dict)
    ]
    profile_seed = {
        "contract_id": contract_id,
        "proposal_ids": [proposal.get("proposal_id") for proposal in proposals],
        "schema_version": EVOLVE_STRUCTURAL_PROPOSAL_ENGINE_SCHEMA_VERSION,
    }
    profile = {
        "schema_version": EVOLVE_STRUCTURAL_PROPOSAL_ENGINE_SCHEMA_VERSION,
        "profile_id": f"evolve_structural::{_json_hash(profile_seed)}",
        "slice": "PR-12H-C",
        "active": bool(active),
        "generated_from_contract_id": contract_id,
        "mode": mode_family,
        "preview_only": bool(preview_only),
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_count": len(proposals),
        "proposal_budget": int(max_proposals),
        "kind_histogram": kind_histogram,
        "risk_histogram": risk_histogram,
        "coverage": engine_category_coverage,
        "observed_signal_coverage": coverage,
        "proposal_signal_categories": [key for key, value in coverage.items() if value],
        "required_categories": required_categories,
        "uncovered_categories": [key for key, value in engine_category_coverage.items() if not value],
        "coverage_complete": all(engine_category_coverage.values()),
        "geometry_consumption": {
            "schema_version": geometry_report.get("schema_version"),
            "overall_score": geometry_report.get("overall_score"),
            "recommendation_count": len(list(geometry_report.get("recommendations") or [])),
            "calibration_proposal_codes": _string_list([item.get("proposal_code") for item in geometry_proposals], limit=12),
            "benchmark_checks": dict(dict(geometry_report.get("benchmarks") or {}).get("checks") or {}),
        },
        "elastic_topology_profile": dict(elastic_topology.get("elastic_topology_profile") or {}),
        "mutation_boundary": {
            "proposal_engine_mutates_graph": False,
            "proposals_mutate_graph": False,
            "auto_apply_allowed": False,
            "requires_pr12h_d_apply_policy": True,
            "requires_before_after_geometry_measurement": True,
        },
        "evidence_sources": [
            "maintenance_contract",
            "brain_geometry_calibration",
            "failure_signatures.geometry",
            "sleep_duplicate_candidates",
            "evolve_profile",
            "highway_calibration_profile",
            "elastic_topology_solver",
            "mission_learning_rollup",
            "memory_learning_events",
            "ingest_learning_feedback",
        ],
        "review_required": bool(proposals),
    }
    return {
        "evolve_structural_proposals": proposals,
        "elastic_topology_proposals": list(elastic_topology.get("elastic_topology_proposals") or []),
        "evolve_structural_profile": profile,
    }


def _pr12a_to_g_regression_contracts() -> list[dict[str, Any]]:
    return [
        {
            "slice": "PR-12A",
            "contract": "SemanticQueryContractV2",
            "protected_surface": "semantic_contract",
            "required_test": "tests/test_pr12_semantic_contract.py",
        },
        {
            "slice": "PR-12B",
            "contract": "MCP Context Package V2",
            "protected_surface": "context_package",
            "required_test": "tests/test_pr12b_context_package.py",
        },
        {
            "slice": "PR-12C",
            "contract": "AI path itinerary and path corridor retrieval",
            "protected_surface": "path_corridor",
            "required_test": "tests/test_pr12c_path_corridors.py",
        },
        {
            "slice": "PR-12D",
            "contract": "Document workspace retrieval",
            "protected_surface": "document_workspace",
            "required_test": "tests/test_pr12d_document_workspace.py",
        },
        {
            "slice": "PR-12E",
            "contract": "Cognitive write intelligence",
            "protected_surface": "cognitive_write_plan",
            "required_test": "tests/test_pr12e_cognitive_write_intelligence.py",
        },
        {
            "slice": "PR-12F",
            "contract": "Human-in-the-loop learning modes",
            "protected_surface": "learning_policy",
            "required_test": "tests/test_pr12f_hitl_learning_modes.py",
        },
        {
            "slice": "PR-12G",
            "contract": "Brain geometry calibration",
            "protected_surface": "brain_geometry_calibration",
            "required_test": "tests/test_pr12g_brain_geometry_calibration.py",
        },
    ]


def _top_trace_regions(trace_insights: dict[str, Any], *, limit: int = 6) -> list[str]:
    bucket_hits = dict(trace_insights.get("bucket_hits") or {})
    ordered = sorted(bucket_hits.items(), key=lambda item: _safe_int(item[1]), reverse=True)
    return _string_list([key for key, _value in ordered], limit=limit)


def _retrieval_failure_refs(*, reasons: list[str], fallback: str) -> list[dict[str, Any]]:
    normalized = _string_list(reasons, limit=8) or [fallback]
    return [_evidence_ref("failure_signatures.retrieval", reason) for reason in normalized]


def build_retrieval_trace_learning_gate(
    *,
    mode: str,
    preview_only: bool,
    graph: dict[str, Any],
    maintenance_baseline_contract: dict[str, Any],
    trace_insights: dict[str, Any],
    retrieval_gap_review: dict[str, Any],
    max_proposals: int = 16,
) -> dict[str, Any]:
    _ = graph
    maintenance_contract = dict(maintenance_baseline_contract.get("maintenance_contract") or {})
    failure_signatures = dict(maintenance_baseline_contract.get("failure_signatures") or {})
    retrieval_signatures = dict(failure_signatures.get("retrieval") or {})
    contract_id = str(maintenance_contract.get("contract_id") or "maintenance_contract::unknown")
    mode_family = str(mode or maintenance_contract.get("mode") or "sleep_evolve")
    route_examples = [
        dict(item)
        for item in list(retrieval_gap_review.get("route_gap_examples") or retrieval_signatures.get("route_gap_examples") or [])
        if isinstance(item, dict)
    ]
    final_examples = [
        dict(item)
        for item in list(retrieval_gap_review.get("final_eval_failure_examples") or retrieval_signatures.get("final_eval_failure_examples") or [])
        if isinstance(item, dict)
    ]
    gap_reasons = dict(retrieval_gap_review.get("gap_reasons") or retrieval_signatures.get("gap_reasons") or {})
    stop_reasons = dict(trace_insights.get("stop_reasons") or retrieval_signatures.get("stop_reasons") or {})
    top_regions = _top_trace_regions(trace_insights, limit=6)
    proposals: list[dict[str, Any]] = []

    for example in route_examples[:6]:
        reasons = _string_list(example.get("reasons"), limit=8) or _string_list(gap_reasons.keys(), limit=8)
        proposals.append(
            _make_proposal(
                proposal_kind="retrieval_gap_review",
                risk_level="high" if any(reason in {"no_evidence", "destination_not_reached"} for reason in reasons) else "medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_region_ids=top_regions,
                evidence_refs=[
                    _evidence_ref(
                        "recent_search_sessions",
                        str(example.get("search_id") or "route_gap"),
                        detail={
                            "query_text": example.get("query_text"),
                            "query_class": example.get("query_class"),
                            "answerability_state": example.get("answerability_state"),
                            "reasons": reasons,
                            "destination_reached_ratio": example.get("destination_reached_ratio"),
                            "evidence_count": example.get("evidence_count"),
                        },
                    ),
                    _evidence_ref("search_events", "bucket_hotspots", detail={"target_region_ids": top_regions, "stop_reasons": stop_reasons}),
                ],
                failure_signature_refs=_retrieval_failure_refs(reasons=reasons, fallback="route_gap"),
                proposed_action="review_route_gap_landing_path_or_corridor_policy_before_compiling_prior",
                preview_delta={
                    "validated_failure": True,
                    "failure_family": "route_gap",
                    "query_text": example.get("query_text"),
                    "target_region_ids": top_regions,
                    "compiled_prior_auto_apply_allowed": False,
                    "suggested_review": "repair_landing_path_corridor_or_document_workspace_policy_without_query_specific_code",
                },
                reason="A structured retrieval trace reached a route gap; it can become a maintenance learning proposal only behind review and regression gates.",
                priority=0.93,
                preview_only=preview_only,
                source_slice="PR-12H-E",
                rollback_boundary="retrieval_trace_learning_review_only_pr12h_e",
            )
        )

    for example in final_examples[:6]:
        reasons = _string_list(example.get("reasons"), limit=8) or _string_list(gap_reasons.keys(), limit=8)
        risk = "high" if any("contradiction" in reason or "adequacy" in reason for reason in reasons) else "medium"
        proposals.append(
            _make_proposal(
                proposal_kind="retrieval_gap_review",
                risk_level=risk,
                mode_family=mode_family,
                contract_id=contract_id,
                target_region_ids=top_regions,
                evidence_refs=[
                    _evidence_ref(
                        "recent_search_sessions",
                        str(example.get("search_id") or "final_eval_failure"),
                        detail={
                            "query_text": example.get("query_text"),
                            "query_class": example.get("query_class"),
                            "answerability_state": example.get("answerability_state"),
                            "reasons": reasons,
                            "answer_adequacy_passed": example.get("answer_adequacy_passed"),
                        },
                    )
                ],
                failure_signature_refs=_retrieval_failure_refs(reasons=reasons, fallback="final_eval_failure"),
                proposed_action="review_answer_context_mismatch_stop_policy_or_missing_evidence_before_learning",
                preview_delta={
                    "validated_failure": True,
                    "failure_family": "final_eval_failure",
                    "query_text": example.get("query_text"),
                    "compiled_prior_auto_apply_allowed": False,
                    "suggested_review": "repair_context_package_or_stop_judge_contract_before_answer_layer_changes",
                },
                reason="The retrieval trace produced an answer/context quality failure; learning must target context and stop contracts, not answer-specific patches.",
                priority=0.91 if risk == "high" else 0.86,
                preview_only=preview_only,
                source_slice="PR-12H-E",
                rollback_boundary="retrieval_trace_learning_review_only_pr12h_e",
            )
        )

    if stop_reasons and not proposals:
        proposals.append(
            _make_proposal(
                proposal_kind="retrieval_gap_review",
                risk_level="medium",
                mode_family=mode_family,
                contract_id=contract_id,
                target_region_ids=top_regions,
                evidence_refs=[
                    _evidence_ref("search_events", "stop_reason_histogram", detail={"stop_reasons": stop_reasons, "target_region_ids": top_regions})
                ],
                failure_signature_refs=_retrieval_failure_refs(reasons=list(stop_reasons.keys()), fallback="stop_reason"),
                proposed_action="review_stop_reason_histogram_before_adjusting_route_or_judge_policy",
                preview_delta={
                    "validated_failure": bool(stop_reasons),
                    "failure_family": "stop_reason_histogram",
                    "compiled_prior_auto_apply_allowed": False,
                    "suggested_review": "inspect_repeated_stop_reasons_before_learning_a_prior",
                },
                reason="Recent search events exposed stop reasons but no complete example packet; treat as review evidence only.",
                priority=0.78,
                preview_only=preview_only,
                source_slice="PR-12H-E",
                rollback_boundary="retrieval_trace_learning_review_only_pr12h_e",
            )
        )

    proposals = _dedupe_sort_proposals(proposals, max_proposals=max_proposals)
    regression_contracts = _pr12a_to_g_regression_contracts()
    proposal_preview_safe = all(dict(proposal.get("preview_delta") or {}).get("mutates_graph") is False for proposal in proposals)
    contracts_complete = len(regression_contracts) == 7 and {item["slice"] for item in regression_contracts} == {
        "PR-12A",
        "PR-12B",
        "PR-12C",
        "PR-12D",
        "PR-12E",
        "PR-12F",
        "PR-12G",
    }
    active = bool(
        proposals
        or _safe_int(retrieval_gap_review.get("gap_session_count") or retrieval_signatures.get("gap_session_count") or 0) > 0
        or bool(retrieval_signatures.get("review_required"))
    )
    gate = {
        "schema_version": RETRIEVAL_TRACE_LEARNING_GATE_SCHEMA_VERSION,
        "slice": "PR-12H-E",
        "active": active,
        "generated_from_contract_id": contract_id,
        "mode": mode_family,
        "preview_only": bool(preview_only),
        "proposal_count": len(proposals),
        "converted_failure_count": len(proposals),
        "validated_trace_evidence": {
            "session_count": _safe_int(retrieval_gap_review.get("session_count") or retrieval_signatures.get("session_count") or 0),
            "gap_session_count": _safe_int(retrieval_gap_review.get("gap_session_count") or retrieval_signatures.get("gap_session_count") or 0),
            "route_gap_example_count": len(route_examples),
            "final_eval_failure_example_count": len(final_examples),
            "stop_reason_count": len(stop_reasons),
            "gap_reasons": gap_reasons,
            "target_region_ids": top_regions,
            "evidence_source": "recent_search_sessions.result_json",
        },
        "mutation_boundary": {
            "proposal_engine_mutates_graph": False,
            "proposals_mutate_graph": False,
            "compiled_priors_auto_apply": False,
            "requires_pr12h_d_apply_policy": True,
            "requires_human_review": bool(proposals),
        },
        "regression_contracts": regression_contracts,
        "required_regression_tests": [item["required_test"] for item in regression_contracts],
        "required_regression_command": "python -m pytest tests/test_pr12_semantic_contract.py tests/test_pr12b_context_package.py tests/test_pr12c_path_corridors.py tests/test_pr12d_document_workspace.py tests/test_pr12e_cognitive_write_intelligence.py tests/test_pr12f_hitl_learning_modes.py tests/test_pr12g_brain_geometry_calibration.py -q",
        "contract_gate_passed": contracts_complete and proposal_preview_safe,
        "external_regression_required_for_slice_closure": True,
        "external_regression_status": "must_pass_before_product_ready_claim",
        "learning_policy": {
            "query_specific_code_allowed": False,
            "answer_layer_patch_allowed": False,
            "reviewed_prior_or_contract_repair_only": True,
            "mcp_context_package_is_primary_product": True,
        },
    }
    return {
        "retrieval_trace_learning_proposals": proposals,
        "retrieval_trace_learning_gate": gate,
    }


def combine_maintenance_proposal_surfaces(
    *,
    mode: str,
    preview_only: bool,
    maintenance_baseline_contract: dict[str, Any],
    sleep_consolidation: dict[str, Any],
    evolve_structural: dict[str, Any],
    retrieval_trace_learning: dict[str, Any] | None = None,
    max_proposals: int = 48,
) -> dict[str, Any]:
    maintenance_contract = dict(maintenance_baseline_contract.get("maintenance_contract") or {})
    contract_id = str(maintenance_contract.get("contract_id") or "maintenance_contract::unknown")
    sleep_proposals = [
        dict(item)
        for item in list(sleep_consolidation.get("sleep_consolidation_proposals") or [])
        if isinstance(item, dict)
    ]
    evolve_proposals = [
        dict(item)
        for item in list(evolve_structural.get("evolve_structural_proposals") or [])
        if isinstance(item, dict)
    ]
    retrieval_trace_learning = dict(retrieval_trace_learning or {})
    retrieval_proposals = [
        dict(item)
        for item in list(retrieval_trace_learning.get("retrieval_trace_learning_proposals") or [])
        if isinstance(item, dict)
    ]
    proposals = _dedupe_sort_proposals([*sleep_proposals, *evolve_proposals, *retrieval_proposals], max_proposals=max_proposals)
    child_profiles = {
        "sleep_consolidation_profile": dict(sleep_consolidation.get("sleep_consolidation_profile") or {}),
        "evolve_structural_profile": dict(evolve_structural.get("evolve_structural_profile") or {}),
        "retrieval_trace_learning_gate": dict(retrieval_trace_learning.get("retrieval_trace_learning_gate") or {}),
    }
    summary_seed = {
        "contract_id": contract_id,
        "proposal_ids": [proposal.get("proposal_id") for proposal in proposals],
        "schema_version": MAINTENANCE_PROPOSAL_SUMMARY_SCHEMA_VERSION,
    }
    summary = {
        "schema_version": MAINTENANCE_PROPOSAL_SUMMARY_SCHEMA_VERSION,
        "summary_id": f"maintenance_proposals::{_json_hash(summary_seed)}",
        "slice_span": ["PR-12H-B", "PR-12H-C", "PR-12H-E"],
        "generated_from_contract_id": contract_id,
        "mode": str(mode or maintenance_contract.get("mode") or "sleep_evolve"),
        "preview_only": bool(preview_only),
        "proposal_count": len(proposals),
        "proposal_budget": int(max_proposals),
        "kind_histogram": _histogram([proposal.get("proposal_kind") for proposal in proposals]),
        "risk_histogram": _histogram([proposal.get("risk_level") for proposal in proposals]),
        "child_profiles": child_profiles,
        "mutation_boundary": {
            "proposal_summary_mutates_graph": False,
            "proposals_mutate_graph": False,
            "auto_apply_allowed": False,
            "requires_pr12h_d_apply_policy": True,
            "requires_pr12h_e_regression_gate": True,
        },
        "review_required": bool(proposals),
    }
    return {
        "maintenance_proposals": proposals,
        "maintenance_proposal_summary": summary,
    }


def _graph_hash(graph: dict[str, Any]) -> str:
    compact_nodes = []
    for node in list(graph.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        compact_nodes.append(
            {
                "id": node.get("id"),
                "memory_type": node.get("memory_type"),
                "summary": node.get("summary"),
                "raw_text": node.get("raw_text"),
                "final_position": node.get("final_position"),
                "lifecycle_status": node.get("lifecycle_status"),
                "source_trust": node.get("source_trust"),
                "claim_status": node.get("claim_status"),
                "is_document_anchor": node.get("is_document_anchor"),
                "highways": node.get("highways"),
                "links": node.get("links"),
            }
        )
    compact_edges = [
        dict(edge)
        for edge in list(graph.get("edges") or [])
        if isinstance(edge, dict)
    ]
    return _json_hash({"nodes": compact_nodes, "edges": compact_edges})


def _is_document_anchor_node(node: dict[str, Any]) -> bool:
    provenance = dict(node.get("provenance") or {})
    return (
        bool(node.get("is_document_anchor"))
        or str(node.get("memory_type") or "") == "document_anchor"
        or str(node.get("node_kind") or "") == "document_anchor"
        or str(provenance.get("source_type") or "") == "document"
    )


def _protected_node_reasons(node: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    memory_type = str(node.get("memory_type") or "").strip()
    claim_status = str(node.get("claim_status") or "").strip()
    source_trust = str(node.get("source_trust") or "").strip()
    if _is_document_anchor_node(node):
        reasons.append("document_anchor")
    if memory_type in {"identity", "identity_style", "value"}:
        reasons.append("identity_or_core_profile")
    if memory_type == "relational":
        reasons.append("relationship")
    if claim_status == "fact" and source_trust and source_trust not in {"system_metadata", "synthetic_test"}:
        reasons.append("source_grounded_fact")
    if bool(node.get("requires_human_review")) or list(node.get("cognitive_review_reasons") or []):
        reasons.append("pending_human_review")
    return _string_list(reasons, limit=8)


def _node_fingerprint(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(node.get("id") or ""),
        "memory_type": node.get("memory_type"),
        "summary": node.get("summary"),
        "raw_text": node.get("raw_text"),
        "lifecycle_status": node.get("lifecycle_status"),
        "source_trust": node.get("source_trust"),
        "claim_status": node.get("claim_status"),
        "is_document_anchor": bool(node.get("is_document_anchor")),
        "final_position": node.get("final_position"),
        "highway_count": len(list(node.get("highways") or [])),
        "link_count": len(list(node.get("links") or [])),
    }


def _changed_fields(before_node: dict[str, Any], after_node: dict[str, Any]) -> list[str]:
    fields = [
        "memory_type",
        "summary",
        "raw_text",
        "lifecycle_status",
        "source_trust",
        "claim_status",
        "is_document_anchor",
        "final_position",
    ]
    changed = [field for field in fields if before_node.get(field) != after_node.get(field)]
    if list(before_node.get("highways") or []) != list(after_node.get("highways") or []):
        changed.append("highways")
    if list(before_node.get("links") or []) != list(after_node.get("links") or []):
        changed.append("links")
    return changed


def _graph_delta(before_graph: dict[str, Any], candidate_graph: dict[str, Any]) -> dict[str, Any]:
    before_nodes = {
        _node_id(node): dict(node)
        for node in list(before_graph.get("nodes") or [])
        if isinstance(node, dict) and _node_id(node)
    }
    after_nodes = {
        _node_id(node): dict(node)
        for node in list(candidate_graph.get("nodes") or [])
        if isinstance(node, dict) and _node_id(node)
    }
    before_ids = set(before_nodes)
    after_ids = set(after_nodes)
    deleted_ids = sorted(before_ids - after_ids)
    created_ids = sorted(after_ids - before_ids)
    changed_rows: list[dict[str, Any]] = []
    for node_id in sorted(before_ids & after_ids):
        before_node = before_nodes[node_id]
        after_node = after_nodes[node_id]
        fields = _changed_fields(before_node, after_node)
        if fields:
            changed_rows.append(
                {
                    "node_id": node_id,
                    "changed_fields": fields,
                    "protected_reasons": _protected_node_reasons(before_node),
                    "before": _node_fingerprint(before_node),
                    "after": _node_fingerprint(after_node),
                }
            )
    deleted_rows = [
        {
            "node_id": node_id,
            "protected_reasons": _protected_node_reasons(before_nodes[node_id]),
            "before": _node_fingerprint(before_nodes[node_id]),
        }
        for node_id in deleted_ids
    ]
    created_rows = [
        {
            "node_id": node_id,
            "protected_reasons": _protected_node_reasons(after_nodes[node_id]),
            "after": _node_fingerprint(after_nodes[node_id]),
        }
        for node_id in created_ids
    ]
    return {
        "deleted_ids": deleted_ids,
        "created_ids": created_ids,
        "changed_rows": changed_rows,
        "deleted_rows": deleted_rows,
        "created_rows": created_rows,
        "before_node_count": len(before_nodes),
        "candidate_node_count": len(after_nodes),
    }


def build_apply_policy_rollback_guard(
    *,
    mode: str,
    preview_only: bool,
    before_graph: dict[str, Any],
    candidate_graph: dict[str, Any],
    maintenance_baseline_contract: dict[str, Any],
    maintenance_proposals: list[dict[str, Any]],
    document_anchor_guard: dict[str, Any],
    quality_before: dict[str, Any],
    candidate_quality_after: dict[str, Any],
    candidate_quality_delta: dict[str, Any],
    overall_quality_delta_score: float,
    reviewed_candidate_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    maintenance_contract = dict(maintenance_baseline_contract.get("maintenance_contract") or {})
    contract_id = str(maintenance_contract.get("contract_id") or "maintenance_contract::unknown")
    proposals = [
        dict(item)
        for item in list(maintenance_proposals or [])
        if isinstance(item, dict)
    ]
    reviewed_actions = [
        dict(item)
        for item in list(reviewed_candidate_actions or [])
        if isinstance(item, dict)
    ]
    reviewed_field_allowlist: dict[str, set[str]] = {}
    for action in reviewed_actions:
        node_id = str(action.get("node_id") or "").strip()
        if not node_id:
            continue
        reviewed_field_allowlist.setdefault(node_id, set()).update(_string_list(action.get("allowed_fields"), limit=12))
    delta = _graph_delta(before_graph, candidate_graph)
    destructive_blocks: list[dict[str, Any]] = []
    for row in list(delta.get("deleted_rows") or []):
        reasons = _string_list(row.get("protected_reasons"), limit=8)
        if reasons:
            destructive_blocks.append(
                {
                    "node_id": row.get("node_id"),
                    "operation": "delete",
                    "protected_reasons": reasons,
                }
            )
    for row in list(delta.get("changed_rows") or []):
        node_id = str(row.get("node_id") or "").strip()
        reasons = _string_list(row.get("protected_reasons"), limit=8)
        fields = set(_string_list(row.get("changed_fields"), limit=16))
        destructive_fields = fields & {"memory_type", "summary", "raw_text", "lifecycle_status", "source_trust", "claim_status", "is_document_anchor"}
        strict_reasons = set(reasons) & {"document_anchor", "relationship", "source_grounded_fact", "pending_human_review"}
        if reasons and not strict_reasons and node_id:
            destructive_fields -= reviewed_field_allowlist.get(node_id, set())
            before_fingerprint = dict(row.get("before") or {})
            after_fingerprint = dict(row.get("after") or {})
            for metadata_field in ("source_trust", "claim_status"):
                before_value = str(before_fingerprint.get(metadata_field) or "").strip()
                after_value = str(after_fingerprint.get(metadata_field) or "").strip()
                if not before_value and after_value:
                    destructive_fields.discard(metadata_field)
        if reasons and destructive_fields:
            destructive_blocks.append(
                {
                    "node_id": node_id or row.get("node_id"),
                    "operation": "protected_field_change",
                    "protected_reasons": reasons,
                    "changed_fields": sorted(destructive_fields),
                }
            )
    missing_document_anchors = _string_list(document_anchor_guard.get("missing_document_anchor_ids"), limit=24)
    if missing_document_anchors:
        for node_id in missing_document_anchors:
            destructive_blocks.append(
                {
                    "node_id": node_id,
                    "operation": "document_anchor_missing",
                    "protected_reasons": ["document_anchor"],
                }
            )

    risk_histogram = _histogram([proposal.get("risk_level") for proposal in proposals])
    reviewed_low_risk = [
        proposal
        for proposal in proposals
        if str(proposal.get("risk_level") or "") == "low"
        and not bool(proposal.get("review_only", True))
        and bool(dict(proposal.get("apply_policy") or {}).get("human_review_completed"))
    ]
    blocked_reasons: list[str] = []
    if preview_only:
        blocked_reasons.append("preview_only")
    if destructive_blocks:
        blocked_reasons.append("protected_memory_mutation_blocked")
    applied = bool(not preview_only and not destructive_blocks)
    before_hash = _graph_hash(before_graph)
    candidate_hash = _graph_hash(candidate_graph)
    applied_hash = candidate_hash if applied else before_hash
    rollback_snapshot = {
        "schema_version": "agvm.pr12h.rollback_snapshot.v1",
        "snapshot_id": f"rollback::{_json_hash({'contract_id': contract_id, 'before_graph_hash': before_hash, 'candidate_graph_hash': candidate_hash})}",
        "contract_id": contract_id,
        "created_at": utc_timestamp(),
        "before_graph_hash": before_hash,
        "candidate_graph_hash": candidate_hash,
        "applied_graph_hash": applied_hash,
        "before_node_count": int(delta.get("before_node_count") or 0),
        "candidate_node_count": int(delta.get("candidate_node_count") or 0),
        "inverse_delta": {
            "deleted_node_snapshots": [dict(row.get("before") or {}) for row in list(delta.get("deleted_rows") or [])[:20]],
            "created_node_ids": _string_list(delta.get("created_ids"), limit=40),
            "changed_node_snapshots": [
                {
                    "node_id": row.get("node_id"),
                    "changed_fields": row.get("changed_fields"),
                    "before": row.get("before"),
                }
                for row in list(delta.get("changed_rows") or [])[:40]
            ],
        },
        "raw_documents_preserved": not missing_document_anchors,
    }
    no_corruption_guards = {
        "schema_version": "agvm.pr12h.no_corruption_guards.v1",
        "document_anchor_guard": dict(document_anchor_guard or {}),
        "identity_relationship_guard": {
            "protected_node_mutation_count": len(
                [
                    item
                    for item in destructive_blocks
                    if any(reason in {"identity_or_core_profile", "relationship"} for reason in _string_list(item.get("protected_reasons"), limit=8))
                ]
            ),
            "passed": not any(
                any(reason in {"identity_or_core_profile", "relationship"} for reason in _string_list(item.get("protected_reasons"), limit=8))
                for item in destructive_blocks
            ),
        },
        "source_grounded_fact_guard": {
            "protected_fact_mutation_count": len(
                [
                    item
                    for item in destructive_blocks
                    if "source_grounded_fact" in _string_list(item.get("protected_reasons"), limit=8)
                ]
            ),
            "passed": not any("source_grounded_fact" in _string_list(item.get("protected_reasons"), limit=8) for item in destructive_blocks),
        },
        "protected_mutation_blocks": destructive_blocks[:30],
        "passed": not destructive_blocks and not missing_document_anchors,
    }
    return {
        "schema_version": APPLY_POLICY_GUARD_SCHEMA_VERSION,
        "slice": "PR-12H-D",
        "mode": str(mode or maintenance_contract.get("mode") or "sleep_evolve"),
        "preview_only": bool(preview_only),
        "requested_apply": not bool(preview_only),
        "applied": applied,
        "guard_passed": not destructive_blocks and not missing_document_anchors,
        "blocked_reasons": _string_list(blocked_reasons, limit=12),
        "proposal_apply_policy": {
            "proposal_count": len(proposals),
            "risk_histogram": risk_histogram,
            "reviewed_low_risk_proposal_count": len(reviewed_low_risk),
            "reviewed_candidate_action_count": len(reviewed_actions),
            "proposal_graph_mutation_count": 0,
            "auto_apply_allowed": False,
            "reviewed_low_risk_only": True,
            "note": "PR-12H-D records proposal policy and guards direct maintenance apply; proposal-specific transforms require explicit reviewed proposal ids in a later UI/MCP flow.",
        },
        "before_after_audit": {
            "before_graph_hash": before_hash,
            "candidate_graph_hash": candidate_hash,
            "applied_graph_hash": applied_hash,
            "before_node_count": int(delta.get("before_node_count") or 0),
            "candidate_node_count": int(delta.get("candidate_node_count") or 0),
            "created_node_count": len(list(delta.get("created_ids") or [])),
            "deleted_node_count": len(list(delta.get("deleted_ids") or [])),
            "changed_node_count": len(list(delta.get("changed_rows") or [])),
            "quality_before": dict(quality_before or {}),
            "candidate_quality_after": dict(candidate_quality_after or {}),
            "candidate_quality_delta": dict(candidate_quality_delta or {}),
            "candidate_overall_quality_delta_score": round(float(overall_quality_delta_score or 0.0), 6),
        },
        "rollback_snapshot": rollback_snapshot,
        "no_corruption_guards": no_corruption_guards,
        "effective_graph": "candidate_graph" if applied else "before_graph",
    }


def _proposal_node_ids(proposal: dict[str, Any]) -> list[str]:
    preview_delta = dict(proposal.get("preview_delta") or {})
    elastic_payload = dict(preview_delta.get("elastic_topology_proposal") or {})
    displacement_ids = [
        str(item.get("node_id") or "").strip()
        for item in list(elastic_payload.get("node_displacement_preview") or [])
        if isinstance(item, dict)
    ]
    return _string_list(
        list(proposal.get("target_node_ids") or [])
        + list(elastic_payload.get("target_cluster_node_ids") or [])
        + list(elastic_payload.get("affected_neighbor_node_ids") or [])
        + displacement_ids,
        limit=80,
    )


def _proposal_has_provenance(proposal: dict[str, Any]) -> bool:
    evidence = [dict(item) for item in list(proposal.get("evidence_refs") or []) if isinstance(item, dict)]
    if evidence:
        return True
    preview_delta = dict(proposal.get("preview_delta") or {})
    elastic_payload = dict(preview_delta.get("elastic_topology_proposal") or {})
    return bool([dict(item) for item in list(elastic_payload.get("evidence_refs") or []) if isinstance(item, dict)])


def _proposal_is_matrix_candidate(proposal: dict[str, Any]) -> bool:
    kind = str(proposal.get("proposal_kind") or "").strip().lower()
    action = str(proposal.get("proposed_action") or "").strip().lower()
    preview_delta = dict(proposal.get("preview_delta") or {})
    elastic_payload = dict(preview_delta.get("elastic_topology_proposal") or {})
    return bool(
        "matrix" in kind
        or "matrix_calibration" in action
        or bool(elastic_payload.get("matrix_calibration_candidate"))
        or str(preview_delta.get("topology_change_class") or "").strip().lower() == "matrix_calibration_candidate"
    )


def _proposal_phase(proposal: dict[str, Any]) -> str:
    if _proposal_is_matrix_candidate(proposal):
        return "matrix"
    kind = str(proposal.get("proposal_kind") or "").strip().lower()
    mode_family = str(proposal.get("mode_family") or "").strip().lower()
    if kind in {"duplicate_review", "merge_review", "source_hygiene_review", "confidence_review", "warm_depromotion_review", "cold_promotion_review"}:
        return "sleep"
    if "sleep" in mode_family and "evolve" not in mode_family:
        return "sleep"
    if kind in {
        "geometry_reposition_review",
        "highway_review",
        "split_review",
        "document_project_coupling_review",
        "retrieval_gap_review",
        "elastic_topology_deformation_review",
    }:
        return "evolve"
    if "evolve" in mode_family:
        return "evolve"
    return "review"


def _proposal_is_sleep_merge_or_archive(proposal: dict[str, Any]) -> bool:
    kind = str(proposal.get("proposal_kind") or "").strip().lower()
    action = str(proposal.get("proposed_action") or "").strip().lower()
    return bool(kind in {"duplicate_review", "merge_review"} or any(token in action for token in ("archive", "merge", "consolidat")))


def _proposal_is_evolve_movement(proposal: dict[str, Any]) -> bool:
    kind = str(proposal.get("proposal_kind") or "").strip().lower()
    action = str(proposal.get("proposed_action") or "").strip().lower()
    preview_delta = dict(proposal.get("preview_delta") or {})
    elastic_payload = dict(preview_delta.get("elastic_topology_proposal") or {})
    return bool(
        kind in {"geometry_reposition_review", "document_project_coupling_review", "elastic_topology_deformation_review"}
        or "move" in action
        or "reposition" in action
        or list(elastic_payload.get("node_displacement_preview") or [])
    )


def _maintenance_selected_proposals(proposals: list[dict[str, Any]], selected_proposal_ids: list[str] | None) -> list[dict[str, Any]]:
    selected = set(_string_list(selected_proposal_ids or [], limit=300))
    if not selected:
        return list(proposals)
    return [proposal for proposal in proposals if str(proposal.get("proposal_id") or "").strip() in selected]


def _maintenance_revision_keys(
    *,
    brain_id: str | None,
    mode: str,
    proposals: list[dict[str, Any]],
    selected_proposal_ids: list[str],
    metamemory_snapshot_payload: dict[str, Any],
    apply_policy_guard: dict[str, Any],
    before_after_audit: dict[str, Any],
    calibration_delta: dict[str, Any],
) -> dict[str, Any]:
    spatial_brief = dict(metamemory_snapshot_payload.get("spatial_brief") or {})
    before_hash = str(before_after_audit.get("before_graph_hash") or apply_policy_guard.get("before_graph_hash") or "").strip()
    candidate_hash = str(before_after_audit.get("candidate_graph_hash") or apply_policy_guard.get("candidate_graph_hash") or "").strip()
    applied_hash = str(before_after_audit.get("applied_graph_hash") or apply_policy_guard.get("applied_graph_hash") or "").strip()
    proposal_seed = [
        {
            "proposal_id": proposal.get("proposal_id"),
            "proposal_kind": proposal.get("proposal_kind"),
            "phase": _proposal_phase(proposal),
            "target_node_ids": _proposal_node_ids(proposal),
        }
        for proposal in proposals
    ]
    matrix_seed = {
        "mode": mode,
        "calibration_delta": calibration_delta,
        "matrix_candidate_ids": [proposal.get("proposal_id") for proposal in proposals if _proposal_is_matrix_candidate(proposal)],
    }
    topology_seed = {
        "mode": mode,
        "proposal_ids": [proposal.get("proposal_id") for proposal in proposals],
        "selected_proposal_ids": selected_proposal_ids,
        "before_graph_hash": before_hash,
        "candidate_graph_hash": candidate_hash,
    }
    metamemory_revision = (
        str(metamemory_snapshot_payload.get("snapshot_id") or "").strip()
        or str(spatial_brief.get("hash") or spatial_brief.get("revision") or "").strip()
        or f"metamemory::{_json_hash({'mode': mode, 'empty': True})}"
    )
    return {
        "schema_version": "agvm.maintenance_revision_keys.v1",
        "brain_revision_key": f"brain::{brain_id or 'runtime_scope_unattached'}::{before_hash or 'unknown'}",
        "graph_before_revision": before_hash or None,
        "graph_candidate_revision": candidate_hash or None,
        "graph_applied_revision": applied_hash or None,
        "topology_revision_key": f"topology::{_json_hash(topology_seed)}",
        "matrix_revision_key": f"matrix::{_json_hash(matrix_seed)}",
        "metamemory_revision_key": metamemory_revision,
        "proposal_set_revision_key": f"proposals::{_json_hash({'items': proposal_seed})}",
        "preview_signature_key": f"maintenance_preview::{_json_hash({'mode': mode, 'topology': topology_seed, 'matrix': matrix_seed, 'metamemory': metamemory_revision})}",
    }


def _maintenance_cache_invalidation_plan(
    *,
    revision_keys: dict[str, Any],
    applied: bool,
    selected_proposal_ids: list[str],
    classes_present: dict[str, bool],
) -> dict[str, Any]:
    triggers = []
    if bool(classes_present.get("evolve")):
        triggers.append("topology_change_candidate")
    if bool(classes_present.get("matrix")):
        triggers.append("matrix_calibration_candidate")
    if bool(classes_present.get("sleep")):
        triggers.append("sleep_consolidation_candidate")
    if applied:
        triggers.append("approved_apply_executed")
    invalidate_on_apply = [
        {
            "cache": "ai_spatial_cache",
            "revision_key": revision_keys.get("topology_revision_key"),
            "reason": "landing/path coordinates depend on topology and matrix revisions",
        },
        {
            "cache": "metamemory_spatial_brief_cache",
            "revision_key": revision_keys.get("metamemory_revision_key"),
            "reason": "AI landing planner must see the refreshed spatial/matrix brief after maintenance",
        },
        {
            "cache": "atlas_topology_summary_cache",
            "revision_key": revision_keys.get("topology_revision_key"),
            "reason": "UI/API topology summaries must not reuse stale node-region projections",
        },
        {
            "cache": "mcp_first_payload_cache",
            "revision_key": revision_keys.get("preview_signature_key"),
            "reason": "MCP retrieve payloads must be keyed by brain, matrix, topology and metamemory revisions",
        },
        {
            "cache": "benchmark_artifacts",
            "revision_key": revision_keys.get("proposal_set_revision_key"),
            "reason": "certification artifacts are invalid after topology/matrix/metamemory changes",
        },
    ]
    return {
        "schema_version": "agvm.maintenance_cache_invalidation_plan.v1",
        "selected_proposal_ids": selected_proposal_ids,
        "triggers": triggers,
        "invalidate_on_apply": invalidate_on_apply,
        "preview_invalidates_runtime_cache": False,
        "apply_invalidates_runtime_cache": True,
        "revision_keyed": True,
    }


def _maintenance_post_apply_validation_plan(
    *,
    applied: bool,
    classes_present: dict[str, bool],
    selected_proposal_ids: list[str],
) -> dict[str, Any]:
    checks = [
        {"check": "api_health", "endpoint": "/health", "required": True},
        {"check": "brain_health", "endpoint": "/mcp/brain-health", "required": True},
        {
            "check": "focused_retrieve_identity_probe",
            "tool": "retrieve_context",
            "required": True,
            "purpose": "verify identity nucleus survived maintenance",
        },
        {
            "check": "focused_retrieve_path_probe",
            "tool": "retrieve_path_corridor",
            "required": bool(classes_present.get("evolve") or classes_present.get("matrix")),
            "purpose": "verify path/landing truth after topology or matrix maintenance",
        },
        {
            "check": "focused_document_ref_probe",
            "tool": "retrieve_document",
            "required": bool(classes_present.get("sleep")),
            "purpose": "verify document anchors remain readable after consolidation",
        },
    ]
    status = "pending" if applied else "not_started_preview_only"
    return {
        "schema_version": "agvm.maintenance_post_apply_validation_plan.v1",
        "status": status,
        "required_before_healthy_for_benchmark": True,
        "selected_proposal_ids": selected_proposal_ids,
        "checks": checks,
        "healthy_for_benchmark_requires": [
            "api_health_ok",
            "brain_health_serious_benchmark_ready",
            "focused_retrieve_probes_green",
            "metamemory_spatial_brief_refreshed_if_topology_or_matrix_changed",
        ],
    }


def _maintenance_conflict_resolution(
    *,
    proposals: list[dict[str, Any]],
    selected_proposal_ids: list[str],
    archived_node_ids: list[str],
    superseded_node_ids: list[str],
    confirm_apply: bool,
    apply_requested: bool,
    blocked_reason: str | None,
) -> dict[str, Any]:
    selected_proposals = _maintenance_selected_proposals(proposals, selected_proposal_ids)
    sleep_candidates = [proposal for proposal in selected_proposals if _proposal_phase(proposal) == "sleep"]
    evolve_candidates = [proposal for proposal in selected_proposals if _proposal_phase(proposal) == "evolve"]
    matrix_candidates = [proposal for proposal in selected_proposals if _proposal_phase(proposal) == "matrix"]
    local_elastic_candidates = [
        proposal
        for proposal in evolve_candidates
        if str(proposal.get("proposal_kind") or "") == "elastic_topology_deformation_review" and not _proposal_is_matrix_candidate(proposal)
    ]
    conflicts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    sleep_mutating_ids: set[str] = set(_string_list(archived_node_ids + superseded_node_ids, limit=300))
    for proposal in sleep_candidates:
        if _proposal_is_sleep_merge_or_archive(proposal):
            sleep_mutating_ids.update(_proposal_node_ids(proposal))
    evolve_moving_ids: set[str] = set()
    for proposal in evolve_candidates:
        if _proposal_is_evolve_movement(proposal):
            evolve_moving_ids.update(_proposal_node_ids(proposal))
    overlap = sorted(sleep_mutating_ids & evolve_moving_ids)
    if overlap:
        conflicts.append(
            {
                "code": "sleep_evolve_node_overlap",
                "severity": "apply_blocker",
                "node_ids": overlap[:80],
                "policy": "prefer_conservative_sleep_or_require_manual_selection",
            }
        )
        decisions.append(
            {
                "code": "conservative_sleep_preferred",
                "node_ids": overlap[:80],
                "decision": "do_not_apply_sleep_and_evolve_moves_for_the_same_nodes_in_one_transaction",
            }
        )

    protected_motion_rows: list[dict[str, Any]] = []
    protected_pinned_rows: list[dict[str, Any]] = []
    for proposal in evolve_candidates:
        preview_delta = dict(proposal.get("preview_delta") or {})
        elastic_payload = dict(preview_delta.get("elastic_topology_proposal") or {})
        for item in list(elastic_payload.get("node_displacement_preview") or []):
            if not isinstance(item, dict):
                continue
            anchor_class = str(item.get("anchor_class") or "movable_memory").strip()
            node_id = str(item.get("node_id") or "").strip()
            displacement = _safe_float(item.get("max_displacement") or 0.0)
            pinned = bool(item.get("pinned"))
            if anchor_class in {"movable_memory", "ordinary_memory", ""}:
                continue
            if pinned or displacement <= 0.00001:
                protected_pinned_rows.append({"proposal_id": proposal.get("proposal_id"), "node_id": node_id, "anchor_class": anchor_class})
                continue
            protected_motion_rows.append(
                {
                    "proposal_id": proposal.get("proposal_id"),
                    "node_id": node_id,
                    "anchor_class": anchor_class,
                    "max_displacement": round(displacement, 6),
                }
            )
    if protected_motion_rows:
        conflicts.append(
            {
                "code": "protected_anchor_movement_requires_downgrade",
                "severity": "apply_blocker",
                "rows": protected_motion_rows[:80],
                "policy": "downgrade_to_bridge_or_highway_unless_explicit_coordinate_safe_policy_allows_it",
            }
        )
    if protected_pinned_rows:
        decisions.append(
            {
                "code": "protected_anchor_pinned",
                "decision": "protected anchors remain visible in preview but receive no coordinate mutation",
                "rows": protected_pinned_rows[:80],
            }
        )

    if matrix_candidates and local_elastic_candidates:
        conflicts.append(
            {
                "code": "matrix_supersedes_local_elastic_moves",
                "severity": "defer_local_moves",
                "matrix_proposal_ids": _string_list([proposal.get("proposal_id") for proposal in matrix_candidates], limit=80),
                "deferred_local_proposal_ids": _string_list([proposal.get("proposal_id") for proposal in local_elastic_candidates], limit=80),
                "policy": "run_matrix_preview_or_apply_matrix_candidate_before_local_elastic_coordinate_changes",
            }
        )
        decisions.append(
            {
                "code": "defer_local_elastic_until_matrix_decision",
                "decision": "matrix-level drift owns the topology revision before local dragging can be applied",
            }
        )

    missing_provenance_ids = [
        str(proposal.get("proposal_id") or "")
        for proposal in selected_proposals
        if str(proposal.get("proposal_id") or "").strip() and not _proposal_has_provenance(proposal)
    ]
    if missing_provenance_ids:
        conflicts.append(
            {
                "code": "proposal_missing_provenance",
                "severity": "apply_blocker",
                "proposal_ids": missing_provenance_ids[:120],
                "policy": "every applied proposal must cite evidence_refs or embedded elastic evidence",
            }
        )

    conflict_codes = _string_list([conflict.get("code") for conflict in conflicts], limit=80)
    apply_blockers = [
        dict(conflict)
        for conflict in conflicts
        if str(conflict.get("severity") or "") in {"apply_blocker", "blocked"}
    ]
    apply_blocked = bool(blocked_reason or missing_provenance_ids or apply_blockers)
    if apply_requested and not confirm_apply:
        apply_blocked = True
    return {
        "schema_version": "agvm.maintenance_conflict_resolution.v1",
        "selected_scope": "selected_proposals" if selected_proposal_ids else "all_available_preview_proposals",
        "selected_proposal_ids": selected_proposal_ids,
        "sleep_candidate_count": len(sleep_candidates),
        "evolve_candidate_count": len(evolve_candidates),
        "matrix_candidate_count": len(matrix_candidates),
        "conflict_count": len(conflicts),
        "conflict_codes": conflict_codes,
        "conflicts": conflicts,
        "decisions": decisions,
        "apply_blocked": apply_blocked,
        "blocked_reasons": _string_list([blocked_reason] + conflict_codes, limit=100),
        "missing_provenance_proposal_ids": missing_provenance_ids[:120],
        "policy": "resolve_cross_phase_conflicts_before_any_sleep_evolve_matrix_apply",
    }


def build_maintenance_transaction(
    *,
    mode: str,
    tool_name: str | None = None,
    preview_only: bool = True,
    brain_id: str | None = None,
    maintenance_proposals: list[dict[str, Any]] | None = None,
    selected_proposal_ids: list[str] | None = None,
    selected_missing_proposal_ids: list[str] | None = None,
    confirm_apply: bool = False,
    apply_requested: bool | None = None,
    apply_policy_guard: dict[str, Any] | None = None,
    rollback_snapshot: dict[str, Any] | None = None,
    before_after_audit: dict[str, Any] | None = None,
    metamemory_snapshot_payload: dict[str, Any] | None = None,
    maintenance_preview_plan: dict[str, Any] | None = None,
    sleep_consolidation_profile: dict[str, Any] | None = None,
    evolve_structural_profile: dict[str, Any] | None = None,
    retrieval_trace_learning_gate: dict[str, Any] | None = None,
    calibration_delta: dict[str, Any] | None = None,
    archived_node_ids: list[str] | None = None,
    superseded_node_ids: list[str] | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    proposals = [dict(item) for item in list(maintenance_proposals or []) if isinstance(item, dict)]
    selected_ids = _string_list(selected_proposal_ids or [], limit=300)
    selected_missing = _string_list(selected_missing_proposal_ids or [], limit=300)
    guard = dict(apply_policy_guard or {})
    rollback = dict(rollback_snapshot or guard.get("rollback_snapshot") or {})
    audit = dict(before_after_audit or guard.get("before_after_audit") or {})
    metamemory_payload = dict(metamemory_snapshot_payload or {})
    calibration = dict(calibration_delta or {})
    normalized_mode = str(mode or "sleep_evolve").strip().lower()
    normalized_tool = str(tool_name or "").strip()
    requested_apply = bool(apply_requested if apply_requested is not None else normalized_tool.endswith("_apply") or not preview_only)
    applied = bool(guard.get("applied"))
    proposal_ids = _string_list([proposal.get("proposal_id") for proposal in proposals], limit=300)
    selected_proposals = _maintenance_selected_proposals(proposals, selected_ids)
    phase_counts = {
        "sleep": sum(1 for proposal in proposals if _proposal_phase(proposal) == "sleep"),
        "evolve": sum(1 for proposal in proposals if _proposal_phase(proposal) == "evolve"),
        "matrix": sum(1 for proposal in proposals if _proposal_phase(proposal) == "matrix"),
        "review": sum(1 for proposal in proposals if _proposal_phase(proposal) == "review"),
    }
    classes_present = {
        "sleep": phase_counts["sleep"] > 0,
        "evolve": phase_counts["evolve"] > 0,
        "matrix": phase_counts["matrix"] > 0 or bool(calibration),
    }
    revision_keys = _maintenance_revision_keys(
        brain_id=brain_id,
        mode=normalized_mode,
        proposals=proposals,
        selected_proposal_ids=selected_ids,
        metamemory_snapshot_payload=metamemory_payload,
        apply_policy_guard=guard,
        before_after_audit=audit,
        calibration_delta=calibration,
    )
    conflict_resolution = _maintenance_conflict_resolution(
        proposals=proposals,
        selected_proposal_ids=selected_ids,
        archived_node_ids=_string_list(archived_node_ids or [], limit=300),
        superseded_node_ids=_string_list(superseded_node_ids or [], limit=300),
        confirm_apply=confirm_apply,
        apply_requested=requested_apply,
        blocked_reason=blocked_reason,
    )
    cache_plan = _maintenance_cache_invalidation_plan(
        revision_keys=revision_keys,
        applied=applied,
        selected_proposal_ids=selected_ids,
        classes_present=classes_present,
    )
    validation_plan = _maintenance_post_apply_validation_plan(
        applied=applied,
        classes_present=classes_present,
        selected_proposal_ids=selected_ids,
    )
    blocked_reasons = _string_list(
        [blocked_reason]
        + list(guard.get("blocked_reasons") or [])
        + selected_missing
        + list(conflict_resolution.get("blocked_reasons") or []),
        limit=120,
    )
    if applied:
        state = "applied_needs_validation"
    elif blocked_reasons and requested_apply:
        state = "blocked"
    elif requested_apply and selected_ids:
        state = "apply_pending"
    elif proposals or calibration:
        state = "preview_ready"
    else:
        state = "healthy_for_benchmark"
    if applied and validation_plan.get("status") == "passed":
        state = "healthy_for_benchmark"
    health_flags = {
        "needs_sleep": bool(classes_present.get("sleep")),
        "needs_evolve": bool(classes_present.get("evolve")),
        "needs_matrix_calibration": bool(classes_present.get("matrix")),
        "preview_ready": state == "preview_ready",
        "apply_pending": state == "apply_pending",
        "applied_needs_validation": state == "applied_needs_validation",
        "healthy_for_benchmark": state == "healthy_for_benchmark",
    }
    preview_signature = {
        "schema_version": "agvm.maintenance_preview_signature.v1",
        "signature_id": revision_keys["preview_signature_key"],
        "revision_keys": revision_keys,
        "proposal_ids": proposal_ids,
        "selected_proposal_ids": selected_ids,
        "phase_counts": phase_counts,
        "conflict_codes": conflict_resolution.get("conflict_codes"),
    }
    transaction_id = f"maintenance_tx::{_json_hash({'signature': preview_signature, 'state': state, 'tool_name': normalized_tool})}"
    return {
        "schema_version": MAINTENANCE_TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "state": state,
        "health_flags": health_flags,
        "mode": normalized_mode,
        "tool_name": normalized_tool,
        "brain_scope": {
            "brain_id": brain_id,
            "scope_required_for_apply": True,
            "signed_apply_cannot_cross_brain_boundary": True,
            "apply_brain_id_must_match_preview_brain_id": True,
            "scope_status": "scoped" if brain_id else "runtime_scope_unattached",
        },
        "phase_order": [
            {"phase": "scan", "status": "complete" if maintenance_preview_plan else "not_supplied"},
            {"phase": "sleep_candidate", "status": "present" if classes_present.get("sleep") else "not_needed"},
            {"phase": "evolve_candidate", "status": "present" if classes_present.get("evolve") else "not_needed"},
            {"phase": "optional_matrix_candidate", "status": "present" if classes_present.get("matrix") else "not_needed"},
            {"phase": "conflict_resolver", "status": "complete"},
            {"phase": "preview_signature", "status": "complete"},
            {"phase": "apply_selected_proposals", "status": "applied" if applied else "pending_or_not_requested"},
            {"phase": "post_apply_validation", "status": validation_plan.get("status")},
        ],
        "proposal_set": {
            "available_proposal_ids": proposal_ids,
            "selected_proposal_ids": selected_ids,
            "selected_missing_proposal_ids": selected_missing,
            "selected_proposal_count": len(selected_proposals),
            "all_available_count": len(proposals),
            "phase_counts": phase_counts,
            "selected_phase_counts": {
                "sleep": sum(1 for proposal in selected_proposals if _proposal_phase(proposal) == "sleep"),
                "evolve": sum(1 for proposal in selected_proposals if _proposal_phase(proposal) == "evolve"),
                "matrix": sum(1 for proposal in selected_proposals if _proposal_phase(proposal) == "matrix"),
                "review": sum(1 for proposal in selected_proposals if _proposal_phase(proposal) == "review"),
            },
        },
        "preview_signature": preview_signature,
        "conflict_resolution": conflict_resolution,
        "apply_contract": {
            "requested_apply": requested_apply,
            "confirm_apply": bool(confirm_apply),
            "preview_only": bool(preview_only),
            "backend_applied": applied,
            "selected_set_all_or_nothing": True,
            "independent_subgroups_supported": False,
            "can_apply_transaction": bool(requested_apply and confirm_apply and selected_ids and not selected_missing and not blocked_reasons and not conflict_resolution.get("apply_blocked")),
            "blocked_reasons": blocked_reasons,
            "requires_proposal_provenance": True,
            "requires_before_after_audit": True,
            "requires_rollback_snapshot": True,
        },
        "rollback_contract": {
            "available": bool(rollback),
            "snapshot_id": rollback.get("snapshot_id"),
            "raw_documents_preserved": rollback.get("raw_documents_preserved"),
            "all_or_nothing": True,
            "restore_revision": rollback.get("before_graph_hash") or audit.get("before_graph_hash"),
        },
        "metamemory_refresh_contract": {
            "required_after_apply": bool(applied and (classes_present.get("evolve") or classes_present.get("matrix") or classes_present.get("sleep"))),
            "required_before_benchmark": True,
            "current_snapshot_id": metamemory_payload.get("snapshot_id"),
            "current_spatial_brief_hash": dict(metamemory_payload.get("spatial_brief") or {}).get("hash"),
            "refresh_targets": [
                "metamemory_spatial_brief",
                "retrieval_role_package",
                "sleep_role_package",
                "matrix_geometry_brief",
                "atlas_topology_summary",
            ],
            "stale_cache_revision_keys": revision_keys,
        },
        "cache_invalidation_plan": cache_plan,
        "post_apply_validation_plan": validation_plan,
        "source_contracts": {
            "maintenance_preview_plan": dict(maintenance_preview_plan or {}),
            "sleep_consolidation_profile_id": dict(sleep_consolidation_profile or {}).get("profile_id"),
            "evolve_structural_profile_id": dict(evolve_structural_profile or {}).get("profile_id"),
            "retrieval_trace_learning_gate_id": dict(retrieval_trace_learning_gate or {}).get("gate_id"),
            "calibration_delta_present": bool(calibration),
        },
    }


def _geometry_failure_signatures(geometry_report: dict[str, Any]) -> dict[str, Any]:
    recommendations = [dict(item) for item in list(geometry_report.get("recommendations") or []) if isinstance(item, dict)]
    proposals = [dict(item) for item in list(geometry_report.get("calibration_proposals") or []) if isinstance(item, dict)]
    benchmarks = dict(geometry_report.get("benchmarks") or {})
    failing_benchmarks = [
        key
        for key, value in benchmarks.items()
        if isinstance(value, bool) and not value and key not in {"all_pass"}
    ]
    severity_histogram = _histogram([item.get("severity") for item in recommendations])
    return {
        "schema_version": "agvm.pr12h.geometry_failure_signatures.v1",
        "evidence_source": "brain_geometry_calibration",
        "score": _safe_float(geometry_report.get("score") or benchmarks.get("score") or 0.0),
        "recommendation_count": len(recommendations),
        "proposal_count": len(proposals),
        "severity_histogram": severity_histogram,
        "recommendation_codes": [str(item.get("code") or "") for item in recommendations[:12] if str(item.get("code") or "").strip()],
        "failing_benchmarks": failing_benchmarks,
        "review_required": bool(recommendations or proposals or failing_benchmarks),
    }


def _retrieval_failure_signatures(retrieval_gap_review: dict[str, Any], trace_insights: dict[str, Any]) -> dict[str, Any]:
    gap_reasons = dict(retrieval_gap_review.get("gap_reasons") or {})
    stop_reasons = dict(trace_insights.get("stop_reasons") or {})
    return {
        "schema_version": "agvm.pr12h.retrieval_failure_signatures.v1",
        "evidence_source": "recent_search_sessions.search_events.heuristic_calibration",
        "session_count": _safe_int(retrieval_gap_review.get("session_count") or 0),
        "gap_session_count": _safe_int(retrieval_gap_review.get("gap_session_count") or 0),
        "route_gap_session_count": _safe_int(retrieval_gap_review.get("route_gap_session_count") or 0),
        "final_eval_failure_session_count": _safe_int(retrieval_gap_review.get("final_eval_failure_session_count") or 0),
        "gap_reasons": gap_reasons,
        "stop_reasons": stop_reasons,
        "maintenance_retrieval_gap_detection_ratio": _safe_float(
            retrieval_gap_review.get("maintenance_retrieval_gap_detection_ratio") or 0.0
        ),
        "route_gap_examples": list(retrieval_gap_review.get("route_gap_examples") or [])[:5],
        "final_eval_failure_examples": list(retrieval_gap_review.get("final_eval_failure_examples") or [])[:5],
        "review_required": bool(gap_reasons or stop_reasons or list(retrieval_gap_review.get("recommendations") or [])),
    }


def _source_failure_signatures(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    document_nodes = [node for node in nodes if bool(node.get("is_document_anchor")) or str(node.get("memory_type") or "") == "document_anchor"]
    missing_raw_document_ids = [_node_id(node) for node in document_nodes if not _node_text(node)]
    low_confidence_ids = [
        _node_id(node)
        for node in nodes
        if _node_id(node)
        and min(
            _safe_float(node.get("memory_confidence"), 1.0),
            _safe_float(node.get("evidence_confidence"), 1.0),
            _safe_float(node.get("stability_confidence"), 1.0),
        )
        < 0.35
    ]
    source_metadata_ids = [
        _node_id(node)
        for node in nodes
        if str(node.get("claim_status") or "") in {"source_metadata", "test_artifact"}
        or str(node.get("source_trust") or "") in {"system_metadata", "synthetic_test"}
    ]
    signatures: list[dict[str, Any]] = []
    if missing_raw_document_ids:
        signatures.append(
            {
                "code": "document_anchor_missing_raw_text",
                "severity": "high",
                "target_node_ids": missing_raw_document_ids[:12],
                "count": len(missing_raw_document_ids),
                "review_only": True,
            }
        )
    if low_confidence_ids:
        signatures.append(
            {
                "code": "low_source_or_memory_confidence",
                "severity": "medium",
                "target_node_ids": low_confidence_ids[:12],
                "count": len(low_confidence_ids),
                "review_only": True,
            }
        )
    if source_metadata_ids:
        signatures.append(
            {
                "code": "non_product_source_metadata_present",
                "severity": "low",
                "target_node_ids": source_metadata_ids[:12],
                "count": len(source_metadata_ids),
                "review_only": True,
            }
        )
    return {
        "schema_version": "agvm.pr12h.source_failure_signatures.v1",
        "evidence_source": "graph_source_hygiene",
        "node_count": len(nodes),
        "document_anchor_count": len(document_nodes),
        "source_trust_histogram": _histogram([node.get("source_trust") for node in nodes]),
        "claim_status_histogram": _histogram([node.get("claim_status") for node in nodes]),
        "source_type_histogram": _histogram([(node.get("provenance") or {}).get("source_type") for node in nodes]),
        "missing_raw_document_anchor_count": len(missing_raw_document_ids),
        "low_confidence_node_count": len(low_confidence_ids),
        "metadata_or_test_artifact_count": len(source_metadata_ids),
        "signatures": signatures,
        "review_required": bool(signatures),
    }


def _write_failure_signatures(correction_insights: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    review_nodes = [
        _node_id(node)
        for node in nodes
        if bool(node.get("requires_human_review")) or list(node.get("cognitive_review_reasons") or [])
    ]
    hypothesis_nodes = [
        _node_id(node)
        for node in nodes
        if str(node.get("claim_status") or "") in {"hypothesis", "inferred", "possibility"}
    ]
    target_hits = dict(correction_insights.get("target_hits") or {})
    target_modes = dict(correction_insights.get("target_modes") or {})
    mode_counts = dict(correction_insights.get("mode_counts") or {})
    high_impact_modes = {
        key: value
        for key, value in mode_counts.items()
        if str(key) in {"delete", "replace", "supersede", "relationship", "identity", "contradiction", "revise"}
    }
    signatures: list[dict[str, Any]] = []
    if target_hits:
        signatures.append(
            {
                "code": "recent_write_correction_targets",
                "severity": "medium",
                "target_node_ids": list(target_hits.keys())[:12],
                "count": sum(_safe_int(value) for value in target_hits.values()),
                "review_only": True,
            }
        )
    if high_impact_modes:
        signatures.append(
            {
                "code": "high_impact_write_modes_seen",
                "severity": "high",
                "mode_counts": high_impact_modes,
                "count": sum(_safe_int(value) for value in high_impact_modes.values()),
                "review_only": True,
            }
        )
    if review_nodes:
        signatures.append(
            {
                "code": "pending_human_review_nodes",
                "severity": "medium",
                "target_node_ids": review_nodes[:12],
                "count": len(review_nodes),
                "review_only": True,
            }
        )
    return {
        "schema_version": "agvm.pr12h.write_failure_signatures.v1",
        "evidence_source": "correction_history.cognitive_write_annotations",
        "correction_target_count": len(target_hits),
        "correction_mode_counts": mode_counts,
        "correction_target_modes": target_modes,
        "pending_human_review_node_count": len(review_nodes),
        "hypothesis_node_count": len(hypothesis_nodes),
        "hypothesis_node_ids": hypothesis_nodes[:12],
        "signatures": signatures,
        "review_required": bool(signatures),
    }


def build_maintenance_baseline_contract(
    *,
    mode: str,
    preview_only: bool,
    focus_node_id: str | None,
    max_nodes_considered: int,
    graph: dict[str, Any],
    geometry_report: dict[str, Any],
    trace_insights: dict[str, Any],
    correction_insights: dict[str, Any],
    retrieval_gap_review: dict[str, Any],
    working_memory_depromotion_policy: dict[str, Any],
    ingest_learning_review: dict[str, Any] | None = None,
    quality_before: dict[str, Any],
) -> dict[str, Any]:
    metamemory = build_versioned_metamemory_snapshot(role="sleep")
    proposal_schema = maintenance_proposal_schema()
    failure_signatures = {
        "schema_version": FAILURE_SIGNATURE_SCHEMA_VERSION,
        "geometry": _geometry_failure_signatures(geometry_report),
        "retrieval": _retrieval_failure_signatures(retrieval_gap_review, trace_insights),
        "source": _source_failure_signatures(graph),
        "write": _write_failure_signatures(correction_insights, graph),
        "working_memory": {
            "schema_version": "agvm.pr12h.working_memory_failure_signatures.v1",
            "evidence_source": "warm_thread_state",
            "warm_state_count": _safe_int(working_memory_depromotion_policy.get("warm_state_count") or 0),
            "depromotion_candidate_count": _safe_int(working_memory_depromotion_policy.get("depromote_candidate_count") or 0),
            "token_pressure_candidate_count": _safe_int(working_memory_depromotion_policy.get("token_pressure_candidate_count") or 0),
            "contradiction_risk_candidate_count": _safe_int(working_memory_depromotion_policy.get("contradiction_risk_candidate_count") or 0),
            "review_required": bool(list(working_memory_depromotion_policy.get("candidates") or [])),
        },
        "ingest_learning": {
            "schema_version": "agvm.m3.ingest_learning_failure_signatures.v1",
            "evidence_source": "memory_learning_events",
            "review_id": dict(ingest_learning_review or {}).get("review_id"),
            "event_count": _safe_int(dict(ingest_learning_review or {}).get("event_count") or 0),
            "priority_event_count": _safe_int(dict(ingest_learning_review or {}).get("priority_event_count") or 0),
            "event_kind_histogram": dict(dict(ingest_learning_review or {}).get("event_kind_histogram") or {}),
            "candidate_node_count": len(list(dict(ingest_learning_review or {}).get("candidate_node_ids") or [])),
            "human_readable_evidence": list(dict(ingest_learning_review or {}).get("human_readable_evidence") or [])[:12],
            "whole_brain_cursor": dict(dict(ingest_learning_review or {}).get("whole_brain_cursor") or {}),
            "review_required": bool(_safe_int(dict(ingest_learning_review or {}).get("priority_event_count") or 0)),
        },
    }
    review_required = any(bool(dict(value).get("review_required")) for value in failure_signatures.values() if isinstance(value, dict))
    contract_seed = {
        "mode": mode,
        "preview_only": preview_only,
        "focus_node_id": focus_node_id,
        "max_nodes_considered": max_nodes_considered,
        "metamemory_snapshot_id": metamemory["snapshot_id"],
        "failure_signatures": failure_signatures,
    }
    contract = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": f"maintenance_contract::{_json_hash(contract_seed)}",
        "slice": "PR-12H-A",
        "mode": str(mode or "sleep_evolve"),
        "preview_only": bool(preview_only),
        "focus_node_id": focus_node_id,
        "max_nodes_considered": int(max_nodes_considered),
        "created_at": utc_timestamp(),
        "metamemory_snapshot_id": metamemory["snapshot_id"],
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "failure_signature_schema_version": FAILURE_SIGNATURE_SCHEMA_VERSION,
        "mutation_boundary": {
            "preview_only": bool(preview_only),
            "mutates_graph": not bool(preview_only),
            "failure_signatures_mutate_graph": False,
            "metamemory_snapshot_mutates_graph": False,
            "proposal_schema_mutates_graph": False,
            "auto_apply_allowed": False,
        },
        "evidence_sources": [
            "brain_geometry_calibration",
            "recent_search_sessions",
            "search_events",
            "correction_history",
            "warm_thread_state",
            "graph_source_hygiene",
            "metamemory",
            "memory_learning_events",
            "ingest_learning_feedback",
        ],
        "quality_baseline": dict(quality_before),
        "review_required": review_required,
        "closure_gates": {
            "proposal_schema_present": bool(proposal_schema),
            "metamemory_snapshot_versioned": bool(metamemory.get("snapshot_id")),
            "geometry_signatures_recorded": "geometry" in failure_signatures,
            "retrieval_signatures_recorded": "retrieval" in failure_signatures,
            "source_signatures_recorded": "source" in failure_signatures,
            "write_signatures_recorded": "write" in failure_signatures,
            "ingest_learning_signatures_recorded": "ingest_learning" in failure_signatures,
            "preview_non_mutating_contract": bool(preview_only),
        },
    }
    return {
        "maintenance_contract": contract,
        "proposal_schema": proposal_schema,
        "metamemory_snapshot": metamemory,
        "failure_signatures": failure_signatures,
    }
